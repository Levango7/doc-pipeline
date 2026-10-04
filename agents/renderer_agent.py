"""Renderer Agent v1.0 - 多格式渲染插件（Markdown → docx / pdf）

定位：把管线产出的 Markdown 中间态渲染成交付格式。
这是系统里第一个"产出多种格式"的节点，也是"文档生成系统"区别于
"Markdown 生成器"的关键一步。

设计要点：
- **不破坏主产物**：Markdown 始终是中间态，docx/pdf 是衍生产物。
  渲染失败只记 warning，不让整条流水线失败（降级，与 layout 一致的策略）
- **后端可选**：python-docx / reportlab 未安装时如实回报跳过原因，
  不抛异常中断流水线
- 输出路径由配置或 pipeline 决定，绝不接受外部传入的任意路径
"""
from pathlib import Path
from typing import Any

from pipeline_core import renderer
from pipeline_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "renderer"
# 随产品发布的内置 Agent：显式声明信任，加载器据此跳过 AST 沙箱检查
# （信任来自声明本身，不再依赖 core 里写死的名单）
SANDBOX_TRUSTED = True
AGENT_VERSION = "1.0"
AGENT_DESC = "多格式渲染 Agent - Markdown → docx/pdf（OOXML + ReportLab 双路线）"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 80
INPUT_TOPICS = ["renderer.render", "renderer.input", "layout.done", "checker.done"]
OUTPUT_TOPICS = ["renderer.done", "renderer.failed"]
# 产物契约（引擎按此声明组装下游载荷，见 pipeline_core/artifacts.py）：产出文件在磁盘，结果仅供观测
PRODUCES: dict = {}
CONSUMES = ["content"]
DEPENDENCIES = ["layout"]
CACHE_TTL = 0
RESPAWN = False


class RendererAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._formats = self._parse_formats(config.get("formats"))
        self._output_dir = config.get("output_dir", "output")
        self._title = config.get("title", "")
        self.log_info(
            f"Renderer v{AGENT_VERSION} 初始化完成"
            f"（格式: {','.join(self._formats) or '无'}，"
            f"当前环境可用: {','.join(renderer.supported_formats()) or '无'}）"
        )

    @staticmethod
    def _parse_formats(raw) -> list[str]:
        """formats 配置归一化；空/缺失时按环境实际可用能力决定。"""
        if not raw:
            return renderer.supported_formats()
        if isinstance(raw, str):
            raw = [f.strip() for f in raw.split(",")]
        wanted = [str(f).strip().lower().lstrip(".") for f in raw if str(f).strip()]
        available = renderer.supported_formats()
        # 只保留环境真正装了的格式，避免必然失败的渲染
        return [f for f in wanted if f in available]

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "开始渲染...")
        payload = msg.payload
        content = payload.get("content", "")
        task_id = payload.get("task_id", "")

        if not content:
            empty_result: dict[str, Any] = {
                "status": "error", "message": "内容为空，无法渲染"}
            self.publish("renderer.failed",
                         {"task_id": task_id, **empty_result})
            return empty_result

        title = payload.get("title") or self._title or "生成文档"
        base_name = self._resolve_base_name(payload, task_id)
        outputs: dict[str, dict] = {}

        for fmt in self._formats:
            target = Path(self._output_dir) / f"{base_name}.{fmt}"
            res = renderer.render(content, target, fmt=fmt, title=title)
            if res.get("status") == "ok":
                size_kb = res.get("size", 0) / 1024
                self.log_info(f"{fmt} 渲染完成: {target.name} ({size_kb:.1f} KB)")
            else:
                self.log_warning(f"{fmt} 渲染跳过: {res.get('message')}")
            outputs[fmt] = res

        ok_formats = [f for f, r in outputs.items() if r.get("status") == "ok"]
        skipped = {f: r.get("message") for f, r in outputs.items()
                   if r.get("status") != "ok"}

        # 显式标注为 dict[str, Any]：mypy 会按首个键把字面量推断成
        # dict[str, str]，后续塞入 list/dict 就报错
        result: dict[str, Any] = {
            "status": "ok" if ok_formats else "error",
            "task_id": task_id,
            "formats": ok_formats,
            "outputs": {f: r["path"] for f, r in outputs.items()
                        if r.get("status") == "ok"},
            "sizes": {f: r["size"] for f, r in outputs.items()
                      if r.get("status") == "ok"},
        }
        if skipped:
            result["skipped"] = skipped
        # 全部失败才算流水线失败：Markdown 产物仍在，渲染是增强而非前置
        if not ok_formats:
            result["message"] = "无可用渲染后端（pip install python-docx reportlab）"

        self.publish(
            "renderer.done" if ok_formats else "renderer.failed",
            result,
        )
        return result

    @staticmethod
    def _resolve_base_name(payload: dict, task_id: str) -> str:
        """产物文件名：优先 target 提供的基名，否则用 task_id 兜底。"""
        target = payload.get("target") or payload.get("output_path") or ""
        if target:
            stem = Path(str(target)).stem
            if stem:
                return stem
        if task_id:
            safe = "".join(c for c in str(task_id) if c.isalnum() or c in "-_")[:60]
            if safe:
                return safe
        return "document"

"""Layout Agent v2 - 排版优化插件"""
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta

AGENT_NAME = "layout"
AGENT_VERSION = "2.0"
AGENT_DESC = "排版优化 Agent - 智能图表修复、表格对齐"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 70
INPUT_TOPICS = ["checker.done", "layout.optimize", "layout.input"]
OUTPUT_TOPICS = ["layout.done"]
DEPENDENCIES = ["checker"]
CACHE_TTL = 0
RESPAWN = False

class LayoutAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._dry_run = config.get("dry_run", False)
        self.log_info(f"Layout v{AGENT_VERSION} 初始化完成")

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "开始排版优化...")
        payload = msg.payload
        content = payload.get("content", "")
        target = payload.get("target") or payload.get("file") or payload.get("target_file")

        # 优先处理 message 内的 content（DAG 流式传递）
        if content:
            result = self._optimize_content(content)
            result["content"] = result.get("optimized", content)
            return result

        if not target:
            return {"status": "error", "message": "未指定文件或内容"}
        return self._optimize(target)

    def _optimize_content(self, content: str) -> dict:
        """直接优化 content 字符串（不依赖文件）"""
        try:
            scripts_dir = Path(__file__).parent.parent / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            from layout_optimizer import LayoutOptimizer
            opt = LayoutOptimizer(content)
            opt.run()
            fixed = opt.get_result_stats().get("borders_fixed", 0) if hasattr(opt, "get_result_stats") else 0
            self.report(AgentStatus.RUNNING, f"修复 {fixed} 处边框")
            if not self._dry_run:
                return {"status": "ok", "optimized": opt.get_result(), "fixed": fixed}
            return {"status": "ok", "optimized": content, "fixed": fixed}
        except Exception as e:
            self.log_warning(f"排版优化失败（保留原文）: {e}")
            return {"status": "ok", "optimized": content, "fixed": 0}

    def _optimize(self, target: str) -> dict:
        import os
        target = str(Path(target).resolve())
        if not os.path.exists(target):
            return {"status": "error", "message": f"文件不存在: {target}"}
        try:
            scripts_dir = Path(__file__).parent.parent / "scripts"
            if str(scripts_dir) not in sys.path:
                sys.path.insert(0, str(scripts_dir))
            from layout_optimizer import LayoutOptimizer
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            opt = LayoutOptimizer(content)
            result = opt.run()
            fixed = result.get("stats", {}).get("borders_fixed", 0)
            self.report(AgentStatus.RUNNING, f"修复 {fixed} 处边框")
            if fixed > 0 and not self._dry_run:
                new_content = opt.get_result()
                from safe_writer import safe_write
                backup_dir = Path(target).parent / "backups"
                write_result = safe_write(
                    target=target,
                    content=new_content,
                    backup_dir=str(backup_dir),
                    reason="layout_optimizer",
                    agent="LayoutAgent"
                )
                result["write_result"] = write_result
            return {"status": "ok", "target": target, "borders_fixed": fixed, "has_changes": fixed > 0}
        except Exception as e:
            self.log_error(f"排版优化失败: {e}")
            return {"status": "error", "message": str(e)}

    def handle_checker_done(self, msg: Message):
        payload = msg.payload
        target = payload.get("target")
        if target:
            self.report(AgentStatus.RUNNING, f"排版优化 {Path(target).name}")
            result = self._optimize(target)
            self.publish("layout.done", result)

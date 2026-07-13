"""
Bootstrap — 启动自检 + 零配置降级
==================================
核心特性：
  - 环境校验（Python 版本、依赖包、目录结构）
  - LLM 路由健康检查
  - 搜索引擎可用性检查
  - 配置文件校验
  - 优雅降级：缺失可选依赖时自动降级而非崩溃
  - 启动报告：一目了然的状态摘要

用法：
    from pipeline_core.bootstrap import run_startup_check
    report = run_startup_check()
    if report.has_errors:
        print(report.summary())
        sys.exit(1)
"""
import os
import sys
import time
import json
import logging
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """单项检查结果"""
    name: str
    status: str       # "ok" | "warn" | "error" | "skip"
    message: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def is_warn(self) -> bool:
        return self.status == "warn"


@dataclass
class StartupReport:
    """启动检查报告"""
    checks: list[CheckResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def has_errors(self) -> bool:
        return any(c.is_error for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.is_warn for c in self.checks)

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.is_ok)

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if c.is_error)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.is_warn)

    def add(self, result: CheckResult):
        self.checks.append(result)

    def summary(self) -> str:
        """生成摘要文本"""
        lines = [
            "=" * 60,
            "Doc-Pipeline 启动检查报告",
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}",
            f"结果: {self.ok_count} OK / {self.warn_count} WARN / {self.error_count} ERROR",
            "=" * 60,
        ]
        for c in self.checks:
            icon = {"ok": "[OK]", "warn": "[WARN]", "error": "[ERR]", "skip": "[SKIP]"}[c.status]
            lines.append(f"  {icon} {c.name}: {c.message}")
        lines.append("=" * 60)
        if self.has_errors:
            lines.append("存在错误，建议修复后重新启动")
        elif self.has_warnings:
            lines.append("存在警告，系统将以降级模式运行")
        else:
            lines.append("所有检查通过，系统就绪")
        lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "has_errors": self.has_errors,
            "has_warnings": self.has_warnings,
            "ok": self.ok_count,
            "warn": self.warn_count,
            "error": self.error_count,
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message}
                for c in self.checks
            ],
        }


# ─── 检查项 ──────────────────────────────────────

def _check_python_version(report: StartupReport):
    """检查 Python 版本"""
    v = sys.version_info
    if v >= (3, 10):
        report.add(CheckResult("Python 版本", "ok", f"{v.major}.{v.minor}.{v.micro}"))
    elif v >= (3, 8):
        report.add(CheckResult("Python 版本", "warn",
                               f"{v.major}.{v.minor}.{v.micro} (建议 3.10+)"))
    else:
        report.add(CheckResult("Python 版本", "error",
                               f"{v.major}.{v.minor}.{v.micro} (最低 3.8)"))


def _check_dependencies(report: StartupReport):
    """检查依赖包"""
    required = {"yaml": "PyYAML"}
    optional = {
        "aiohttp": "aiohttp (异步 HTTP，缺失时 fetcher 降级为同步)",
        "numpy": "numpy (TF-IDF 语义匹配，缺失时降级为关键词匹配)",
        "requests": "requests (HTTP 请求，缺失时使用 urllib)",
    }

    for mod, name in required.items():
        try:
            importlib.import_module(mod)
            report.add(CheckResult(f"依赖 {name}", "ok", "已安装"))
        except ImportError:
            report.add(CheckResult(f"依赖 {name}", "error", "未安装（必需）"))

    for mod, name in optional.items():
        try:
            importlib.import_module(mod)
            report.add(CheckResult(f"依赖 {name}", "ok", "已安装"))
        except ImportError:
            report.add(CheckResult(f"依赖 {name}", "warn", "未安装（可选，将降级）"))


def _check_project_structure(report: StartupReport, project_root: Path):
    """检查项目目录结构"""
    required_dirs = ["pipeline_core", "agents", "pipelines", "scripts", "tests"]
    for d in required_dirs:
        if (project_root / d).is_dir():
            report.add(CheckResult(f"目录 {d}/", "ok", "存在"))
        else:
            report.add(CheckResult(f"目录 {d}/", "error", "缺失"))

    # 关键文件
    key_files = [
        ("pipeline_core/__init__.py", True),
        ("pipeline_core/dag_executor.py", True),
        ("pipeline_core/scheduler.py", True),
        ("agents/writer.py", True),
        ("agents/researcher.py", True),
        ("pipelines/docgen.yaml", True),
        (".env", False),
        ("config.json", False),
    ]
    for fpath, required in key_files:
        exists = (project_root / fpath).exists()
        if exists:
            report.add(CheckResult(f"文件 {fpath}", "ok", "存在"))
        elif required:
            report.add(CheckResult(f"文件 {fpath}", "error", "缺失（必需）"))
        else:
            report.add(CheckResult(f"文件 {fpath}", "warn", "缺失（可选）"))


def _check_llm_router(report: StartupReport):
    """检查 LLM 路由器"""
    try:
        from pipeline_core.llm_router import LLMRouter, get_router
        router = LLMRouter.from_env()
        active = router.get_active_providers()
        if active:
            names = [p.name for p in active]
            report.add(CheckResult("LLM 路由器", "ok",
                                   f"{len(active)} 个供应商可用: {', '.join(names)}"))
        else:
            report.add(CheckResult("LLM 路由器", "error",
                                   "无可用供应商（请检查 .env 配置）"))
    except Exception as e:
        report.add(CheckResult("LLM 路由器", "error", f"初始化失败: {e}"))


def _check_search_engines(report: StartupReport):
    """检查搜索引擎"""
    try:
        from pipeline_core.search_engines import SearchEngineManager
        mgr = SearchEngineManager.from_env()
        available = {name: eng.is_available() for name, eng in mgr._engines.items()}
        active = [k for k, v in available.items() if v]
        if active:
            report.add(CheckResult("搜索引擎", "ok",
                                   f"{len(active)} 个引擎可用: {', '.join(active)}"))
        else:
            report.add(CheckResult("搜索引擎", "warn",
                                   "无可用搜索引擎（将无法执行搜索）"))
    except Exception as e:
        report.add(CheckResult("搜索引擎", "error", f"初始化失败: {e}"))


def _check_pipeline_config(report: StartupReport, project_root: Path):
    """检查流水线配置"""
    docgen = project_root / "pipelines" / "docgen.yaml"
    if not docgen.exists():
        report.add(CheckResult("流水线配置", "error", "docgen.yaml 不存在"))
        return
    try:
        import yaml
        with open(docgen, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        agents = cfg.get("agents", [])
        topology = cfg.get("topology", {})
        levels = topology.get("levels", [])
        report.add(CheckResult("流水线配置", "ok",
                               f"{len(agents)} 个 agent, {len(levels)} 层 DAG"))
    except Exception as e:
        report.add(CheckResult("流水线配置", "error", f"解析失败: {e}"))


def _check_output_dirs(report: StartupReport, project_root: Path):
    """检查输出目录（自动创建）"""
    dirs = ["output", "logs", "cache", "checkpoints", "backups", "tmp_fetcher"]
    for d in dirs:
        path = project_root / d
        if path.is_dir():
            report.add(CheckResult(f"输出目录 {d}/", "ok", "存在"))
        else:
            try:
                path.mkdir(parents=True, exist_ok=True)
                report.add(CheckResult(f"输出目录 {d}/", "ok", "自动创建"))
            except Exception:
                report.add(CheckResult(f"输出目录 {d}/", "warn", "无法创建"))


def _check_git(report: StartupReport, project_root: Path):
    """检查 Git 仓库"""
    git_dir = project_root / ".git"
    if git_dir.is_dir():
        report.add(CheckResult("Git 仓库", "ok", "已初始化"))
    else:
        report.add(CheckResult("Git 仓库", "warn", "未初始化（建议 git init）"))


# ─── 主入口 ──────────────────────────────────────

def run_startup_check(project_root: str = None,
                      run_health_check: bool = False) -> StartupReport:
    """运行启动检查

    Args:
        project_root: 项目根目录（默认自动检测）
        run_health_check: 是否运行 LLM 健康检查（耗时，默认关闭）

    Returns:
        StartupReport
    """
    if project_root is None:
        project_root = Path(__file__).parent.parent
    else:
        project_root = Path(project_root)

    report = StartupReport()

    # 基础检查
    _check_python_version(report)
    _check_dependencies(report)
    _check_project_structure(report, project_root)
    _check_pipeline_config(report, project_root)
    _check_output_dirs(report, project_root)
    _check_git(report, project_root)

    # 模块检查
    _check_llm_router(report)
    _check_search_engines(report)

    # 可选：LLM 健康检查（发送测试请求）
    if run_health_check:
        try:
            from pipeline_core.llm_router import get_router
            router = get_router()
            health = router.health_check_all()
            healthy = sum(1 for v in health.values() if v.get("healthy"))
            total = len(health)
            if healthy > 0:
                report.add(CheckResult("LLM 健康检查", "ok",
                                       f"{healthy}/{total} 个供应商健康"))
            else:
                report.add(CheckResult("LLM 健康检查", "error",
                                       "所有供应商不健康"))
        except Exception as e:
            report.add(CheckResult("LLM 健康检查", "error", str(e)))

    return report


def quick_check(project_root: str = None) -> bool:
    """快速检查（只返回 True/False，不生成报告）"""
    report = run_startup_check(project_root)
    return not report.has_errors

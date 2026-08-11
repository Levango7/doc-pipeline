"""检查点管理器 —— 负责断点续传、检查点保存/加载/清理"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 安全修复 (P0): task_id 路径遍历防护 —— 仅允许字母/数字/下划线/连字符
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_task_id(task_id: str) -> bool:
    """校验 task_id 仅含字母/数字/下划线/连字符（防止路径遍历）。"""
    if not task_id or len(task_id) > 128:
        return False
    return bool(_TASK_ID_RE.match(task_id))


class CheckpointManager:
    """断点续传管理"""

    def __init__(self, checkpoint_dir: str = "checkpoints", logger=None):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logger

    def _log(self, level: str, msg: str, **kw):
        """结构化日志记录"""
        if self._logger:
            self._logger.log(level, msg, **kw)

    def save(self, task, full_state: bool = False, agent_snapshots: dict = None):
        """保存断点"""
        # 安全修复 (P0): task_id 路径遍历防护
        if not _validate_task_id(task.id):
            raise ValueError(f"无效的 task_id: {task.id!r}")
        try:
            data = task.to_dict()
            if full_state:
                data["_result"] = {
                    k: v for k, v in task.result.items()
                }
                data["_steps"] = [
                    {
                        "step_name": s.step_name,
                        "agent_name": s.agent_name,
                        "status": s.status,
                        "started_at": s.started_at,
                        "finished_at": s.finished_at,
                        "error": s.error,
                    }
                    for s in task.steps
                ]
                data["_dag_nodes"] = {
                    name: {
                        "status": n.status,
                        "error": n.error,
                        "attempts": n.attempts,
                        "result_keys": list(n.result.keys()),
                    }
                    for name, n in (task.dag_nodes or {}).items()
                }
            if agent_snapshots:
                data["agent_snapshots"] = agent_snapshots
            with open(task.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            self._log("error", "保存断点失败", error=str(e))

    def load(self, task_id: str):
        """加载断点（仅恢复基础信息），返回 (task, agent_snapshots) 元组"""
        # 安全修复 (P0): task_id 路径遍历防护
        if not _validate_task_id(task_id):
            raise ValueError(f"无效的 task_id: {task_id!r}")
        from .pipeline import PipelineTask, TaskStatus

        checkpoint_file = self.checkpoint_dir / f"{task_id}.json"
        if not checkpoint_file.exists():
            return None, None

        try:
            with open(checkpoint_file, encoding="utf-8") as f:
                data = json.load(f)

            # 断点完整性校验
            required = ["id", "pipeline", "input"]
            missing = [k for k in required if k not in data]
            if missing:
                self._log("warning", "断点损坏", missing=missing)
                return None, None

            # 验证关键字段不为空
            if not data.get("id") or not data.get("pipeline"):
                self._log("warning", "断点无效")
                return None, None

            task = PipelineTask(
                id=data["id"],
                pipeline_name=data["pipeline"],
                input_file=data.get("input", ""),
                config=data.get("config", {}),
                status=TaskStatus.PAUSED,
                current_step=len(data.get("steps", [])),
            )
            task.result = data.get("_result", {})
            agent_snapshots = data.get("agent_snapshots", {})
            return task, agent_snapshots
        except Exception as e:
            self._log("error", "加载断点失败", error=str(e))
            return None, None

    def remove(self, task_id: str):
        """移除断点文件"""
        # 安全修复 (P0): task_id 路径遍历防护
        if not _validate_task_id(task_id):
            raise ValueError(f"无效的 task_id: {task_id!r}")
        checkpoint_file = self.checkpoint_dir / f"{task_id}.json"
        try:
            if checkpoint_file.exists():
                checkpoint_file.unlink()
        except Exception as e:
            self._log("warning", "移除断点文件失败", error=str(e))

    def cleanup_old(self, max_age_days: int = 7):
        """清理过期的 checkpoint 和报告文件"""
        cutoff = time.time() - max_age_days * 86400
        for f in self.checkpoint_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError as e:
                    self._log("warning", "清理过期 checkpoint 失败", file=str(f), error=str(e))

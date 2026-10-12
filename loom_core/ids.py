"""任务 ID 生成统一入口（admin_api / mcp_server / run 共用）"""
from __future__ import annotations

import uuid


def new_task_id() -> str:
    """16 位 hex 短任务 ID：uuid4 截断，碰撞概率足够低且保持短"""
    return uuid.uuid4().hex[:16]

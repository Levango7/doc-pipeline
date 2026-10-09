"""审计留痕：追加一行 JSONL（谁在什么时候对什么做了什么）。

两条写入路径的先例是入站 webhook（续22）：每次调用无论成败都落一行到
`state_root()/audit/<channel>.jsonl`。配置变更（续25）沿用同一份实现——
不做第二份"差不多的写日志代码"，也就不会出现两份格式悄悄漂移。

约定：
- 目录可用 `DOC_PIPELINE_STATE_DIR` 重定向（与四个 store 同源）；
- 写盘失败**不静默**：返回错误字符串，调用方必须如实回报给请求方；
- 值里可能带密钥（配置值、令牌），调用方负责先脱敏再传进来——
  本模块不做猜测性擦除（猜错比不擦更危险）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Lock

from artesian.fast_json import dumps as _fast_dumps

from .state_paths import state_root

_LOCK = Lock()


def audit_path(channel: str) -> Path:
    """某个审计通道的 JSONL 路径（channel 为文件名主体，如 webhooks / config）。"""
    return state_root() / "audit" / f"{channel}.jsonl"


def write_audit(channel: str, record: dict) -> str | None:
    """向通道追加一行审计（含时间戳）。成功返回 None，失败返回错误信息。

    故意接住**所有**异常（不只 OSError）：审计失效可以，但审计失效把调用方的
    请求打成 500 不行。调用方拿到错误字符串后须如实回报（响应带 audit_error）
    ——失败被看见，不算静默。
    """
    try:
        p = audit_path(channel)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = _fast_dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            **record}, ensure_ascii=False)
        with _LOCK, open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return None
    except Exception as e:  # noqa: BLE001 —— 见 docstring：审计不许炸调用方
        return str(e)


def read_audit(channel: str, limit: int = 50) -> list[dict]:
    """读回最近 limit 条（供 API 侧查看；文件不存在返回空列表）。"""
    import json

    p = audit_path(channel)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                      # 半行（写盘中断）跳过，不因一行坏掉整份读取
    return out

"""运行时状态目录解析：把 SQLite store 与版本库的位置变成可重定向的。

此前四个 store 的默认路径写成 `<源码目录>/bus_data/*.db`，且在 **import 期**
就把字符串固化下来。后果有两层：

1. 测试进程与真实运行共用同一份历史。幂等键是 `{task_id}:{node}:{attempts}`，
   复用同一个 task_id 再跑一次就会命中历史记录，节点被静默跳过却逐个记成
   success（实测：`processed_keys` 里积了 1116 条，第二次跑同 id 的流水线
   全程空转、仍然 done + exit 0）。
2. 部署时状态无法外置（容器里源码目录可能只读）。

现在默认值在调用期解析，`DOC_PIPELINE_STATE_DIR` / `DOC_PIPELINE_VERSIONS_DIR`
可以把状态整体挪走；不设环境变量时行为与既往一致（仍是 `<checkout>/bus_data`）。
"""
from __future__ import annotations

import os
from pathlib import Path

STATE_DIR_ENV = "DOC_PIPELINE_STATE_DIR"
VERSIONS_DIR_ENV = "DOC_PIPELINE_VERSIONS_DIR"

_PROJECT_ROOT = Path(__file__).parent.parent.absolute()


def state_root() -> Path:
    """四个 SQLite store 的公共目录。"""
    override = (os.environ.get(STATE_DIR_ENV) or "").strip()
    return Path(override) if override else _PROJECT_ROOT / "bus_data"


def store_path(filename: str) -> str:
    return str(state_root() / filename)


def versions_root(default: str = "versions") -> str:
    """文档版本库根目录（默认沿用相对路径，受进程 cwd 影响）。"""
    override = (os.environ.get(VERSIONS_DIR_ENV) or "").strip()
    return override if override else default

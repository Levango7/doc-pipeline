"""`.env` 读取——取数层各后端的配置来源。

单独成模块而不是就地 `os.environ.get`，是因为搜索/嵌入这类后端需要
**同一份**配置解析：命令行环境、`.env` 文件、向上层目录查找的 .env，
三者要按固定优先级合并。合并规则里有两个容易踩的坑，都在实现里显式处理：
`.env` 中的空值行被丢弃（`KEY=` 不算配置），而系统环境里的空值不覆盖
`.env` 的有效值——否则一个 `BOCHA_API_KEY=""` 会把文件里配好的 Key 抹掉。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def load_env(env_path: str | None = None) -> dict[str, str]:
    """读取 `.env` 并与系统环境合并（系统非空值优先）。

    - `env_path=None` 时从当前目录向上找第一个 `.env`；
    - 找不到 `.env`（或路径不存在）时返回 `dict(os.environ)`；
    - 解析规则：忽略空行与 `#` 注释、忽略没有 `=` 的行、值去首尾空白与成对引号、
      空值不入库。
    """
    if env_path is None:
        cwd = Path.cwd()
        for p in [cwd] + list(cwd.parents):
            candidate = p / ".env"
            if candidate.exists():
                env_path = str(candidate)
                break

    env: dict[str, str] = {}
    if not env_path or not Path(env_path).exists():
        return dict(os.environ)

    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if value:
                    env[key] = value
    except Exception as e:
        logger.warning(f"加载 .env 失败: {e}")

    for k, v in os.environ.items():
        if v:
            env[k] = v

    return env

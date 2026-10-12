"""环境变量名双读：`LOOM_*` 优先，`DOC_PIPELINE_*` 过渡期回退。

改名批次（spec §7.1「env 前缀 `DOC_PIPELINE_*` → `LOOM_*`」）落地时只换
新名会撕掉一个已经存在的对外承诺：MAOP 侧按 spec §7.3 第 6 步的约定设置
`DOC_PIPELINE_ROOT`（它侧零改动，本项目代记账不代改），旧名一旦失效，
MAOP 立刻找不到本仓。所以读法定为：

  1. `LOOM_<后缀>`（新部署一律用这个）；
  2. `DOC_PIPELINE_<后缀>`（回退可读，首次命中记一条 warning 提示迁移）；
  3. 都没有 → 调用方默认值。

warning 只记一次/每名——双读期可能被高频调用（每个 store 的默认路径解析
都要走这里），不复读刷屏。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 新前缀 / 旧前缀（旧前缀仅过渡期可读）
PREFIX = "LOOM_"
LEGACY_PREFIX = "DOC_PIPELINE_"

_warned: set[str] = set()


def read(suffix: str, default: str | None = None) -> str | None:
    """按后缀读环境变量（LOOM_ 优先，DOC_PIPELINE_ 回退）。

    `read("ROOT")` 依次看 `LOOM_ROOT`、`DOC_PIPELINE_ROOT`。
    返回值不做 strip——与 os.environ 语义一致，由调用方决定怎么处理空串。
    """
    new_name = f"{PREFIX}{suffix}"
    value = os.environ.get(new_name)
    if value is not None:
        return value
    legacy_name = f"{LEGACY_PREFIX}{suffix}"
    value = os.environ.get(legacy_name)
    if value is not None:
        if legacy_name not in _warned:
            _warned.add(legacy_name)
            logger.warning(
                "环境变量 %s 已更名，请改用 %s（旧名过渡期仍可读）",
                legacy_name, new_name)
        return value
    return default


def reset_warning_cache() -> None:
    """仅供测试：清空"已警告"集合，便于断言 warning 只记一次。"""
    _warned.clear()

"""兼容 shim：旧包名 `pipeline_core` → 新包名 `loom_core`（spec §7.1 命名收官·第二段）。

旧名以**转发别名**保留：`import pipeline_core` 与 `import loom_core` 得到
**同一个模块对象**（`pipeline_core.scheduler is loom_core.scheduler`），类与函数
身份一致，isinstance / patch / 注册表 / 事件钩子全部照常工作；首次导入旧根名记
一条 DeprecationWarning，请新代码直接用 `loom_core`。

**这个文件本身是对外契约**：MAOP 的 `doc_pipeline_adapter._resolve_doc_pipeline_root()`
正是用 `pipeline_core/__init__.py` 的存在性判定项目根，其两条运行时 import
（`PipelineOrchestrator` / `event_hook`）也经本 shim 命中同一模块对象——
**它侧零改动**。

实现说明（两件套，与 `docpipeline` shim 同款）：
  1. 根名替换：`sys.modules["pipeline_core"] = loom_core`——importlib 官方支持
     模块在执行期替换自身（`_bootstrap._load` 会在 exec 后取回替换对象）；
  2. 子模块别名：meta-path finder 把 `pipeline_core.X` 映射到 `loom_core.X` 的
     **同一模块对象**（先于路径查找器生效）。

为什么不用"转发文件"（每个子模块一个 `from loom_core.x import *`）：那会制造
两份模块对象——`pipeline_core.pipeline.TaskStatus` 与 `loom_core.pipeline.TaskStatus`
不再 `is` 同一物，枚举比较 / isinstance / 测试 patch 全部碎掉。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = "pipeline_core"
_NEW = "loom_core"

#: 旧根名已警告过（同进程只提示一次）
_warned = False


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, new_name: str) -> None:
        self._new_name = new_name

    def create_module(self, spec):  # type: ignore[no-untyped-def]
        # 返回**已导入的目标模块本身**：身份保真是本 shim 的核心契约
        return importlib.import_module(self._new_name)

    def exec_module(self, module) -> None:  # type: ignore[no-untyped-def]
        # 目标模块已在 create_module 里执行过；不要重复执行
        pass


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # type: ignore[no-untyped-def]
        if fullname == _OLD or fullname.startswith(_OLD + "."):
            new_name = _NEW + fullname[len(_OLD):]
            return importlib.util.spec_from_loader(fullname, _AliasLoader(new_name))
        return None


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

if not _warned:
    _warned = True
    warnings.warn(
        f"import {_OLD} 已更名（spec §7.1）：请改用 {_NEW}；旧名以转发别名保留",
        DeprecationWarning, stacklevel=2)

# 根名替换必须在最后一行：importlib 取回替换后的对象
sys.modules[_OLD] = importlib.import_module(_NEW)

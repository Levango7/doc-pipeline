"""运行态路径隔离（P0-6）。

历史问题：四个 SQLite store 与文档版本库的默认位置在 **import 期** 就固化成
`<checkout>/bus_data/*.db`，于是

- 测试与真实运行共用同一份幂等键历史（`{task_id}:{node}:{attempts}`），
  同名 task_id 再跑一次会命中记录 → 节点静默空转仍报 success；
- `versions/` 被测试写进 260+ 个指向 `.pytest_tmp` 的死条目，
  `/api/versions/stats` 慢到 4.9s（客户端 5s 超时即翻成测试失败）。

现在两处都改为调用期解析并支持环境变量重定向；本文件锁住该契约。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core import state_paths  # noqa: E402

PROJECT = Path(__file__).parent.parent


def test_default_state_dir_is_checkout_bus_data(monkeypatch):
    monkeypatch.delenv(state_paths.STATE_DIR_ENV, raising=False)
    assert state_paths.state_root() == PROJECT / "bus_data"


def test_env_redirects_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(state_paths.STATE_DIR_ENV, str(tmp_path / "state"))
    assert state_paths.store_path("message_bus.db") == str(tmp_path / "state" / "message_bus.db")
    for name in ("message_bus.db", "tasks.db", "cost.db", "quality.db"):
        assert state_paths.store_path(name).startswith(str(tmp_path))


def test_versions_root_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv(state_paths.VERSIONS_DIR_ENV, raising=False)
    assert state_paths.versions_root() == "versions"
    monkeypatch.setenv(state_paths.VERSIONS_DIR_ENV, str(tmp_path / "ver"))
    assert state_paths.versions_root() == str(tmp_path / "ver")


@pytest.mark.parametrize("module_name,cls_name,db", [
    ("pipeline_core.message_store", "PersistentStore", "message_bus.db"),
    ("pipeline_core.task_queue", "TaskQueue", "tasks.db"),
    ("pipeline_core.cost_tracker", "CostTracker", "cost.db"),
    ("pipeline_core.quality_feedback", "QualityFeedback", "quality.db"),
])
def test_stores_honor_env_without_explicit_path(module_name, cls_name, db,
                                                 monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv(state_paths.STATE_DIR_ENV, str(tmp_path / "state"))
    cls = getattr(importlib.import_module(module_name), cls_name)
    obj = cls()
    path = getattr(obj, "db_path", None) or obj._db_path
    assert str(tmp_path / "state" / db) == str(path), (
        f"{cls_name} 没有按环境变量解析默认路径，实际 {path}")
    closer = getattr(obj, "close", None) or getattr(obj, "close_all", None)
    if callable(closer):
        closer()


def test_version_manager_honors_env(monkeypatch, tmp_path):
    from pipeline_core.version_manager import VersionManager
    monkeypatch.setenv(state_paths.VERSIONS_DIR_ENV, str(tmp_path / "ver"))
    vm = VersionManager()
    assert str(vm._root) == str(tmp_path / "ver")
    assert vm.commit("a.md", "内容一", task_id="t1")["version"] == 1
    assert (tmp_path / "ver").exists()
    # 真实 checkout 的 versions/ 不许被这次构造写脏
    assert not (PROJECT / "versions").joinpath("a.md").exists()


def test_conftest_isolates_session_state():
    """跑测试时，状态目录必须是 .test_state 而不是仓库的 bus_data/。"""
    override = os.environ.get(state_paths.STATE_DIR_ENV, "")
    assert override and ".test_state" in override.replace("\\", "/"), (
        f"conftest 未把测试状态隔离出 checkout（当前 {override!r}）")
    assert Path(override).is_dir()

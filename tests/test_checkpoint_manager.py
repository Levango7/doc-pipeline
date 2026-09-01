"""tests/test_checkpoint_manager.py — CheckpointManager 原子写/加载/清理。"""
import time

import pytest

from pipeline_core.checkpoint_manager import CheckpointManager


class _FakeTask:
    def __init__(self, task_id):
        self.id = task_id
        self.pipeline_name = "docgen"
        self.input_file = "in.md"
        self.config = {}
        self.result = {"writer": {"content": "hello"}}
        self.steps = []
        self.checkpoint_file = ""
        self.dag_nodes = {}

    def to_dict(self):
        return {
            "id": self.id,
            "pipeline": self.pipeline_name,
            "input": self.input_file,
            "config": self.config,
        }


class TestCheckpointManager:
    def test_save_and_load_roundtrip(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        task = _FakeTask("task-001")
        task.checkpoint_file = str(tmp_path / "task-001.json")
        mgr.save(task, full_state=True)

        loaded, _ = mgr.load("task-001")
        assert loaded is not None
        assert loaded.id == "task-001"
        assert loaded.result == {"writer": {"content": "hello"}}

    def test_save_invalid_task_id_raises(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        task = _FakeTask("../../etc/passwd")
        with pytest.raises(ValueError, match="无效的 task_id"):
            mgr.save(task)

    def test_load_missing_checkpoint_returns_none(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        loaded, _ = mgr.load("nonexistent")
        assert loaded is None

    def test_load_corrupt_checkpoint_returns_none(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json at all", encoding="utf-8")
        mgr = CheckpointManager(str(tmp_path))
        # 用有效 id 写文件
        task = _FakeTask("bad")
        task.checkpoint_file = str(f)
        mgr.save(task)
        # 覆盖为损坏内容
        f.write_text("{corrupt", encoding="utf-8")
        loaded, _ = mgr.load("bad")
        assert loaded is None

    def test_save_atomic_write_no_tmp_leftover(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        task = _FakeTask("atomic-001")
        task.checkpoint_file = str(tmp_path / "atomic-001.json")
        mgr.save(task)
        # 不应残留 .tmp 文件
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_remove_checkpoint(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        task = _FakeTask("to-remove")
        task.checkpoint_file = str(tmp_path / "to-remove.json")
        mgr.save(task)
        mgr.remove("to-remove")
        assert not (tmp_path / "to-remove.json").exists()

    def test_cleanup_old_files(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        # 写一个过期文件
        old = tmp_path / "old.json"
        old.write_text("{}", encoding="utf-8")
        old_time = time.time() - 10 * 86400
        import os
        os.utime(old, (old_time, old_time))
        mgr.cleanup_old(max_age_days=7)
        assert not old.exists()

"""VersionManager 原子索引写入、损坏安全模式与回滚一致性测试"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.version_manager import VersionIndexCorrupted, VersionManager


@pytest.fixture()
def vm(tmp_path: Path) -> VersionManager:
    return VersionManager(versions_dir=str(tmp_path / "versions"))


class TestAtomicIndex:
    def test_commit_roundtrip(self, vm: VersionManager, tmp_path: Path):
        fp = str(tmp_path / "doc.md")
        v1 = vm.commit(fp, "hello")
        v2 = vm.commit(fp, "world")
        assert v1["version"] == 1
        assert v2["version"] == 2
        assert vm.get_content(fp, 1) == "hello"
        assert vm.get_content(fp, 2) == "world"

    def test_no_temp_files_left_after_save(self, vm: VersionManager, tmp_path: Path):
        fp = str(tmp_path / "doc.md")
        vm.commit(fp, "hello")
        leftovers = [
            p for p in vm._index_path(fp).parent.iterdir() if p.name.endswith(".tmp")
        ]
        assert leftovers == []


class TestCorruptedIndexSafeMode:
    def test_load_index_raises_and_preserves_corrupt_file(
        self, vm: VersionManager, tmp_path: Path
    ):
        fp = str(tmp_path / "doc.md")
        vm.commit(fp, "v1 content")
        idx_path = vm._index_path(fp)
        idx_path.write_text('{"truncated...', encoding="utf-8")
        with pytest.raises(VersionIndexCorrupted):
            vm.history(fp)
        corrupt = [
            p for p in idx_path.parent.iterdir() if ".corrupt-" in p.name
        ]
        assert len(corrupt) == 1

    def test_commit_in_safe_mode_never_deletes_old_versions(
        self, vm: VersionManager, tmp_path: Path
    ):
        fp = str(tmp_path / "doc.md")
        e1 = vm.commit(fp, "v1 content")
        e2 = vm.commit(fp, "v2 content")
        idx_path = vm._index_path(fp)
        idx_path.write_text("{bad json", encoding="utf-8")

        entry = vm.commit(fp, "v3 content")

        assert Path(e1["content_path"]).read_text(encoding="utf-8") == "v1 content"
        assert Path(e2["content_path"]).read_text(encoding="utf-8") == "v2 content"
        assert entry["version"] > e2["version"]
        corrupt = [p for p in idx_path.parent.iterdir() if ".corrupt-" in p.name]
        assert corrupt

    def test_safe_mode_skips_cleanup_beyond_max_versions(self, tmp_path: Path):
        vm_small = VersionManager(versions_dir=str(tmp_path / "vs"), max_versions=1)
        fp = str(tmp_path / "doc.md")
        vm_small.commit(fp, "A")
        vm_small.commit(fp, "B")
        idx_path = vm_small._index_path(fp)
        assert not (idx_path.parent / "v1.md").exists()
        idx_path.write_text("broken", encoding="utf-8")

        vm_small.commit(fp, "C")

        remaining = sorted(p.name for p in idx_path.parent.glob("v*.md"))
        assert "v2.md" in remaining

    def test_safe_mode_does_not_overwrite_existing_disk_versions(
        self, vm: VersionManager, tmp_path: Path
    ):
        fp = str(tmp_path / "doc.md")
        e1 = vm.commit(fp, "v1 content")
        e2 = vm.commit(fp, "v2 content")
        idx_path = vm._index_path(fp)
        idx_path.write_text("{", encoding="utf-8")

        entry = vm.commit(fp, "brand new")

        assert entry["version"] >= 3
        assert Path(e1["content_path"]).read_text(encoding="utf-8") == "v1 content"
        assert Path(e2["content_path"]).read_text(encoding="utf-8") == "v2 content"


class TestRollback:
    def test_rollback_writes_target_atomically(
        self, vm: VersionManager, tmp_path: Path
    ):
        fp = str(tmp_path / "doc.md")
        target = Path(fp)
        target.write_text("current", encoding="utf-8")
        vm.commit(fp, "old version")
        r = vm.rollback(fp, 1)
        assert r["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "old version"
        leftovers = [p for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_rolled_back_current_content_can_be_restored(
        self, vm: VersionManager, tmp_path: Path
    ):
        """rollback 后，回滚前的当前内容必须仍可再次 rollback 回来"""
        fp = str(tmp_path / "doc.md")
        target = Path(fp)
        vm.commit(fp, "A")
        vm.commit(fp, "B")
        target.write_text("B", encoding="utf-8")

        r1 = vm.rollback(fp, 1)
        assert r1["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "A"

        backup_version = r1["new_version"] - 1
        assert vm.get_content(fp, backup_version) == "B"

        r2 = vm.rollback(fp, backup_version)
        assert r2["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "B"

    def test_rollback_missing_version_returns_error(
        self, vm: VersionManager, tmp_path: Path
    ):
        fp = str(tmp_path / "doc.md")
        vm.commit(fp, "only")
        r = vm.rollback(fp, 99)
        assert r["status"] == "error"

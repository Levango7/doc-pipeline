"""SafeWriter — 安全写入

测试原则：
  - 使用 tmp_path 隔离测试文件
  - 每个测试方法聚焦一个行为
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.safe_writer import (
    MANIFEST_FILE,
    cleanup_tiered,
    diff_preview,
    file_checksum,
    file_info,
    load_manifest,
    now_ts,
    safe_write,
    save_manifest,
)

# ─── 工具函数 ────────────────────────────

class TestUtilityFunctions:
    """工具函数"""

    def test_now_ts_format(self):
        """now_ts 返回 YYYYMMDD_HHMMSS 格式"""
        ts = now_ts()
        assert len(ts) == 15
        assert ts[8] == "_"

    def test_file_info_nonexistent(self):
        """不存在文件返回空信息"""
        info = file_info("/nonexistent/file.txt")
        assert info["exists"] is False
        assert info["size"] == 0
        assert info["lines"] == 0

    def test_file_info_existing(self, tmp_path):
        """存在文件返回正确信息"""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3")
        info = file_info(str(f))
        assert info["exists"] is True
        # size 取决于平台换行符（Windows 可能 \r\n）
        assert info["size"] > 0
        assert info["lines"] == 3
        assert len(info["sha256"]) == 64

    def test_file_checksum_nonexistent(self):
        """不存在文件返回 None"""
        assert file_checksum("/nonexistent") is None

    def test_file_checksum_existing(self, tmp_path):
        """存在文件返回 SHA256"""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        chk = file_checksum(str(f))
        assert len(chk) == 64


# ─── diff_preview ────────────────────────────

class TestDiffPreview:
    """diff 预览"""

    def test_no_diff(self):
        """相同内容无差异"""
        result = diff_preview("same", "same")
        assert "无差异" in result

    def test_addition(self):
        """新增行"""
        old = "line1\nline2"
        new = "line1\nline2\nline3"
        result = diff_preview(old, new)
        assert "+" in result
        assert "+1" in result  # 1 行新增

    def test_deletion(self):
        """删除行"""
        old = "line1\nline2\nline3"
        new = "line1\nline2"
        result = diff_preview(old, new)
        assert "-" in result
        assert "-1" in result

    def test_truncation(self):
        """大量差异截断"""
        old = "\n".join([f"old{i}" for i in range(100)])
        new = "\n".join([f"new{i}" for i in range(100)])
        result = diff_preview(old, new, max_lines=10)
        assert "省略" in result


# ─── Manifest 管理 ────────────────────────────

class TestManifest:
    """manifest 加载/保存"""

    def test_load_nonexistent_manifest(self):
        """不存在 manifest 返回默认值"""
        m = load_manifest("/nonexistent/manifest.json")
        assert m["version"] == "4.0"
        assert m["files"] == {}
        assert m["checksum"] is None

    def test_save_and_load_manifest(self, tmp_path):
        """保存后加载一致"""
        path = str(tmp_path / "manifest.json")
        data = {"version": "4.0", "files": {"f1": {"backups": []}}}
        save_manifest(path, data, backup=False)
        loaded = load_manifest(path)
        assert loaded["files"]["f1"]["backups"] == []

    def test_corrupted_manifest_returns_default(self, tmp_path):
        """损坏 manifest 返回默认值"""
        path = str(tmp_path / "manifest.json")
        with open(path, "w") as f:
            f.write("not json")
        m = load_manifest(path)
        assert m["version"] == "4.0"


# ─── safe_write 核心功能 ────────────────────────────

class TestSafeWrite:
    """safe_write 核心写入"""

    def test_write_new_file(self, tmp_path):
        """写入新文件"""
        target = tmp_path / "new.txt"
        result = safe_write(
            str(target), "hello world",
            backup_dir=str(tmp_path / "backups"),
            show_diff=False,
        )
        assert result["status"] == "ok"
        assert target.read_text() == "hello world"

    def test_write_existing_file_creates_backup(self, tmp_path):
        """覆盖已有文件时创建备份"""
        target = tmp_path / "existing.txt"
        target.write_text("old content")
        result = safe_write(
            str(target), "new content",
            backup_dir=str(tmp_path / "backups"),
            show_diff=False,
        )
        assert result["status"] == "ok"
        assert target.read_text() == "new content"
        assert result["backup"] is not None

    def test_dry_run_does_not_write(self, tmp_path):
        """dry_run 不实际写入"""
        target = tmp_path / "dry.txt"
        target.write_text("original")
        result = safe_write(
            str(target), "modified",
            backup_dir=str(tmp_path / "backups"),
            dry_run=True,
            show_diff=False,
        )
        assert result["status"] == "ok"
        assert result["dry_run"] is True
        assert target.read_text() == "original"

    def test_empty_content_rejected(self, tmp_path):
        """空内容被拒绝"""
        target = tmp_path / "empty.txt"
        result = safe_write(
            str(target), "",
            backup_dir=str(tmp_path / "backups"),
            show_diff=False,
        )
        assert result["status"] == "error"
        assert "P0" in result["issues"][0]

    def test_shrink_over_50_percent_rejected(self, tmp_path):
        """缩减超 50% 被拒绝"""
        target = tmp_path / "shrink.txt"
        target.write_text("x" * 1000)
        result = safe_write(
            str(target), "short",
            backup_dir=str(tmp_path / "backups"),
            show_diff=False,
        )
        assert result["status"] == "error"
        assert any("P1" in i for i in result["issues"])

    def test_newline_lf(self, tmp_path):
        """LF 换行符"""
        target = tmp_path / "lf.txt"
        result = safe_write(
            str(target), "line1\r\nline2",
            backup_dir=str(tmp_path / "backups"),
            newline="lf",
            show_diff=False,
        )
        assert result["status"] == "ok"
        assert target.read_bytes() == b"line1\nline2"

    def test_newline_crlf(self, tmp_path):
        """CRLF 换行符"""
        target = tmp_path / "crlf.txt"
        result = safe_write(
            str(target), "line1\nline2",
            backup_dir=str(tmp_path / "backups"),
            newline="crlf",
            show_diff=False,
        )
        assert result["status"] == "ok"
        assert target.read_bytes() == b"line1\r\nline2"


# ─── cleanup_tiered 分级清理 ────────────────────────────

class TestCleanupTiered:
    """分级清理备份"""

    def test_no_manifest_returns_zero(self, tmp_path):
        """无 manifest 返回 0 删除"""
        result = cleanup_tiered(str(tmp_path / "backups"))
        assert result["deleted"] == 0

    def test_cleanup_old_backups(self, tmp_path):
        """清理过期备份"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        manifest_path = str(backup_dir / MANIFEST_FILE)

        # 创建一个 100 天前的备份
        old_backup = backup_dir / "old.txt"
        old_backup.write_text("old")
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=100)).isoformat()

        data = {
            "version": "4.0",
            "files": {
                "target.txt": {
                    "backups": [{
                        "path": str(old_backup),
                        "timestamp": old_ts,
                        "size": 3, "lines": 1, "sha256": "",
                        "reason": "test", "agent": "test",
                    }],
                    "latest": None,
                }
            },
        }
        save_manifest(manifest_path, data, backup=False)

        result = cleanup_tiered(str(backup_dir))
        assert result["deleted"] == 1
        assert not old_backup.exists()

    def test_preserves_recent_backups(self, tmp_path):
        """保留近期备份"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        manifest_path = str(backup_dir / MANIFEST_FILE)

        # 创建一个 5 天前的备份（应在 TIER_FULL=30 天内保留）
        recent_backup = backup_dir / "recent.txt"
        recent_backup.write_text("recent")
        recent_ts = (datetime.datetime.now() - datetime.timedelta(days=5)).isoformat()

        data = {
            "version": "4.0",
            "files": {
                "target.txt": {
                    "backups": [{
                        "path": str(recent_backup),
                        "timestamp": recent_ts,
                        "size": 6, "lines": 1, "sha256": "",
                        "reason": "test", "agent": "test",
                    }],
                    "latest": None,
                }
            },
        }
        save_manifest(manifest_path, data, backup=False)

        result = cleanup_tiered(str(backup_dir))
        assert result["deleted"] == 0
        assert recent_backup.exists()

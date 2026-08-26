"""SafeWriter Agent 回归：payload 隔离、manifest .bak 恢复、清理范围限定"""
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.safe_writer_agent import SafeWriterAgent
from pipeline_core.base_agent import AgentMeta
from pipeline_core.message_bus_v3 import Message, MessageType
from pipeline_core.version_manager import VersionManager


def _make_agent(tmp_path: Path, **extra) -> SafeWriterAgent:
    config = {
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "quiet": True,
        "backup_dir": str(tmp_path / "backups"),
    }
    config.update(extra)
    meta = AgentMeta(name="safe_writer", version="2.0")
    return SafeWriterAgent("safe_writer", meta, config, None, None)


def _msg(payload: dict) -> Message:
    return Message(topic="writer.done", payload=payload, msg_type=MessageType.REQUEST)


# ─── payload 隔离（并发消息不串档） ────────────────────────────

class TestPayloadIsolation:

    def test_quality_score_isolated_between_interleaved_messages(self, tmp_path, monkeypatch):
        """两消息并发交错处理时，quality_score 写进各自文档的版本元数据"""
        vm = VersionManager(versions_dir=str(tmp_path / "versions"))
        monkeypatch.setattr("pipeline_core.version_manager.get_version_manager", lambda: vm)
        agent = _make_agent(tmp_path)
        assert not hasattr(agent, "_current_payload")

        file_a = tmp_path / "doc_a.md"
        file_b = tmp_path / "doc_b.md"
        file_a.write_text("AAA original", encoding="utf-8")
        file_b.write_text("BBB original", encoding="utf-8")

        barrier = threading.Barrier(2)
        results = {}

        def run(tag: str, score: float):
            msg = _msg({
                "task_id": f"task-{tag}",
                "content": f"{tag} new content",
                "target_file": str(tmp_path / f"doc_{tag}.md"),
                "quality_score": score,
            })
            barrier.wait()
            results[tag] = agent.handle(msg)

        t1 = threading.Thread(target=run, args=("a", 91.5))
        t2 = threading.Thread(target=run, args=("b", 42.5))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["a"]["status"] == "ok"
        assert results["b"]["status"] == "ok"

        hist_a = vm.history(str(file_a))
        hist_b = vm.history(str(file_b))
        assert len(hist_a) >= 1 and len(hist_b) >= 1
        latest_a, latest_b = hist_a[0], hist_b[0]
        assert latest_a["quality_score"] == 91.5
        assert latest_a["task_id"] == "task-a"
        assert latest_b["quality_score"] == 42.5
        assert latest_b["task_id"] == "task-b"

    def test_handle_writer_done_passes_payload_score(self, tmp_path, monkeypatch):
        """handle_writer_done 路径同样显式传递 quality_score 到版本元数据"""
        vm = VersionManager(versions_dir=str(tmp_path / "versions"))
        monkeypatch.setattr("pipeline_core.version_manager.get_version_manager", lambda: vm)
        agent = _make_agent(tmp_path)
        target = tmp_path / "w.md"
        target.write_text("old", encoding="utf-8")

        agent.handle_writer_done(_msg({
            "task_id": "t-w",
            "content": "writer output",
            "target_file": str(target),
            "quality_score": 77.5,
        }))

        latest = vm.history(str(target))[0]
        assert latest["quality_score"] == 77.5
        assert latest["task_id"] == "t-w"


# ─── manifest 损坏恢复 ────────────────────────────

class TestManifestRecovery:

    def test_corrupted_manifest_recovers_from_bak(self, tmp_path):
        """manifest 损坏时从 manifest.json.bak 兜底恢复"""
        agent = _make_agent(tmp_path)
        manifest_path = Path(agent.config["backup_dir"]) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        good = {"files": {"x.md": {"backups": [{"path": "b1"}], "latest": {"path": "b1"}}},
                "version": "2.0"}
        manifest_path.write_text(json.dumps(good), encoding="utf-8")
        Path(str(manifest_path) + ".bak").write_text(json.dumps(good), encoding="utf-8")
        manifest_path.write_text("{ this is !! broken json", encoding="utf-8")

        loaded = agent._load_manifest(manifest_path)

        assert loaded.get("files") == good["files"]

    def test_corrupted_manifest_without_bak_returns_empty(self, tmp_path):
        """损坏且无 .bak 时返回空 manifest 结构，流程走全新写入"""
        agent = _make_agent(tmp_path)
        manifest_path = Path(agent.config["backup_dir"]) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("not json at all", encoding="utf-8")

        loaded = agent._load_manifest(manifest_path)

        assert loaded == {"files": {}, "version": "2.0"}

    def test_corrupted_manifest_with_corrupt_bak_returns_empty(self, tmp_path):
        """.bak 也损坏时同样返回空 manifest 结构"""
        agent = _make_agent(tmp_path)
        manifest_path = Path(agent.config["backup_dir"]) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("bad main", encoding="utf-8")
        Path(str(manifest_path) + ".bak").write_text("bad bak", encoding="utf-8")

        loaded = agent._load_manifest(manifest_path)

        assert loaded == {"files": {}, "version": "2.0"}

    def test_write_succeeds_when_manifest_corrupted_no_bak(self, tmp_path):
        """manifest 损坏且无 bak 时写入流程不再崩溃，正常完成全新写入"""
        agent = _make_agent(tmp_path)
        manifest_path = Path(agent.config["backup_dir"]) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("corrupted!!", encoding="utf-8")
        target = tmp_path / "doc.md"
        target.write_text("original", encoding="utf-8")

        result = agent.handle(_msg({
            "task_id": "t1", "content": "new content here",
            "target_file": str(target),
        }))

        assert result["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "new content here"


# ─── 清理范围限定为当前文档 ────────────────────────────

class TestCleanupScopedPerDocument:

    def test_cleanup_does_not_delete_other_documents_backups(self, tmp_path):
        """max_backups 超限只删当前文档自己的最旧备份，他人备份与 manifest 引用完好"""
        agent = _make_agent(tmp_path, max_backups=2)
        backup_dir = Path(agent.config["backup_dir"])
        backup_dir.mkdir(parents=True, exist_ok=True)
        src_a = tmp_path / "src" / "doc_a.md"
        src_b = tmp_path / "src" / "doc_b.md"
        src_a.parent.mkdir(parents=True, exist_ok=True)
        src_a.write_text("A doc", encoding="utf-8")
        src_b.write_text("B doc", encoding="utf-8")

        key_a = str(Path(str(src_a)).resolve())
        key_b = str(Path(str(src_b)).resolve())

        old_files_a = []
        for i in range(3):
            p = backup_dir / f"doc_a_2026010{i}_000000.md"
            p.write_text(f"a-backup-{i}", encoding="utf-8")
            stamp = 1_700_000_000 + i * 100
            os.utime(p, (stamp, stamp))
            old_files_a.append(p)
        files_b = []
        for i in range(2):
            p = backup_dir / f"doc_b_2026010{i}_000000.md"
            p.write_text(f"b-backup-{i}", encoding="utf-8")
            stamp = 1_700_000_000 + i * 100
            os.utime(p, (stamp, stamp))
            files_b.append(p)

        def entries(paths):
            return [{"path": str(p), "timestamp": "2026-01-01T00:00:00",
                     "size": 1, "reason": "seed", "agent": "test", "task_id": ""} for p in paths]

        man = {
            "version": "2.0",
            "files": {
                key_a: {"backups": entries(old_files_a), "latest": entries(old_files_a)[-1]},
                key_b: {"backups": entries(files_b), "latest": entries(files_b)[-1]},
            },
        }
        manifest_path = backup_dir / "manifest.json"
        agent._save_manifest(manifest_path, man)

        result = agent.handle(_msg({
            "task_id": "t-a", "content": "A updated",
            "target_file": str(src_a),
        }))
        assert result["status"] == "ok"

        backups_a = sorted(backup_dir.glob("doc_a_*"))
        sorted(backup_dir.glob("doc_b_*"))
        assert len(backups_a) <= 3
        kept_names = {p.name for p in backups_a}
        assert "doc_a_20260100_000000.md" not in kept_names
        for p in files_b:
            assert p.exists(), f"他人备份被误删: {p.name}"

        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(loaded["files"][key_a]["backups"]) == len(backups_a)
        assert len(loaded["files"][key_b]["backups"]) == 2
        for entry in loaded["files"][key_b]["backups"]:
            assert Path(entry["path"]).exists()

    def test_cleanup_prunes_dangling_references(self, tmp_path):
        """被清理/丢失的备份条目从 manifest 中同步移除，不留悬空引用"""
        agent = _make_agent(tmp_path, max_backups=10)
        backup_dir = Path(agent.config["backup_dir"])
        backup_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = backup_dir / "manifest.json"
        key = "F:\\nonexistent\\ghost.md"
        ghost_entries = [
            {"path": str(backup_dir / "gone_1.md"), "timestamp": "2026-01-01T00:00:00",
             "size": 1, "reason": "r", "agent": "t", "task_id": ""},
            {"path": str(backup_dir / "gone_2.md"), "timestamp": "2026-01-02T00:00:00",
             "size": 1, "reason": "r", "agent": "t", "task_id": ""},
        ]
        man = {"version": "2.0",
               "files": {key: {"backups": list(ghost_entries), "latest": ghost_entries[-1]}}}
        agent._save_manifest(manifest_path, man)

        agent._cleanup(str(backup_dir), manifest_path, key)

        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert loaded["files"][key]["backups"] == []
        assert loaded["files"][key]["latest"] is None

    def test_cleanup_ttl_only_touches_own_document(self, tmp_path):
        """TTL 过期清理同样只作用于当前文档的备份列表"""
        agent = _make_agent(tmp_path, backup_ttl_days=7)
        backup_dir = Path(agent.config["backup_dir"])
        backup_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = backup_dir / "manifest.json"

        stale_b = backup_dir / "doc_b_stale.md"
        stale_b.write_text("stale b", encoding="utf-8")
        old_stamp = 1_000_000_000
        os.utime(stale_b, (old_stamp, old_stamp))

        own_old = backup_dir / "doc_a_old.md"
        own_old.write_text("own old", encoding="utf-8")
        os.utime(own_old, (old_stamp, old_stamp))

        key_b = "X:\\other\\doc_b.md"
        key_a = "X:\\mine\\doc_a.md"
        man = {
            "version": "2.0",
            "files": {
                key_b: {"backups": [{"path": str(stale_b), "timestamp": "2001-09-09T01:46:40"}],
                        "latest": None},
                key_a: {"backups": [{"path": str(own_old), "timestamp": "2001-09-09T01:46:40"}],
                        "latest": None},
            },
        }
        agent._save_manifest(manifest_path, man)

        agent._cleanup(str(backup_dir), manifest_path, key_a)

        assert stale_b.exists()
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(loaded["files"][key_b]["backups"]) == 1
        assert loaded["files"][key_a]["backups"] == []

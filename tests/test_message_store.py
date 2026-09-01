"""tests/test_message_store.py — PersistentStore SQLite CRUD + WAL + 并发。"""
import threading

from pipeline_core.message_store import Message, MessageMetrics, PersistentStore


def _make_msg(msg_id="m1", topic="test.topic"):
    return Message(msg_id=msg_id, topic=topic, payload={"k": "v"}, from_agent="a", to_agent="b")


class TestPersistentStore:
    def test_save_and_count(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        store.save_message(_make_msg())
        h = store.health()
        assert h["status"] == "ok"
        assert h["messages"] == 1

    def test_mark_delivered(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        store.save_message(_make_msg("m1"))
        store.mark_delivered("m1")
        assert store.count_undelivered_events() == 0

    def test_mark_delivered_batch(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        for i in range(5):
            store.save_message(_make_msg(f"m{i}"))
        store.mark_delivered_batch(["m0", "m1", "m2"])
        assert store.count_undelivered_events() == 2

    def test_idempotent_save(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        store.save_message(_make_msg("dup"))
        store.save_message(_make_msg("dup"))  # INSERT OR REPLACE
        assert store.health()["messages"] == 1

    def test_move_to_dlq(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        msg = _make_msg("fail-1")
        store.save_message(msg)
        store.move_to_dlq(msg, "timeout")
        dlq = store.list_dlq()
        assert len(dlq) == 1
        assert dlq[0]["error"] == "timeout"

    def test_replay_dlq_increments_count(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        msg = _make_msg("replay-1")
        store.save_message(msg)
        store.move_to_dlq(msg, "err")
        dlq = store.list_dlq()
        row = store.replay_dlq(dlq[0]["id"])
        assert row["replay_count"] == 1
        row2 = store.replay_dlq(dlq[0]["id"])
        assert row2["replay_count"] == 2

    def test_wal_mode_enabled(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        conn = store._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_concurrent_writes(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        errors = []

        def _write(i):
            try:
                store.save_message(_make_msg(f"c{i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert store.health()["messages"] == 20

    def test_health_returns_status(self, tmp_path):
        store = PersistentStore(str(tmp_path / "test.db"))
        h = store.health()
        assert h["status"] == "ok"
        assert "messages" in h


class TestMessageMetrics:
    def test_record_and_snapshot(self):
        m = MessageMetrics()
        m.record_sent()
        m.record_sent()
        m.record_failed()
        d = m.to_dict()
        assert d["sent"] == 2
        assert d["failed"] == 1
        assert d["dlq"] == 0

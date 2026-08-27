"""QualityGate 共享单例并发隔离回归

重构前 handle() 会把 profile 配置写回实例属性（self._weights/_threshold/
self._profile_name 等）：两个携带不同 profile 的并发请求会互相覆盖配置，
一个请求可能用上另一个请求的阈值/权重。
重构后配置按请求解析为只读快照（_resolve_run_cfg），实例状态不再被修改。
本文件守护该不变量。
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.quality_gate import QualityGateAgent  # noqa: E402
from pipeline_core.base_agent import AgentMeta  # noqa: E402
from pipeline_core.message_bus_v3 import Message, MessageType  # noqa: E402

# 非完美文档（缺目录/参考资料等必需章节，completeness 必然 < 100）
_CONTENT = "# 标题\n\n这是一段正文内容，长度足够通过基础检查，但结构并不完整。\n"


def _make_gate(tmp_path: Path) -> QualityGateAgent:
    config = {
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "quiet": True,
    }
    return QualityGateAgent("quality_gate",
                            AgentMeta(name="quality_gate", version="2.0"),
                            config, None, None)


def _msg(task_id: str, cfg: dict | None = None) -> Message:
    payload = {"task_id": task_id, "content": _CONTENT}
    if cfg:
        payload["config"] = cfg
    return Message(topic="quality_gate.check", payload=payload,
                   msg_type=MessageType.REQUEST)


class TestConcurrentIsolation:
    """并发 handle() 不得互相污染配置"""

    def test_concurrent_threshold_overrides_isolated(self, tmp_path):
        """一半请求 threshold=0（必过）、一半 threshold=100（必败），
        每个结果必须对应自己的阈值（旧实现会因共享 self._threshold 串味）"""
        gate = _make_gate(tmp_path)
        n_each = 16
        results: list[tuple[int, dict]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_each * 2)
        errors: list[Exception] = []

        def worker(idx: int, threshold: float):
            try:
                barrier.wait()
                result = gate.handle(_msg(f"t-{idx}", {"threshold": threshold}))
                with results_lock:
                    results.append((idx, threshold, result))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for i in range(n_each):
            threads.append(threading.Thread(target=worker, args=(i, 0)))
            threads.append(threading.Thread(target=worker, args=(i + n_each, 100)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发 handle() 抛异常: {errors}"
        assert len(results) == n_each * 2
        for idx, threshold, result in results:
            if threshold == 0:
                assert result["status"] == "pass", \
                    f"threshold=0 的请求 #{idx} 必须通过，实际: {result['status']}"
            else:
                assert result["status"] == "fail", \
                    f"threshold=100 的请求 #{idx} 必须失败，实际: {result['status']}"

    def test_concurrent_profile_switch_isolated(self, tmp_path):
        """一半请求切到 tutorial profile、一半用默认 technical-doc，
        每个结果的 profile 字段必须对应自己的请求"""
        gate = _make_gate(tmp_path)
        n_each = 12
        results: list[tuple[str, dict]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(n_each * 2)
        errors: list[Exception] = []

        def worker(idx: int, use_tutorial: bool):
            try:
                barrier.wait()
                cfg = {"quality_profile": "tutorial"} if use_tutorial else None
                result = gate.handle(_msg(f"p-{idx}", cfg))
                with results_lock:
                    results.append(("tutorial" if use_tutorial else "technical-doc",
                                    result))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for i in range(n_each):
            threads.append(threading.Thread(target=worker, args=(i, True)))
            threads.append(threading.Thread(target=worker, args=(i + n_each, False)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发 handle() 抛异常: {errors}"
        assert len(results) == n_each * 2
        for expected, result in results:
            assert result["profile"] == expected, \
                f"期望 profile={expected}，实际 {result['profile']}"

    def test_instance_state_not_mutated_after_profile_switch(self, tmp_path):
        """切换到 tutorial 评估后，实例默认配置保持不变（旧实现会永久污染）"""
        gate = _make_gate(tmp_path)
        assert gate._profile_name == "technical-doc"
        assert gate._default_run_cfg.threshold == 70

        switched = gate.handle(_msg("s1", {"quality_profile": "tutorial"}))
        assert switched["profile"] == "tutorial"

        # 实例态不变
        assert gate._profile_name == "technical-doc"
        assert gate._threshold == 70
        assert gate._default_run_cfg.profile_name == "technical-doc"

        # 后续无 config 请求仍走默认 profile
        default = gate.handle(_msg("s2"))
        assert default["profile"] == "technical-doc"

    def test_run_config_overrides_same_profile(self, tmp_path):
        """同 profile 下 run_config 覆盖项生效（threshold 叠加在初始化配置之上）"""
        gate = _make_gate(tmp_path)
        passed = gate.handle(_msg("o1", {"threshold": 0}))
        failed = gate.handle(_msg("o2", {"threshold": 100}))
        assert passed["status"] == "pass"
        assert failed["status"] == "fail"
        # 默认请求不受上面覆盖影响
        default = gate.handle(_msg("o3"))
        assert default["profile"] == "technical-doc"

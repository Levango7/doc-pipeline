"""P2 并发压测：背压验证 + 死锁检测 + 并发安全 + 多任务编排

覆盖四个场景：
1. 总线 EVENT 洪水 → 背压激活，无 OOM
2. 总线 REQUEST 并发 → 50 并发全部响应
3. SQLite 多线程读写 → 无数据损坏
4. 编排器 5 个任务并发 → 全部完成
"""
import sys, os, time, threading, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.message_bus_v3 import MessageBus


# ════════════════════════════════════════════════
# P2-A: 总线 EVENT 背压
# ════════════════════════════════════════════════

class TestBusBackpressure:

    def test_backpressure_activates_under_heavy_load(self):
        """600 EVENT 快速发布 → 背压激活，消息跳过"""
        bus = MessageBus(
            db_path=os.path.join(tempfile.mkdtemp(), "bp.db"),
            backpressure_watermark=50,
        )
        received = []
        def slow(msg):
            time.sleep(0.05)
            received.append(msg)
        bus.subscribe("load.t", slow)

        ok = busy = 0
        for i in range(600):
            r = bus.publish("load.t", "test", {"seq": i})
            if r.get("status") == "sent":
                ok += 1
            elif r.get("status") == "busy":
                busy += 1

        time.sleep(2)
        final = len(received)
        print(f"\n  [背压] sent={ok} busy={busy} delivered={final}")
        assert busy > 0, "背压应触发"
        assert bus._worker_running, "worker 运行中"
        bus._shutdown_event.set()

    def test_burst_does_not_crash(self):
        """突发 200 条 → 系统不崩溃"""
        bus = MessageBus(
            db_path=os.path.join(tempfile.mkdtemp(), "burst.db"),
            backpressure_watermark=100,
        )
        results = []
        def fast(msg):
            results.append(msg.payload.get("seq"))
        bus.subscribe("burst.t", fast)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(
                lambda i=i: bus.publish("burst.t", "test", {"seq": i}), i
            ) for i in range(200)]
            for f in as_completed(futures):
                f.result()

        time.sleep(1)
        depth = bus.queue_depth()
        print(f"\n  [burst] depth after 1s = {depth}")
        assert depth < 200, f"队列深度应降低：{depth}"
        assert len(results) > 0, "消息应被投递"
        bus._shutdown_event.set()


# ════════════════════════════════════════════════
# P2-B: REQUEST 并发
# ════════════════════════════════════════════════

class TestRequestConcurrency:

    def test_50_concurrent_requests(self):
        """50 并发 bus.request → 全部响应"""
        bus = MessageBus(enable_persistence=False)
        counter = {"calls": 0}
        def echo(msg):
            counter["calls"] += 1
            return {"echo": msg.payload.get("seq"), "ok": True}
        bus.subscribe("echo.input", echo)

        n = 50
        results = [None] * n
        def req(seq):
            try:
                return seq, bus.request("echo.input", "t", "echo", {"seq": seq})
            except Exception as e:
                return seq, {"error": str(e)}

        with ThreadPoolExecutor(max_workers=20) as pool:
            for f in as_completed([pool.submit(req, i) for i in range(n)]):
                seq, r = f.result()
                results[seq] = r

        ok = sum(1 for r in results if r and r.get("ok"))
        print(f"\n  [并发] {n} req: {ok} ok")
        assert ok == n, f"应全部成功：{ok}/{n}"
        assert counter["calls"] == n
        bus._shutdown_event.set()

    def test_request_chain_no_deadlock(self):
        """A→B→C request 链 → 无死锁"""
        bus = MessageBus(enable_persistence=False)
        def a(msg):
            br = bus.request("b.input", "a", "b", msg.payload)
            return {"a_ok": True, "b_res": br}
        def b(msg):
            cr = bus.request("c.input", "b", "c", {"seq": msg.payload.get("seed", 0)})
            return {"b_ok": True, "c_res": cr}
        def c(msg):
            return {"c_ok": True, "seq": msg.payload.get("seq")}
        bus.subscribe("a.input", a)
        bus.subscribe("b.input", b)
        bus.subscribe("c.input", c)

        r = bus.request("a.input", "t", "a", {"seed": 42})
        assert r is not None
        assert r["b_res"]["c_res"]["seq"] == 42
        bus._shutdown_event.set()


# ════════════════════════════════════════════════
# P2-C: Store 并发安全
# ════════════════════════════════════════════════

class TestStoreConcurrency:

    def test_20_threads_stress(self):
        """20 线程并发读写 store → 0 异常"""
        bus = MessageBus(
            db_path=os.path.join(tempfile.mkdtemp(), "store_conc.db"),
            backpressure_watermark=1000,
        )
        store = bus._store
        errors = []
        elock = threading.Lock()
        n_ops = 50

        def worker(wid):
            for i in range(n_ops):
                try:
                    bus.publish("stress.t", "t", {"w": wid, "s": i})
                    time.sleep(0.001)
                    if i % 5 == 0:
                        bus.list_dlq()
                except Exception as e:
                    with elock:
                        errors.append((wid, i, str(e)))

        threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        time.sleep(1)

        stats = store.health()
        print(f"\n  [Store] 20t×{n_ops}ops err={len(errors)} "
              f"msgs+dlq={stats.get('dlq',0)+stats.get('messages',0)}")
        assert len(errors) == 0, f"异常：{errors[:3]}"
        assert bus._worker_running
        bus._shutdown_event.set()

    def test_no_massive_loss(self):
        """并发发布 → 丢失率 < 20%"""
        bus = MessageBus(db_path=os.path.join(tempfile.mkdtemp(), "loss.db"))
        received = []
        rlock = threading.Lock()
        def rec(msg):
            with rlock:
                received.append(msg.payload.get("seq"))
        bus.subscribe("loss.t", rec)

        n = 100
        with ThreadPoolExecutor(max_workers=10) as pool:
            for f in as_completed([pool.submit(lambda i=i: bus.publish("loss.t", "t", {"seq": i}), i) for i in range(n)]):
                f.result()

        time.sleep(2)
        with rlock:
            got = set(received)
        missing = n - len(got)
        print(f"\n  [不丢] sent={n} got={len(got)} missing={missing}")
        assert len(got) >= n * 0.8, f"丢失过多：{missing}/{n}"
        bus._shutdown_event.set()


# ════════════════════════════════════════════════
# P2-D: 编排器多任务并发
# ════════════════════════════════════════════════

class TestOrchestratorConcurrency:
    """多任务并行执行 → 无死锁，全部完成"""

    def test_5_concurrent_tasks(self):
        """5 个并发流水线任务 → 全部完成"""
        import tempfile
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.scheduler import ExecutionPlan
        from pipeline_core.pipeline import TaskStatus
        from pipeline_core.base_agent import BaseAgent, AgentMeta

        bus = MessageBus(enable_persistence=False)
        orch = PipelineOrchestrator(
            checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"),
        )
        orch.bus = bus

        class _Fast(BaseAgent):
            def __init__(self, name, **kw):
                super().__init__(
                    name, AgentMeta(name=name, version="1.0", description=""), {}, None, None,
                )
                self.calls = 0
            def handle(self, msg):
                self.calls += 1
                return {"status": "ok"}

        fast = _Fast("fast")
        orch.registry.register(fast.meta, fast)
        bus.subscribe("fast.input", lambda m: fast.handle(m))

        from pipeline_core.scheduler import AgentConfig, ExecutionNode

        acfg = AgentConfig(
            name="fast", config={}, circuit_breaker={},
            pool_size=1, parallelism={}, retry={},
        )
        node = ExecutionNode(
            agent_name="fast_pool_0",
            agent_config=acfg,
            dependencies=[], timeout=30,
        )
        plan = ExecutionPlan(
            plan_id="cp", pipeline_name="c",
            levels=[[node]], raw={}, node_count=1,
        )

        td = tempfile.mkdtemp()
        inf = os.path.join(td, "i.md")
        with open(inf, "w") as f:
            f.write("q")

        n = 5
        errors = []
        def run1(tid):
            try:
                t = orch.run_plan(plan, inf, task_id=f"c-{tid}")
                return tid, t.status
            except Exception as e:
                errors.append((tid, str(e)))
                return tid, None

        with ThreadPoolExecutor(max_workers=n) as pool:
            fs = [pool.submit(run1, i) for i in range(n)]
            rs = [f.result() for f in as_completed(fs)]

        ok = sum(1 for _, s in rs if s == TaskStatus.DONE)
        print(f"\n  [多任务] {n} tasks: {ok} ok, {len(errors)} err")
        assert ok == n, f"全部应完成：{ok}/{n}"
        assert not errors
        bus._shutdown_event.set()
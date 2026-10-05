"""Lockfile 版本锁定：generate/verify 往返、配置漂移、parse 自动校验、--write-lock 接线"""
import argparse
import copy
import shutil
from pathlib import Path

import pytest
import yaml

import run as run_mod
from pipeline_core.scheduler import LockfileMismatchError, Scheduler

PROJECT = Path(__file__).parent.parent

RAW = {
    "name": "lockdemo",
    "agents": [
        {"name": "a", "version": "1.0", "dependencies": [], "config": {"k": "v"}},
        {"name": "b", "version": "2.0", "dependencies": ["a"], "config": {"n": 1}},
    ],
    "topology": {"levels": [["a"], ["b"]]},
}


@pytest.fixture
def sched(tmp_path):
    return Scheduler(pipeline_dir=str(tmp_path))


@pytest.fixture
def plan(sched):
    return sched._build_plan(copy.deepcopy(RAW), "lockdemo")


def _write_yaml(pipelines_dir: Path, raw: dict, name: str = "lockdemo"):
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    with open(pipelines_dir / f"{name}.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)


class TestRoundtrip:
    def test_generate_then_verify_passes(self, sched, plan, tmp_path):
        lock = sched.generate_lockfile(plan, output_dir=str(tmp_path))
        assert Path(lock).exists()
        assert sched.verify_lockfile(plan, lock) == []

    def test_lockfile_contains_config_hash(self, sched, plan, tmp_path):
        lock = sched.generate_lockfile(plan, output_dir=str(tmp_path))
        with open(lock, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert set(data["agents"]) == {"a", "b"}
        for entry in data["agents"].values():
            assert len(entry["config_hash"]) == 12


class TestDriftDetection:
    def test_config_drift_detected(self, sched, plan, tmp_path):
        lock = sched.generate_lockfile(plan, output_dir=str(tmp_path))
        drifted = copy.deepcopy(RAW)
        drifted["agents"][1]["config"]["n"] = 999
        new_plan = sched._build_plan(drifted, "lockdemo")
        issues = sched.verify_lockfile(new_plan, lock)
        assert any("配置漂移" in i and "[b]" in i for i in issues)

    def test_version_drift_detected(self, sched, plan, tmp_path):
        lock = sched.generate_lockfile(plan, output_dir=str(tmp_path))
        drifted = copy.deepcopy(RAW)
        drifted["agents"][0]["version"] = "9.9"
        new_plan = sched._build_plan(drifted, "lockdemo")
        issues = sched.verify_lockfile(new_plan, lock)
        assert any("版本不匹配" in i and "[a]" in i for i in issues)

    def test_missing_agent_in_lock_detected(self, sched, plan, tmp_path):
        lock = sched.generate_lockfile(plan, output_dir=str(tmp_path))
        extra = copy.deepcopy(RAW)
        extra["agents"].append(
            {"name": "c", "version": "1.0", "dependencies": ["b"], "config": {}}
        )
        extra["topology"]["levels"].append(["c"])
        new_plan = sched._build_plan(extra, "lockdemo")
        issues = sched.verify_lockfile(new_plan, lock)
        assert any("[c] 不在 lockfile 中" in i for i in issues)


class TestParseAutoVerify:
    @pytest.fixture
    def project_sandbox(self, tmp_path, sched, plan):
        """pipelines 目录 + yaml + 匹配的 lockfile"""
        sched.generate_lockfile(plan, output_dir=str(tmp_path))
        _write_yaml(tmp_path, RAW)
        return tmp_path

    def test_parse_raises_on_drift(self, sched, project_sandbox):
        drifted = copy.deepcopy(RAW)
        drifted["agents"][1]["config"]["n"] = 42
        _write_yaml(project_sandbox, drifted)
        with pytest.raises(LockfileMismatchError) as exc_info:
            sched.parse("lockdemo")
        assert any("配置漂移" in i for i in exc_info.value.issues)
        assert "配置漂移" in str(exc_info.value)

    def test_parse_raises_on_version_change(self, sched, project_sandbox):
        drifted = copy.deepcopy(RAW)
        drifted["agents"][0]["version"] = "3.0"
        _write_yaml(project_sandbox, drifted)
        with pytest.raises(LockfileMismatchError) as exc_info:
            sched.parse("lockdemo")
        assert any("版本不匹配" in i for i in exc_info.value.issues)

    def test_error_carries_all_issues(self, sched, project_sandbox):
        drifted = copy.deepcopy(RAW)
        drifted["agents"][0]["version"] = "3.0"
        drifted["agents"][1]["config"]["n"] = 7
        _write_yaml(project_sandbox, drifted)
        with pytest.raises(LockfileMismatchError) as exc_info:
            sched.parse("lockdemo")
        assert len(exc_info.value.issues) >= 2
        assert exc_info.value.pipeline_name == "lockdemo"

    def test_parse_file_also_verifies(self, sched, project_sandbox):
        drifted = copy.deepcopy(RAW)
        drifted["agents"][1]["config"]["n"] = 42
        yaml_path = project_sandbox / "lockdemo.yaml"
        _write_yaml(project_sandbox, drifted)
        with pytest.raises(LockfileMismatchError):
            sched.parse_file(str(yaml_path))

    def test_parse_without_lock_stays_compatible(self, sched, tmp_path, caplog):
        _write_yaml(tmp_path, RAW)
        import logging
        with caplog.at_level(logging.DEBUG, logger="pipeline_core.scheduler"):
            plan = sched.parse("lockdemo")
        assert plan.pipeline_name == "lockdemo"
        assert plan.node_count == 2
        assert any("--write-lock" in r.getMessage() for r in caplog.records)

    def test_matching_lock_parses_cleanly(self, sched, project_sandbox):
        plan = sched.parse("lockdemo")
        assert plan.node_count == 2


class TestWriteLockWiring:
    def _args(self, **kw):
        defaults = {"pipeline_file": None, "pipeline": "docgen"}
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_write_lock_generates_and_continues(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = self._args(pipeline_file=str(PROJECT / "pipelines" / "docgen.yaml"),
                          pipeline="docgen", write_lock=True)
        plan, loaded = run_mod._resolve_pipeline_plan(args, None, {})
        assert loaded and plan is not None
        lock_file = tmp_path / "pipelines" / "docgen.lock"
        assert lock_file.exists()
        content = lock_file.read_text(encoding="utf-8")
        assert "config_hash" in content
        # 质量尾是内联进来的节点，锁里记的是带别名的身份（改片段会让父锁漂移）
        assert "safe_writer__quality_tail" in content

    def test_drift_blocks_execution_with_clear_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        src = (PROJECT / "pipelines" / "docgen.yaml").read_text(encoding="utf-8")
        drifted = src.replace("prompt_profile: generic-tech", "prompt_profile: drifted-tech")
        target = tmp_path / "drift.yaml"
        target.write_text(drifted, encoding="utf-8")
        # docgen 现在 call 了 _quality-tail：片段必须和引用它的 yaml 同目录，
        # 只复制主文件会让解析停在"片段不存在"上，测不到漂移这条路径。
        shutil.copy(PROJECT / "pipelines" / "_quality-tail.yaml",
                    tmp_path / "_quality-tail.yaml")

        from pipeline_core.scheduler import Scheduler
        stale_sched = Scheduler()
        stale_plan = stale_sched.parse_file(str(target), verify_lock=False)
        stale_sched.generate_lockfile(stale_plan)
        target.write_text(
            drifted.replace("max_results: 10", "max_results: 11"), encoding="utf-8"
        )

        args = self._args(pipeline_file=str(target), pipeline="drift", write_lock=False)
        with pytest.raises(SystemExit) as exc_info:
            run_mod._resolve_pipeline_plan(args, None, {})
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "版本锁定不一致" in err
        assert "配置漂移" in err
        assert "--write-lock" in err

    def test_no_lock_loads_without_verification(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = PROJECT / "pipelines" / "three_pass.yaml"
        args = self._args(pipeline_file=str(target), pipeline="three_pass", write_lock=False)
        plan, loaded = run_mod._resolve_pipeline_plan(args, None, {})
        assert loaded and plan is not None


# ─── 出厂流水线全量覆盖 ───────────────────────────────────

class TestShippedPipelinesAreLocked:
    """每条 pipelines/*.yaml 都必须有配套 .lock 且能通过校验。

    此前 6 条流水线只有 2 条有锁，漂移护栏对另外 4 条形同虚设。
    """

    def test_every_pipeline_yaml_has_a_lock(self):
        yamls = sorted(p.stem for p in (PROJECT / "pipelines").glob("*.yaml"))
        missing = [n for n in yamls if not (PROJECT / "pipelines" / f"{n}.lock").exists()]
        assert not missing, f"以下流水线缺少 lockfile（用 --write-lock 生成）: {missing}"
        assert yamls, "pipelines/ 下没有 YAML？"

    def test_every_pipeline_verifies_with_lock_enabled(self):
        sched = Scheduler()
        bad = []
        for p in sorted((PROJECT / "pipelines").glob("*.yaml")):
            try:
                sched.parse_file(str(p))  # verify_lock 默认开启
            except Exception as e:  # noqa: BLE001
                bad.append((p.name, f"{type(e).__name__}: {e}"))
        assert not bad, f"锁校验未通过: {bad}"

    def test_lock_covers_config_not_only_topology(self, tmp_path, monkeypatch):
        """加锁后改一个 config 数值必须被拦住（防"有锁但没用"）。

        注意：Scheduler 的 pipeline_dir 默认是**相对路径** pipelines/，
        generate_lockfile 会按它落盘 —— 不在 tmp 下 chdir 就会覆盖仓库里的
        真实 lock（本测试第一版就犯过这个错，把 docreq.lock 写脏了）。
        """
        monkeypatch.chdir(tmp_path)
        sandbox = tmp_path / "pipelines"
        sandbox.mkdir()
        src = (PROJECT / "pipelines" / "docreq.yaml").read_text(encoding="utf-8")
        drifted = src.replace("max_results: 10", "max_results: 11", 1)
        if drifted == src:
            drifted = src.replace("threshold: 70", "threshold: 71", 1)
        target = sandbox / "docreq.yaml"
        target.write_text(drifted, encoding="utf-8")
        shutil.copy(PROJECT / "pipelines" / "_quality-tail.yaml",
                    sandbox / "_quality-tail.yaml")

        sched = Scheduler(pipeline_dir=str(sandbox))
        plan = sched.parse_file(str(target), verify_lock=False)
        sched.generate_lockfile(plan)
        further = (drifted.replace("max_results: 11", "max_results: 12")
                   if "max_results" in drifted
                   else drifted.replace("threshold: 71", "threshold: 72"))
        target.write_text(further, encoding="utf-8")
        with pytest.raises(LockfileMismatchError):
            sched.parse_file(str(target))
        # 仓库里的真实 lock 不许被动过
        repo_lock = (PROJECT / "pipelines" / "docreq.lock").read_bytes()
        assert b"max_results: 12" not in repo_lock


# ─── edges 与 dependencies 一致性 ─────────────────────────

class TestEdgesMatchDependencies:
    """edges 只是给人看的连线图，执行以 dependencies 为准；
    两者不一致必须报错，否则文档化的图与真实图悄悄分叉。"""

    RAW_BASE = {
        "name": "edgedemo",
        "agents": [
            {"name": "a", "version": "1.0", "dependencies": [], "config": {}},
            {"name": "b", "version": "1.0", "dependencies": ["a"], "config": {}},
        ],
        "topology": {"levels": [["a"], ["b"]], "edges": [["a", "b"]]},
    }

    def _write(self, tmp_path, mutate):
        raw = copy.deepcopy(self.RAW_BASE)
        mutate(raw)
        path = tmp_path / "edgedemo.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        return str(path)

    def test_consistent_edges_pass(self, tmp_path):
        plan = Scheduler().parse_file(self._write(tmp_path, lambda r: None),
                                     verify_lock=False)
        assert plan.node_count == 2

    def test_edge_naming_unknown_agent_is_rejected(self, tmp_path):
        """safewriter 式拼错：以前无人发现，现在解析期即报错。"""
        path = self._write(tmp_path, lambda r: r["topology"].__setitem__(
            "edges", [["a", "b"], ["b", "safewriter"]]))
        with pytest.raises(ValueError, match="未定义的 Agent"):
            Scheduler().parse_file(path, verify_lock=False)

    def test_edges_block_is_optional(self, tmp_path):
        """没写 edges 就不校验（YAML 允许只声明 levels），写了就必须一致。"""
        path = self._write(tmp_path, lambda r: r["topology"].__setitem__("edges", []))
        plan = Scheduler().parse_file(path, verify_lock=False)
        assert plan.node_count == 2

    def test_partial_edges_are_rejected(self, tmp_path):
        """声明了一部分连线就必须齐全，缺一条即报错（防图与执行分叉）。"""
        raw = copy.deepcopy(self.RAW_BASE)
        raw["agents"].append({"name": "c", "version": "1.0",
                              "dependencies": ["a"], "config": {}})
        raw["topology"]["levels"] = [["a"], ["b"], ["c"]]
        raw["topology"]["edges"] = [["a", "b"]]  # 少 a→c
        path = tmp_path / "edgedemo.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        with pytest.raises(ValueError, match="edges 缺少"):
            Scheduler().parse_file(str(path), verify_lock=False)

    def test_extra_edge_is_rejected(self, tmp_path):
        raw = copy.deepcopy(self.RAW_BASE)
        raw["agents"].append({"name": "c", "version": "1.0",
                              "dependencies": ["a"], "config": {}})
        raw["topology"]["levels"] = [["a"], ["b"], ["c"]]
        raw["topology"]["edges"] = [["a", "b"], ["a", "c"], ["b", "c"]]
        path = tmp_path / "edgedemo.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        with pytest.raises(ValueError, match="edges 多出"):
            Scheduler().parse_file(str(path), verify_lock=False)

    def test_malformed_edge_is_rejected(self, tmp_path):
        path = self._write(tmp_path, lambda r: r["topology"].__setitem__("edges", ["ab"]))
        with pytest.raises(ValueError, match="二元组"):
            Scheduler().parse_file(path, verify_lock=False)

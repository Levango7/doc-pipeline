"""Lockfile 版本锁定：generate/verify 往返、配置漂移、parse 自动校验、--write-lock 接线"""
import argparse
import copy
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
        assert "safe_writer" in content

    def test_drift_blocks_execution_with_clear_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        src = (PROJECT / "pipelines" / "docgen.yaml").read_text(encoding="utf-8")
        drifted = src.replace("prompt_profile: generic-tech", "prompt_profile: drifted-tech")
        target = tmp_path / "drift.yaml"
        target.write_text(drifted, encoding="utf-8")

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

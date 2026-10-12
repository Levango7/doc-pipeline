"""CLI 动词收敛判据 —— spec §5.3 CLI 行 / §7.3 第 7 步。

动词层：`run / plan / lock / pack / plugin`（prefix 翻译，缺省动词 = run）。
旧 flag（--plan/--dry-run/--write-lock/--list-agents/--enhance 一族）已摘除，
回归到"散装 flag"就意味着收敛被回滚。

四层判据：
  1. 翻译层：纯函数 `_split_verb` 的映射与用法报错（快，定位精确）；
  2. 摘除判据：旧 flag 经 argparse 必被拒（unrecognized；in-process + 一条真 CLI）；
  3. 门面：--help 用户可见面列出五个动词；
  4. 真 CLI 冒烟：pack list / plugin list / plan 各跑一次（含 lock 的沙盒实证）。
"""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


def _run_module():
    """取当前 sys.modules 中的 run 模块实例（test_run.py 会 del 后重导，随取随用）。"""
    return importlib.import_module("run")


# ─── 1. 翻译层（纯函数）────────────────────────────

class TestSplitVerb:
    def test_no_verb_is_implicit_run(self):
        """裸输入 = 隐式 run：既有 `run.py input.md …` 形态必须照旧。"""
        verb, action, rest = _run_module()._split_verb(["input.md", "-o", "x.md"])
        assert verb is None and action is None
        assert rest == ["input.md", "-o", "x.md"]

    def test_empty_argv(self):
        verb, action, rest = _run_module()._split_verb([])
        assert (verb, action, rest) == (None, None, [])

    def test_run_verb_strips_prefix(self):
        verb, action, rest = _run_module()._split_verb(["run", "input.md", "-q", "a"])
        assert verb == "run" and action is None
        assert rest == ["input.md", "-q", "a"]

    def test_plan_verb_takes_pipeline_name(self):
        """`loom plan docgen` 的裸词 = 流水线名（翻译为 --pipeline）。"""
        verb, action, rest = _run_module()._split_verb(["plan", "docgen"])
        assert (verb, action) == ("plan", "plan")
        assert rest == ["--pipeline", "docgen"]

    def test_plan_verb_without_name(self):
        verb, action, rest = _run_module()._split_verb(["plan"])
        assert (verb, action, rest) == ("plan", "plan", [])

    def test_plan_verb_flag_forms_untouched(self):
        verb, action, rest = _run_module()._split_verb(["plan", "--pipeline-file", "x.yaml"])
        assert (verb, action) == ("plan", "plan")
        assert rest == ["--pipeline-file", "x.yaml"]

    def test_lock_verb(self):
        verb, action, rest = _run_module()._split_verb(["lock", "docgen"])
        assert (verb, action) == ("lock", "lock")
        assert rest == ["--pipeline", "docgen"]

    @pytest.mark.parametrize(("argv", "action"), [
        (["pack", "list"], "pack_list"),
        (["pack", "check"], "pack_check"),
    ])
    def test_pack_subcommands(self, argv, action):
        verb, got, rest = _run_module()._split_verb(argv)
        assert verb == "pack" and got == action and rest == []

    def test_pack_requires_known_subcommand(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_module()._split_verb(["pack"])
        assert exc.value.code == 2
        assert "loom pack list|check" in capsys.readouterr().err

    def test_plugin_list_only(self, capsys):
        verb, action, rest = _run_module()._split_verb(["plugin", "list"])
        assert (verb, action, rest) == ("plugin", "plugin_list", [])
        with pytest.raises(SystemExit) as exc:
            _run_module()._split_verb(["plugin", "install"])
        assert exc.value.code == 2
        assert "loom plugin list" in capsys.readouterr().err


# ─── 2. 摘除判据：旧 flag 必被拒 ──────────────────

REMOVED_FLAGS = ["--plan", "--dry-run", "--write-lock", "--list-agents",
                 "--enhance", "--enhance-output", "--no-search"]


class TestRemovedFlags:
    @pytest.mark.parametrize("flag", REMOVED_FLAGS)
    def test_flag_is_unrecognized(self, flag, capsys):
        """动词收敛 = 旧散装 flag 摘除；任一回魂都要在这转红。"""
        parser = _run_module().build_arg_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([flag])
        assert exc.value.code == 2
        assert f"unrecognized arguments: {flag}" in capsys.readouterr().err

    def test_real_cli_rejects_legacy_flag(self):
        """真 CLI 端到端一条：--plan 走真实 argv 也被拒（rc=2）。"""
        proc = subprocess.run(
            [sys.executable, str(PROJECT / "run.py"), "x.md", "--plan"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 2
        assert "unrecognized arguments: --plan" in proc.stderr

    def test_internal_defaults_still_exist(self):
        """内部开关仍在（动词注入用），只是不再暴露为 flag。"""
        parser = _run_module().build_arg_parser()
        args = parser.parse_args(["x.md"])
        assert args.plan is False
        assert args.write_lock is False
        assert args.list_agents is False


# ─── 3. 门面：--help 列出动词 ─────────────────────

class TestHelpSurface:
    def test_help_lists_all_verbs(self):
        proc = subprocess.run(
            [sys.executable, str(PROJECT / "run.py"), "--help"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0
        visible = proc.stdout + proc.stderr
        for verb in ("loom run", "loom plan", "loom lock",
                     "loom pack list", "loom plugin list"):
            assert verb in visible, f"--help 缺动词: {verb}"


# ─── 4. 真 CLI 冒烟 ───────────────────────────────

class TestVerbSmoke:
    def _cli(self, argv):
        return subprocess.run(
            [sys.executable, str(PROJECT / "run.py"), *argv],
            capture_output=True, text=True, timeout=180, cwd=PROJECT)

    def test_pack_list_reports_physical_pack(self):
        proc = self._cli(["pack", "list"])
        assert proc.returncode == 0, proc.stderr[-400:]
        assert "docgen" in proc.stdout
        assert "[pack]" in proc.stdout

    def test_plugin_list_includes_pack_agents(self):
        """plugin list 列的是全清册（含 packs/docgen/agents 的文档件）。"""
        proc = self._cli(["plugin", "list"])
        assert proc.returncode == 0, proc.stderr[-400:]
        assert "writer" in proc.stdout and "quality_gate" in proc.stdout

    def test_plan_verb_previews_without_executing(self):
        proc = self._cli(["plan", "three_pass"])
        assert proc.returncode == 0, proc.stderr[-400:]
        assert "Level" in proc.stdout
        assert "总节点数" in proc.stdout

    def test_lock_verb_writes_lock_and_exits_without_running(self, tmp_path):
        """lock = 纯维护动词：只写锁文件，不执行流水线（沙盒里验证落点）。"""
        src = PROJECT / "pipelines" / "three_pass.yaml"
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        target = sandbox / "three_pass.yaml"
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        proc = self._cli(["lock", "--pipeline-file", str(target)])
        assert proc.returncode == 0, proc.stderr[-400:]
        lock = sandbox / "three_pass.lock"
        assert lock.exists(), "lock 动词没有在 YAML 同目录写出锁文件"
        assert "config_hash" in lock.read_text(encoding="utf-8")

    def test_run_verb_equals_bare_input(self):
        """两种形态进入同一个执行路径：参数解析层面断言等价。"""
        parser = _run_module().build_arg_parser()
        args_bare = parser.parse_args(["x.md", "-q", "a b"])
        _, _, rest = _run_module()._split_verb(["run", "x.md", "-q", "a b"])
        args_verb = parser.parse_args(rest)
        assert args_bare.input == args_verb.input == "x.md"
        assert args_bare.queries == args_verb.queries == ["a b"]

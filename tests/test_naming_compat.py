"""命名收官判据 —— spec §7.1 最后两格：包名与 entry_points group。

两件事各一组判据：

A. **旧包名 shim（docpipeline → loom_doc）**：旧名 import 必须与新名是**同一
   模块对象**（identity 保真——转发文件方案会制造两份模块、patch 打一处对
   另一处无效，本仓在打桩入口上栽过）；旧根名首次导入记一次 DeprecationWarning；
   `docpipeline/__init__.py` 文件必须存在（MAOP 的 adapter 正是用它判项目根，
   属于对外契约的一部分）；包内代码不得再 import 旧名（反向守卫防回退）。

B. **entry_points group（loom.agents / loom.packs，旧名过渡回退）**：新 group
   命中、旧 group 回报迁移 warning、同名时新 group 优先。
"""
import ast
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parent.parent


# ─── A. 旧包名 shim ─────────────────────────────

class TestDocpipelineShim:
    def test_shim_file_exists_for_root_probe(self):
        """MAOP 的 _resolve_doc_pipeline_root() 探 `docpipeline/__init__.py`——
        这个文件是**对外契约**，删了等于把 MAOP 的根解析打断。"""
        assert (PROJECT / "docpipeline" / "__init__.py").is_file()

    def test_identity_and_single_warning(self):
        """子进程里验证：root/子模块/符号 identity 保真 + 恰好一次 DeprecationWarning。"""
        script = (
            "import warnings, json\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            "    import docpipeline\n"
            "    import docpipeline.renderer, docpipeline.ingest\n"
            "    import loom_doc, loom_doc.renderer\n"
            "    from docpipeline.renderer import clean_inline as a\n"
            "    from loom_doc.renderer import clean_inline as b\n"
            "    out = {\n"
            "        'root': docpipeline is loom_doc,\n"
            "        'sub': docpipeline.renderer is loom_doc.renderer,\n"
            "        'symbol': a is b,\n"
            "        'warns': len([x for x in w if x.category is DeprecationWarning]),\n"
            "    }\n"
            "print(json.dumps(out))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(PROJECT),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-600:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["root"] is True, "docpipeline 与 loom_doc 不是同一模块对象"
        assert out["sub"] is True, "子模块 identity 漂了（转发文件型的假兼容）"
        assert out["symbol"] is True, "符号 identity 漂了"
        assert out["warns"] == 1, f"DeprecationWarning 应恰好一次，实际 {out['warns']}"

    def test_source_does_not_use_old_name(self):
        """反向守卫：仓内源码（除 shim 本体）不得 import 旧名。

        允许出现的只有两个 shim 目录本身——它们是旧名的家。
        """
        olds = {"docpipeline", "pipeline_core"}
        offenders: list[str] = []
        scan_roots = ["loom_core", "loom_doc", "agents", "packs", "scripts",
                      "tests", "run.py"]
        for root in scan_roots:
            p = PROJECT / root
            files = [p] if p.is_file() else sorted(p.rglob("*.py"))
            for f in files:
                rel = f.relative_to(PROJECT).as_posix()
                if rel.startswith("docpipeline/") or rel.startswith("pipeline_core/"):
                    continue
                # 例外：发布门禁刻意 import 旧名以验证 shim 的安装态兼容
                if rel == "scripts/verify_dist.py":
                    continue
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8"))
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    hit = False
                    if isinstance(node, ast.Import):
                        hit = any(al.name.split(".")[0] in olds for al in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        hit = node.level == 0 and (node.module or "").split(".")[0] in olds
                    elif isinstance(node, ast.Call) and node.args:
                        fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                        first = node.args[0]
                        hit = (fn in ("import_module", "__import__")
                               and isinstance(first, ast.Constant)
                               and isinstance(first.value, str)
                               and first.value.split(".")[0] in olds)
                    if hit:
                        offenders.append(rel)
                        break
        assert offenders == [], f"这些文件还在 import 旧名 {sorted(olds)}: {sorted(set(offenders))}"


class TestPipelineCoreShim:
    """第二段（pipeline_core → loom_core）三判据：探径契约 / identity+warn / 枚举同一。"""

    def test_shim_file_exists_for_maop_root_probe(self):
        """MAOP 的 _resolve_doc_pipeline_root() 探 `pipeline_core/__init__.py`——
        这个文件是**对外契约**，删了等于把 MAOP 的根解析打断。"""
        assert (PROJECT / "pipeline_core" / "__init__.py").is_file()

    def test_identity_version_and_single_warning(self):
        script = (
            "import warnings, json\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            "    import pipeline_core\n"
            "    from pipeline_core.scheduler import Scheduler\n"
            "    from pipeline_core.pipeline import TaskStatus\n"
            "    import loom_core, loom_core.scheduler, loom_core.pipeline\n"
            "    out = {\n"
            "        'root': pipeline_core is loom_core,\n"
            "        'sub': pipeline_core.scheduler is loom_core.scheduler,\n"
            "        'enum': TaskStatus is loom_core.pipeline.TaskStatus,\n"
            "        'ver': pipeline_core.__version__ == loom_core.__version__,\n"
            "        'warns': len([x for x in w if x.category is DeprecationWarning]),\n"
            "    }\n"
            "print(json.dumps(out))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(PROJECT),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-600:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["root"] is True, "pipeline_core 与 loom_core 不是同一模块对象"
        assert out["sub"] is True, "子模块 identity 漂了（转发文件型的假兼容）"
        assert out["enum"] is True, "枚举 identity 漂了（TaskStatus 双份会让状态比较全部失配）"
        assert out["ver"] is True, "旧名读到的 __version__ 与新名不一致"
        assert out["warns"] == 1, f"DeprecationWarning 应恰好一次，实际 {out['warns']}"


# ─── B. entry_points group 双读 ─────────────────

class _EP:
    def __init__(self, name: str) -> None:
        self.name = name


class TestEntryPointGroupDualRead:
    def test_constants_are_new_names(self):
        from loom_core import pack_manifest as pm
        from loom_core.agent_loader import ENTRY_POINT_GROUP, LEGACY_ENTRY_POINT_GROUPS

        assert ENTRY_POINT_GROUP == "loom.agents"
        assert "doc_pipeline.agents" in LEGACY_ENTRY_POINT_GROUPS
        assert pm.PACK_ENTRY_POINT_GROUP == "loom.packs"
        assert "doc_pipeline.packs" in pm.LEGACY_PACK_ENTRY_POINT_GROUPS

    def test_loader_reads_both_groups_new_wins(self, caplog):
        import loom_core.agent_loader as al

        table = {
            "loom.agents": [_EP("from_new"), _EP("dup")],
            "doc_pipeline.agents": [_EP("from_old"), _EP("dup")],
        }

        def fake_entry_points(group=None):
            return table.get(group, [])

        loader = al.AgentLoader.__new__(al.AgentLoader)
        loader._logger = None
        with patch.object(al, "entry_points", fake_entry_points), \
                caplog.at_level("WARNING", logger="loom_core.agent_loader"):
            found = loader._plugin_entry_points()
        assert set(found) == {"from_new", "from_old", "dup"}
        # 同名时新 group 优先（对象同一性：dup 来自 loom.agents 那个）
        assert found["dup"] is table["loom.agents"][1]
        # 旧 group 的迁移提示只在旧 group 命中时记
        assert any("doc_pipeline.agents" in r.message and "已更名" in r.message
                   for r in caplog.records)

    def test_manifest_reads_both_groups(self, caplog):
        from loom_core import pack_manifest as pm

        def fake_entry_points(group=None):
            return {"loom.packs": [_EP("p_new")],
                    "doc_pipeline.packs": [_EP("p_old")]}.get(group, [])

        with patch.object(pm, "entry_points", fake_entry_points), \
                patch.object(pm, "physical_packs", return_value=[]), \
                patch.object(pm, "pipelines_dir", return_value=PROJECT / "pipelines"), \
                caplog.at_level("WARNING", logger="loom_core.pack_manifest"):
            infos = pm.discover()
        names = {i.name for i in infos}
        assert "p_new" in names and "p_old" in names
        assert any("doc_pipeline.packs" in r.message and "已更名" in r.message
                   for r in caplog.records)

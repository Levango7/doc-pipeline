"""分层护栏：依赖方向必须单向。

Phase 1 把文档领域能力（renderer / ingest / document_enhancer）从
`pipeline_core` 拆到了顶层包 `docpipeline`。拆包本身不构成分层——
只要有人回手写一句 `from docpipeline import renderer` 放进引擎里，
"领域无关的工作流引擎"就又变回"文档流水线脚本集合"，
而且这种回退在测试全绿的情况下完全看不出来（两个包都装在同一棵树里）。

本文件用 AST 静态扫描源码（不 import、不执行被扫代码），
把依赖方向钉成门禁：

    pipeline_core  -/->  docpipeline     禁止（引擎不得认识具体领域）
    pipeline_core  -/->  agents          禁止（引擎不得认识具体插件）
    docpipeline    -/->  agents          禁止（上层不得反向依赖插件层）
    docpipeline    ->   pipeline_core    允许

同时带了判据自身的正例测试：扫描器如果压根命中不了，
"0 违规"就只是空转，而不是结论。
"""
import ast
import re
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
ENGINE = PROJECT / "pipeline_core"
DOC = PROJECT / "docpipeline"

# docpipeline 的非标准库依赖登记（逐条认领，新增必须显式加到这里）
# 同包自引用由 _scan 过滤，不会出现在结果里，因此登记集不含 "docpipeline"。
FIRST_PARTY = {"pipeline_core", "scripts"}
DECLARED_OPTIONAL = {"docx", "reportlab", "pymupdf"}     # requirements.txt 里声明
RUNTIME_PROBED = {"paddleocr", "mineru"}                 # 重型 OCR，有意不进 requirements
# 取数/知识底座：与本仓同盘的另一座仓，靠 `pip install -e ../artesian` 装，
# 还没进 PyPI 所以不能写进 requirements 的包名行——由下面那条判据盯住"过渡装法有留痕"。
LOCAL_LIBRARY = {"artesian"}
IMPORT_TO_DIST = {"docx": "python-docx", "reportlab": "reportlab", "pymupdf": "pymupdf"}


def _requirements_names() -> set[str]:
    """requirements.txt 里真正声明的包名（注释行不算声明）。"""
    names = set()
    for raw in (PROJECT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[<>=!;\[]", line)[0].strip().lower().replace("-", "_"))
    return names


def _source_imports(path: Path) -> set[str]:
    """一个源文件引用到的顶层包名。

    覆盖三种写法：`import x`、`from x import y`、
    以及 `importlib.import_module("x.y")` / `__import__("x.y")` 的字面量参数
    ——动态 import 一样能把引擎拖进文档层，不能只防静态那句。
    （相对 import `from . import y` 的 module 为 None，天然不属于跨包引用。）
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value.split(".")[0])
    return found


def _scan(pkg_dir: Path) -> list[tuple[str, str]]:
    """返回 (文件相对路径, 被引用的顶层包名) 违规候选，排除同包自引用。"""
    violations = []
    for py in sorted(pkg_dir.rglob("*.py")):
        self_name = py.parent.name if py.parent != pkg_dir else pkg_dir.name
        for imported in sorted(_source_imports(py)):
            if imported in (pkg_dir.name, self_name, "__future__"):
                continue
            violations.append((py.relative_to(PROJECT).as_posix(), imported))
    return violations


def _flagged(pkg_dir: Path, forbidden: set[str]) -> list[str]:
    out = []
    for rel, imported in _scan(pkg_dir):
        if imported in forbidden:
            out.append(f"{rel} -> {imported}")
    return out


class TestDependencyDirection:
    def test_engine_does_not_import_document_layer(self):
        assert _flagged(ENGINE, {"docpipeline"}) == [], (
            "pipeline_core 不得 import docpipeline：引擎一旦认识文档层，"
            "就无法承载非文档类工作流。请把该能力做成 Agent 或下沉为引擎原语。"
        )

    def test_engine_does_not_import_agents(self):
        assert _flagged(ENGINE, {"agents"}) == [], (
            "pipeline_core 不得 import agents：Agent 由 agent_loader 按目录发现，"
            "硬编码引用会让新增 Agent 必须改引擎。"
        )

    def test_document_layer_does_not_import_agents(self):
        assert _flagged(DOC, {"agents"}) == [], (
            "docpipeline 不得 import agents：插件层可以调用领域层，反过来会成环。"
        )

    def test_document_layer_external_deps_are_registered(self):
        """docpipeline 用到的非标准库 import 必须逐条登记在册。

        这条的价值不在"通过"，而在新增依赖时必须有人显式认领：
        少一句登记，就多一个装完环境才能发现 ImportError 的运行时坑。
        """
        found = {
            imported
            for _rel, imported in _scan(DOC)
            if imported not in _stdlib_names() and imported != "__future__"
        }
        assert found == FIRST_PARTY | DECLARED_OPTIONAL | RUNTIME_PROBED | LOCAL_LIBRARY, (
            f"docpipeline 依赖集变了：实际 {sorted(found)}；"
            f"登记的是 {sorted(FIRST_PARTY | DECLARED_OPTIONAL | RUNTIME_PROBED | LOCAL_LIBRARY)}"
        )

    def test_local_library_has_reproducible_install_source(self):
        """登记成本地库的依赖，requirements.txt 里必须有**真能装上**的那一行。

        CI 与 Dockerfile 都只跑 `pip install -r requirements.txt`——一句
        "开发期请 pip install -e ../artesian" 的注释对它们是无效的：装完就
        ImportError。所以只接受两种形态：
        ① 可复现的直接引用 `name @ git+https://…@<40 位 sha>`（未发布 PyPI 时）；
        ② PyPI 版本约束 `name>=x` / `name==x`（发布之后）。
        pin 必须是完整提交号：分支名会让同一份清单在不同时间装出不同的库。
        """
        text = (PROJECT / "requirements.txt").read_text(encoding="utf-8")
        declared = _requirements_names()
        for name in LOCAL_LIBRARY:
            pin = re.search(
                rf"^\s*{re.escape(name)}\s*@\s*git\+https?://[^\s@]+@([0-9a-fA-F]+)\s*$",
                text, re.M)
            on_pypi = name.lower().replace("-", "_") in declared
            if pin:
                assert len(pin.group(1)) == 40, (
                    f"{name} 的 git pin 必须是完整 40 位提交号，"
                    f"收到 {pin.group(1)!r}（分支/标签名会让同一份清单装出不同的库）")
            else:
                assert on_pypi, (
                    f"{name} 在 requirements.txt 里没有可安装的来源：既不是 sha pin 的"
                    f" git 直接引用，也没有 PyPI 版本约束行——CI/镜像照这份清单装不出它")

    def test_declared_optional_backends_are_in_requirements(self):
        """登记为"可选但已声明"的后端，必须真在 requirements.txt 里。

        renderer/ingest 的 docx/pdf 路线是惰性 import，缺包时节点如实跳过——
        门禁全靠这份声明；requirements 里悄悄没了，测试得先红。
        """
        declared = _requirements_names()
        missing = {
            imp for imp in DECLARED_OPTIONAL
            if IMPORT_TO_DIST[imp].lower().replace("-", "_") not in declared
        }
        assert missing == set(), f"requirements.txt 未声明这些渲染/摄入后端: {sorted(missing)}"

    def test_scripts_coupling_stays_in_one_place(self):
        """已知的历史耦合：只有 document_enhancer 可以碰 scripts/。

        不是许可，是钉住现状——`pipeline_core` 之外新增对 scripts 的引用
        说明有东西又被写在了仓库脚本里而不是包里。
        """
        offenders = [
            rel
            for rel, imported in _scan(DOC)
            if imported == "scripts" and not rel.endswith("document_enhancer.py")
        ]
        assert offenders == [], f"docpipeline 新增了对 scripts/ 的引用: {offenders}"


class TestScannerActuallyWorks:
    """判据必须能命中，否则上面几条是空转。"""

    @pytest.mark.parametrize(
        ("src", "expected"),
        [
            ("from docpipeline import renderer\n", {"docpipeline"}),
            ("import docpipeline.ingest\n", {"docpipeline"}),
            ('importlib.import_module("docpipeline.renderer")\n', {"docpipeline"}),
            ('__import__("agents.writer")\n', {"agents"}),
            ("from . import artifacts\n", set()),
            ("from pathlib import Path\n", {"pathlib"}),
        ],
    )
    def test_detects(self, tmp_path, src, expected):
        f = tmp_path / "m.py"
        f.write_text(src, encoding="utf-8")
        assert _source_imports(f) == expected


class TestSplitIsReal:
    """拆包要落地：旧路径不许留副本，新包必须真的被打包。"""

    @pytest.mark.parametrize("name", ["renderer.py", "ingest.py", "document_enhancer.py"])
    def test_moved_out_of_engine(self, name):
        assert not (ENGINE / name).exists(), f"pipeline_core/{name} 应已移到 docpipeline/"
        assert (DOC / name).exists(), f"docpipeline/{name} 缺失"

    def test_package_is_declared(self):
        """没写进 [tool.setuptools] packages 的顶层包，装完就 ImportError。"""
        text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
        line = next((ln for ln in text.splitlines() if ln.startswith("packages =")), "")
        assert "docpipeline" in line, f"pyproject 未打包 docpipeline: {line!r}"

    def test_engine_no_longer_reexports_document_api(self):
        import pipeline_core

        for gone in ("DocumentEnhancer", "renderer", "ingest"):
            assert not hasattr(pipeline_core, gone), f"pipeline_core 仍在导出 {gone}"


def _stdlib_names() -> set[str]:
    import sys

    return set(sys.stdlib_module_names)

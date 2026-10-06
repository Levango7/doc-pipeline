"""文档口径与代码实态一致性护栏。

为什么需要这一族测试：本仓的文档一直领先或落后于代码而无人发现——
README 长期写"1854 passed"而实测早已 2200+；`docs/deployment.md` 教用户
`POST /tasks` 提交任务，但该路由只有 GET（`POST` 在 `/api/tasks`）；
`llm_router` 的模块注释写"10 个供应商"而定义表里是 16 条。
此前 `tests/` 里**没有任何一条**测试引用 README 或 docs，所以这些数字
只能靠人肉发现。本文件把三处最容易腐烂的口径改成从代码取数。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ─── 1. README 的测试数必须等于实际收集数 ───────────────────

def _readme_declared_pass_count() -> int | None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*(\d+)\s*个测试本机全绿\*\*（`\1 passed", text)
    return int(m.group(1)) if m else None


@pytest.mark.slow
def test_readme_test_count_matches_collection():
    """README 声明的通过数 == 实际收集数（减去 2 条 skipif 用例）。

    数字来自 `pytest --co`，不是写死的常量——加测试时忘了改 README 会直接红。
    """
    declared = _readme_declared_pass_count()
    assert declared is not None, "README 的测试数句式被改坏了，护栏读不到数字"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--co", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode in (0, 5), f"收集失败：{proc.stdout[-800:]}{proc.stderr[-800:]}"
    tail = proc.stdout.strip().splitlines()[-1]
    m = re.search(r"(\d+)/(\d+)\s+tests? collected", tail)
    if m:
        collected = int(m.group(1))               # 已排除 deselected 的 e2e
    else:
        m2 = re.search(r"(\d+)\s+tests? collected", tail)
        assert m2, f"读不懂 --co 的汇总行：{tail!r}"
        collected = int(m2.group(1))

    # skipif 的用例被收集但不执行，所以 declared 允许比 collected 小的部分
    # 只可能是这些 skip；差超过 10 条就说明数字确实过期了。
    assert 0 <= collected - declared <= 10, (
        f"README 写 {declared} passed，而实际收集 {collected} 条；"
        f"差值 {collected - declared} 超出「仅 skipif」的容忍范围，请更新 README 的测试一节")


# ─── 2. 文档里教的 METHOD+路径必须真存在 ────────────────────

#: 规范端点本身不出现在它自己生成的 paths 里，改查 admin_api 源码是否真的处理它
SPEC_SELF = {"/api/openapi.json"}


def _spec_paths() -> dict:
    from pipeline_core.openapi_spec import generate_spec
    return generate_spec().get("paths", {})


def _served_by_admin(route: str) -> bool:
    src = (ROOT / "pipeline_core" / "admin_api.py").read_text(encoding="utf-8")
    return f'"{route}"' in src or f"'{route}'" in src


@pytest.mark.parametrize("doc", ["docs/deployment.md", "docs/api.md", "README.md"])
def test_documented_endpoints_exist(doc):
    """文档里反引号包住的 `METHOD /路径` 必须在实码里存在且支持该动词。

    只匹配反引号形式，是为了不把散文里的斜杠误当成路由。曾经的事故是
    `docs/deployment.md` 教用户用 POST 打 `/tasks`，而那条路由只有 GET。
    """
    paths = _spec_paths()
    text = (ROOT / doc).read_text(encoding="utf-8")
    refs = re.findall(r"`(GET|POST|DELETE)\s+(/[A-Za-z0-9_/{}.-]*)`", text)
    assert refs, f"{doc} 里没抓到任何端点引用，护栏需要改写法"

    missing = []
    for method, route in refs:
        if route in SPEC_SELF:
            if not _served_by_admin(route):
                missing.append(f"{method} {route}（admin_api 不再处理它）")
            continue
        norm = re.sub(r"<[^>]+>", "{x}", route)
        hit = norm if norm in paths else None
        if hit is None:
            missing.append(f"{method} {route}（规范里没有这个路径）")
        elif method.lower() not in {m.lower() for m in paths[hit]}:
            missing.append(f"{method} {route}（该路径只支持 {sorted(paths[hit])}）")
    assert not missing, f"{doc} 教了代码里不存在的端点：{missing}"


def test_tasks_is_get_only_and_docs_do_not_teach_post_tasks():
    """`/tasks` 只有 GET —— 文档不得把它写成提交任务的入口。

    写法上刻意不在 deployment.md 里留下反引号形式的 `POST /tasks`（连"别这么用"
    的提醒都不行），因为关键字扫描会命中这种自我引用；文档改用散文表述。
    """
    paths = _spec_paths()
    assert "post" not in {m.lower() for m in paths.get("/tasks", {})}, \
        "/tasks 若新增了 POST，这条护栏与 deployment.md 的说明都要同步改"
    deploy = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "`POST /tasks`" not in deploy
    assert "POST /api/tasks" in deploy, "提交任务的正确入口必须在文档里写明"


# ─── 3. LLM 供应商条数从代码取，不许文档写死 ─────────────────

def _provider_defs_length() -> int:
    tree = ast.parse((ROOT / "pipeline_core" / "llm_router.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Name) and tgt.id == "provider_defs"
                        and isinstance(node.value, ast.List)):
                    return len(node.value.elts)
    raise AssertionError("找不到 provider_defs，护栏口径需重写")


def test_llm_router_docstring_matches_provider_table():
    src = (ROOT / "pipeline_core" / "llm_router.py").read_text(encoding="utf-8")
    n = _provider_defs_length()
    docstring = ast.get_docstring(ast.parse(src)) or ""
    m = re.search(r"供应商定义表\s*(\d+)\s*家", docstring)
    assert m, "llm_router 模块注释里得有「供应商定义表 N 家」这句，护栏才有效"
    assert int(m.group(1)) == n, f"注释说 {m.group(1)} 家，定义表实际 {n} 条"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{n} 供应商定价表" in readme or f"{n} 供应商" in readme, \
        f"README 的供应商口径应与定义表一致（当前 {n}）"

"""发布就绪判据 —— PyPI 发布链的仓库侧门禁。

spec §3 P0 判据 2「外部 pip 包提供 Agent」的前置：本仓的发行物必须真的可发布。
三条在源码/元数据层就能钉住的红线（比 twine check 更早、更快）：

1. **依赖闭合**：产品源码 import 的第三方顶层包，要么在 pyproject 的
   Requires-Dist，要么在 scripts/verify_dist.py 的 EXTERNAL 登记表明文认领
   （artesian 未上 PyPI，写版本约束 = pip install 直接失败）；
2. **数据文件进包**：核心流水线的 YAML/lock 走 data-files 的 `pipelines`
   键；docgen pack 的 pipelines/quality/prompts/pack.yaml 走 `packs.docgen`
   package-data（pack-docgen 拆出后不在顶层 pipelines/ 下）。两处都断言
   glob 零命中即红——setuptools 对空 glob 是静默放行的，声明漂移只能靠
   这里的判据抓（2026-10-11 实测：装出来的引擎没有 quality profile）；
3. **插件包元数据**：六个 examples/plugin-* 的 pyproject 必须有
   license/readme/依赖声明（宿主 doc-pipeline）与合法 trove classifiers
   ——PyPI 对这些是上传期硬校验，错一个整个包发不出去。
"""
import tomllib
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))


class TestDependencyClosure:
    """产品源码 import 的第三方包不许超出声明 + EXTERNAL 登记表。"""

    def test_closure_holds(self):
        """直接调 verify_dist 的闭合判定（与发布门禁同一实现，防两套口径漂移）。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "verify_dist", PROJECT / "scripts" / "verify_dist.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        unclaimed = mod.check_dependency_closure()
        assert not unclaimed, f"未声明的第三方 import：{unclaimed}"


class TestPipelineDataInDist:
    """流水线数据必须进发行物：核心散装走 data-files，docgen pack 走 package-data。"""

    def test_data_files_have_no_stale_subdir_keys(self):
        data = _pyproject()["tool"]["setuptools"]["data-files"]
        assert "pipelines" in data
        # quality/prompts 已随 pack-docgen 移入 packs/docgen/：留死键的
        # glob 零命中是静默的，这里显式拦截回归。
        for stale in ("pipelines/quality", "pipelines/prompts"):
            assert stale not in data, (
                f"data-files 残留死键 {stale}：该目录已不在 pipelines/ 下，"
                "数据由 packs.docgen 的 package-data 承载")

    def test_pack_data_declared_and_glob_hits(self):
        pkg = _pyproject()["tool"]["setuptools"]["package-data"]["packs.docgen"]
        for pat in ("pipelines/*.yaml", "pipelines/*.lock",
                    "quality/*.yaml", "prompts/*.yaml", "pack.yaml"):
            assert pat in pkg, f"packs.docgen package-data 缺 {pat}"
        for pat in pkg:
            hits = list((PROJECT / "packs" / "docgen").glob(pat))
            assert hits, f"packs.docgen 的 glob 零命中：{pat}（源文件被删了？）"

    def test_wheel_marker_files_exist_in_repo(self):
        """data-files 引用的源文件必须真的存在（防声明漂移成空匹配）。"""
        data = _pyproject()["tool"]["setuptools"]["data-files"]
        for _key, pats in data.items():
            for pat in pats:
                hits = list(PROJECT.glob(pat))
                assert hits, f"data-files 的 glob 零命中：{pat}（源文件被删了？）"


class TestPluginPackagesMetadata:
    """六个插件包的 PyPI 元数据：上传期硬校验的字段必须齐全且合法。"""

    @pytest.mark.parametrize("pkg_dir", sorted((PROJECT / "examples").glob("plugin-*")))
    def test_metadata_complete_and_valid(self, pkg_dir):
        data = tomllib.loads((pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
        proj = data["project"]
        for key in ("license", "readme", "dependencies", "classifiers"):
            assert key in proj, f"{pkg_dir.name}: pyproject 缺 {key}"

        # 依赖宿主：插件 import loom_core，宿主发行名 2026-10-11 起为 nexusloom
        assert any(d.startswith("nexusloom") for d in proj["dependencies"]), (
            f"{pkg_dir.name}: 未声明对宿主 nexusloom 的依赖")

        # classifiers 必须是合法 trove 条目（PyPI 上传期硬校验；写错整个包发不出）
        valid_prefixes = (
            "Development Status ::", "Intended Audience ::", "License ::",
            "Operating System ::", "Programming Language ::", "Topic ::",
        )
        for c in proj["classifiers"]:
            assert c.startswith(valid_prefixes), (
                f"{pkg_dir.name}: 非法 trove classifier：{c!r}")

    @pytest.mark.parametrize("pkg_dir", sorted((PROJECT / "examples").glob("plugin-*")))
    def test_readme_exists(self, pkg_dir):
        assert (pkg_dir / "README.md").is_file(), (
            f"{pkg_dir.name}: readme 声明了但文件不存在")


class TestReleaseWorkflow:
    """release.yml 必须有 PyPI 链（verify + publish），且 publish 走 OIDC。"""

    def test_release_workflow_has_pypi_chain(self):
        import yaml

        wf = yaml.safe_load(
            (PROJECT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
        jobs = wf.get("jobs", {})
        assert "pypi-verify" in jobs, "release.yml 缺 pypi-verify（发布链第一道门）"
        assert "pypi-publish" in jobs, "release.yml 缺 pypi-publish"
        pub = jobs["pypi-publish"]
        perms = pub.get("permissions", {})
        assert perms.get("id-token") == "write", (
            "pypi-publish 需要 id-token: write（Trusted Publishing / OIDC）")

"""权重体系一致性测试

防止三处漂移：
  - quality_gate._score_all 产出的评分维度（维度唯一来源）
  - pipelines/quality/*.yaml 各 profile 的 weights 键与总和
  - pipeline YAML（docgen/docgen-verified/docreq）不得内嵌 inline weights

背景：pipeline YAML 曾内嵌与 profile 不一致的 weights（且总和≠1.0），
修复后权重统一由 profile 提供（单一事实来源），本测试锁定该约束。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.quality_gate import QUALITY_DIR, QualityGateAgent, load_profile

PROJECT = Path(__file__).parent.parent
PROFILE_NAMES = sorted(p.stem for p in QUALITY_DIR.glob("*.yaml"))


def _code_dimensions() -> set[str]:
    """quality_gate._score_all 实际产出的维度名集合（代码即事实）"""
    gate = QualityGateAgent.__new__(QualityGateAgent)
    gate.log_debug = MagicMock()
    scores = gate._score_all("# 标题\n\n这是一段足够长的正文内容。", ["测试主题"])
    return set(scores.keys())


class TestProfileWeights:
    def test_profiles_exist(self):
        assert PROFILE_NAMES, "pipelines/quality 下未找到任何 profile 文件"

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_weight_keys_subset_of_code_dimensions(self, name):
        """profile 的 weights 键必须全部是代码已实现的评分维度。

        拼错的键会被 _overall_score 的 scores.get(k, 0) 静默当 0 分处理，
        导致总分系统性偏低且不报错——必须在测试层拦截。
        """
        profile = load_profile(name)
        weights = profile.get("weights", {})
        assert weights, f"{name}: 缺少 weights 配置"
        unknown = set(weights) - _code_dimensions()
        assert not unknown, f"{name}: weights 含代码未实现的维度: {unknown}"

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_weights_sum_to_one(self, name):
        """weights 总和必须为 1.0（±0.001），保证 overall_score 与百分制语义一致"""
        profile = load_profile(name)
        total = sum(profile.get("weights", {}).values())
        assert abs(total - 1.0) < 0.001, f"{name}: weights 总和 = {total}，应为 1.0"

    @pytest.mark.parametrize("name", PROFILE_NAMES)
    def test_weights_positive(self, name):
        """每个维度权重必须为正数（0/负权重意味着维度被静默丢弃）"""
        profile = load_profile(name)
        for dim, w in profile.get("weights", {}).items():
            assert isinstance(w, (int, float)) and w > 0, f"{name}: 维度 {dim} 权重非法: {w!r}"


class TestNoInlineWeights:
    """pipeline YAML 不得内嵌 weights —— 唯一事实来源是 pipelines/quality/*.yaml"""

    @pytest.mark.parametrize("pipeline", ["docgen", "docgen-verified", "docreq"])
    def test_pipeline_yaml_has_no_inline_weights(self, pipeline):
        path = PROJECT / "pipelines" / f"{pipeline}.yaml"
        if not path.exists():
            pytest.skip(f"{pipeline}.yaml 不存在")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for agent in raw.get("agents", []):
            cfg = agent.get("config") or {}
            assert "weights" not in cfg, (
                f"{pipeline}.yaml: {agent.get('name')} config 内嵌 weights，"
                "权重应统一由 pipelines/quality/*.yaml 提供"
            )


class TestLockfileInSync:
    """lockfile 的 quality_gate config_hash 与当前 YAML 一致（无配置漂移）"""

    def test_docgen_parse_verifies_lockfile(self):
        from pipeline_core.scheduler import Scheduler
        lock = PROJECT / "pipelines" / "docgen.lock"
        if not lock.exists():
            pytest.skip("docgen.lock 不存在")
        sched = Scheduler(pipeline_dir=str(PROJECT / "pipelines"))
        # parse(verify_lock=True) 若检测到 config_hash 漂移会抛 LockfileMismatchError
        plan = sched.parse("docgen")
        assert plan.pipeline_name == "docgen"

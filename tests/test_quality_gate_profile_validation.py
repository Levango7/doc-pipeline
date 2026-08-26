"""QualityGate Profile 规则校验回归：缺必填键时 ValueError 定位到文件与索引"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.quality_gate import QualityGateAgent
from pipeline_core.base_agent import AgentMeta


def _make_config(tmp_path: Path, **extra) -> dict:
    config = {
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "quiet": True,
    }
    config.update(extra)
    return config


class TestStyleRuleValidation:

    def test_missing_pattern_raises_with_file_and_index(self, tmp_path):
        """规则缺 pattern 键时抛 ValueError，含规则文件名与索引号"""
        bad = tmp_path / "broken_rules.yaml"
        bad.write_text(
            "name: broken\n"
            "style_rules:\n"
            "  - name: ok_rule\n"
            "    pattern: '^x'\n"
            "  - name: missing_pattern\n"
            "    penalty: 2\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as ei:
            QualityGateAgent("quality_gate", AgentMeta(name="quality_gate", version="2.0"),
                             _make_config(tmp_path, quality_profile=str(bad)), None, None)
        msg = str(ei.value)
        assert "broken_rules.yaml" in msg
        assert "style_rules[1]" in msg
        assert "pattern" in msg

    def test_missing_name_raises_with_index(self, tmp_path):
        """规则缺 name 键时抛 ValueError 并定位到条目索引"""
        bad = tmp_path / "no_name.yaml"
        bad.write_text(
            "name: no-name-profile\n"
            "style_rules:\n"
            "  - pattern: '^y'\n"
            "    message: m\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as ei:
            QualityGateAgent("quality_gate", AgentMeta(name="quality_gate", version="2.0"),
                             _make_config(tmp_path, quality_profile=str(bad)), None, None)
        msg = str(ei.value)
        assert "no_name.yaml" in msg
        assert "style_rules[0]" in msg
        assert "name" in msg

    def test_valid_custom_profile_loads_and_compiles(self, tmp_path):
        """合法自定义 profile 正常加载编译，enabled=false 的规则被跳过"""
        good = tmp_path / "custom_ok.yaml"
        good.write_text(
            "name: custom-ok\n"
            "threshold: 60\n"
            "weights:\n"
            "  completeness: 1.0\n"
            "style_rules:\n"
            "  - name: rule_on\n"
            "    pattern: 'foo'\n"
            "    message: hit\n"
            "    penalty: 4\n"
            "  - name: rule_off\n"
            "    pattern: 'bar'\n"
            "    enabled: false\n",
            encoding="utf-8",
        )
        gate = QualityGateAgent("quality_gate", AgentMeta(name="quality_gate", version="2.0"),
                                _make_config(tmp_path, quality_profile=str(good)), None, None)
        names = [r["name"] for r in gate._style_rules]
        assert names == ["rule_on"]

    def test_runtime_profile_switch_validates(self, tmp_path):
        """运行期切换到坏 profile 同样抛 ValueError 而非裸崩"""
        good = tmp_path / "good.yaml"
        good.write_text("name: good-profile\nthreshold: 70\nstyle_rules:\n"
                        "  - name: r1\n    pattern: 'x'\n", encoding="utf-8")
        bad = tmp_path / "bad_switch.yaml"
        bad.write_text("name: bad-switch\nstyle_rules:\n"
                       "  - penalty: 5\n", encoding="utf-8")
        gate = QualityGateAgent("quality_gate", AgentMeta(name="quality_gate", version="2.0"),
                                _make_config(tmp_path, quality_profile=str(good)), None, None)

        from pipeline_core.message_bus_v3 import Message, MessageType
        msg = Message(topic="quality_gate.check", payload={
            "task_id": "t", "content": "# Title\n\nbody content here.\n",
            "config": {"quality_profile": str(bad)},
        }, msg_type=MessageType.REQUEST)

        with pytest.raises(ValueError) as ei:
            gate.handle(msg)
        assert "bad_switch.yaml" in str(ei.value)

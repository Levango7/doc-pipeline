"""tests/test_naming.py — 节点身份与 Agent 名的解析约定。

这是子流水线内联（`writer__review`）与 foreach 的地基：以前"从节点名还原 Agent"
以 `split("_pool_")[0]` 的形式重复在 12 处，加一种命名形态就要改 12 个文件。
收敛成一处之后，约定必须由测试钉住——它错了不会报错，只会让引擎
"把 A 的结果交给 B 执行"这种脏错误发生。
"""
import pytest

from pipeline_core.naming import ALIAS_SEP, POOL_SEP, agent_of, alias_of, node_id, pool_index_of


class TestAgentOf:
    @pytest.mark.parametrize(("name", "agent"), [
        ("writer", "writer"),
        ("writer_pool_0", "writer"),
        ("writer_pool_17", "writer"),
        ("writer__review", "writer"),
        ("kb_pool_2__sub", "kb"),
        ("quality_gate", "quality_gate"),
        ("safe_writer_pool_1__lean", "safe_writer"),   # agent 名本身含下划线
    ])
    def test_strips_pool_and_alias(self, name, agent):
        assert agent_of(name) == agent

    def test_real_agent_names_round_trip_to_themselves(self):
        """出厂 Agent 名不能被解析掉一层。"""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        agents_dir = Path(__file__).resolve().parent.parent / "agents"
        for f in sorted(agents_dir.glob("*.py")):
            src = f.read_text(encoding="utf-8")
            marker = "AGENT_NAME = "
            if marker not in src:
                continue
            declared = src.split(marker)[1].splitlines()[0].strip().strip('"').strip("'")
            assert agent_of(declared) == declared, f"{f.name}: {declared} 被解析成 {agent_of(declared)}"
            assert POOL_SEP not in declared and ALIAS_SEP not in declared, declared


class TestPoolAndAlias:
    @pytest.mark.parametrize(("name", "idx"), [
        ("writer", None),
        ("writer_pool_0", 0),
        ("writer_pool_3", 3),
        ("writer_pool_3__sub", 3),
        ("writer_pool_x", None),        # 畸形后缀不崩，按"非池化"处理
        ("writer__sub", None),
    ])
    def test_pool_index(self, name, idx):
        assert pool_index_of(name) == idx

    @pytest.mark.parametrize(("name", "alias"), [
        ("writer", ""),
        ("writer_pool_1", ""),
        ("writer__review", "review"),
        ("writer__review__deep", "review"),   # 别名只取第一段，深层归下一层
    ])
    def test_alias(self, name, alias):
        assert alias_of(name) == alias


class TestNodeIdRoundTrip:
    @pytest.mark.parametrize(("agent", "pool", "alias"), [
        ("writer", None, ""),
        ("writer", 0, ""),
        ("writer", None, "review"),
        ("kb", 2, "sub"),
    ])
    def test_build_then_parse(self, agent, pool, alias):
        name = node_id(agent, pool, alias)
        assert agent_of(name) == agent
        assert pool_index_of(name) == pool
        assert alias_of(name) == alias

    def test_distinct_inlined_instances_never_collide(self):
        """同一个 Agent 在两处内联出现，节点身份必须不同（否则 dag_nodes 互相覆盖）。"""
        a = node_id("writer", 0, "review")
        b = node_id("writer", 0, "publish")
        assert a != b and agent_of(a) == agent_of(b) == "writer"

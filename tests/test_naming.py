"""tests/test_naming.py — 节点身份与 Agent 名的解析约定。

这是子流水线内联（`writer__review`）与 foreach 的地基：以前"从节点名还原 Agent"
以 `split("_pool_")[0]` 的形式重复在 12 处，加一种命名形态就要改 12 个文件。
收敛成一处之后，约定必须由测试钉住——它错了不会报错，只会让引擎
"把 A 的结果交给 B 执行"这种脏错误发生。
"""
import pytest

from pipeline_core.naming import ALIAS_SEP, POOL_SEP, agent_of, alias_of, family_of, node_id, pool_index_of


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


class TestFamilyOf:
    """family_of 与 agent_of 必须并存：分组按家族，查注册表按 Agent。

    只用 agent_of 做分组的后果是实测到的（不是推演）：链式 call 里
    `layout__review` 掉出上游闭包，第三个节点拿到两跳之前的正文。
    """

    @pytest.mark.parametrize(("name", "family"), [
        ("writer", "writer"),
        ("writer_pool_0", "writer"),
        ("writer_pool_11", "writer"),
        ("writer__review", "writer__review"),
        ("writer_pool_2__review", "writer__review"),
        ("checker__b__via_nest", "checker__b__via_nest"),   # 嵌套内联保持自身
    ])
    def test_family_keeps_alias_drops_pool(self, name, family):
        assert family_of(name) == family

    def test_family_groups_pool_siblings_of_one_inlined_node(self):
        a, b = "writer_pool_0__review", "writer_pool_1__review"
        assert family_of(a) == family_of(b) == "writer__review"
        assert family_of(a) != family_of("writer_pool_0"), "父图同名节点不能被并进来"

    def test_agent_of_would_have_collided_the_two(self):
        """反证：旧写法为什么会掉节点——它把 review 与父图 writer 视为同一个。"""
        assert agent_of("writer_pool_0__review") == agent_of("writer_pool_0") == "writer"


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

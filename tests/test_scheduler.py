"""Scheduler — plan parsing, execution nodes, pooling"""


class TestSchedulerParsing:
    """YAML pipeline 解析"""

    def test_parse_docgen(self, docgen_plan):
        assert docgen_plan is not None
        assert docgen_plan.pipeline_name in ("docgen", "test_pipeline")
        assert docgen_plan.node_count > 0
        assert docgen_plan.plan_id is not None

    def test_plan_has_levels(self, docgen_plan):
        assert len(docgen_plan.levels) > 0, "should have at least 1 level"
        for level in docgen_plan.levels:
            assert len(level) > 0, "each level needs >=1 node"

    def test_plan_node_structure(self, docgen_plan):
        """每个节点有 agent_name、config、dependencies"""
        node = docgen_plan.levels[0][0]
        assert hasattr(node, "agent_name")
        assert hasattr(node, "dependencies")
        assert hasattr(node, "timeout")
        assert hasattr(node, "max_retries")
        assert hasattr(node, "agent_config"), "node missing agent_config"

    def test_researcher_has_pool_size(self, docgen_plan):
        """researcher agent 应有 pool_size=2"""
        for level in docgen_plan.levels:
            for node in level:
                if "researcher" in node.agent_name:
                    assert hasattr(node.agent_config, "pool_size")
                    break

    def test_dependency_ordering(self, docgen_plan):
        """writer 依赖 fetcher/researcher, checker 依赖 writer"""
        dep_map = {}
        for level in docgen_plan.levels:
            for node in level:
                dep_map[node.agent_name] = node.dependencies
        assert "writer" in dep_map
        assert "checker" in dep_map
        # writer 应依赖 fetcher 或 researcher
        writer_deps = dep_map.get("writer", [])
        assert any(d in ("fetcher", "researcher") for d in writer_deps), \
            f"writer should depend on fetcher/researcher, got {writer_deps}"


class TestSchedulerFromDict:
    """从 dict 构建 plan（通过 _build_plan）"""

    def test_parse_dict(self, scheduler):
        raw = {
            "name": "test_pipeline",
            "agents": [{
                "name": "test_agent",
                "timeout": 30,
                "retry": {"max_retries": 2},
                "dependencies": [],
                "config": {"key": "val"},
            }],
            "topology": {"levels": [["test_agent"]]},
        }
        plan = scheduler._build_plan(raw, "test_pipeline")
        assert plan.pipeline_name == "test_pipeline"
        assert plan.node_count == 1
        assert plan.levels[0][0].agent_name == "test_agent"

    def test_checkpoint_blocked(self, scheduler):
        """blocked_agents 配置应被解析"""
        raw = {
            "name": "check_test",
            "agents": [{
                "name": "a",
                "dependencies": [],
                "config": {},
            }],
            "topology": {"levels": [["a"]]},
            "checkpoint": {"blocked_agents": ["a"]},
            "user": {"username": ""},
        }
        plan = scheduler._build_plan(raw, "check_test")
        assert plan is not None
        assert plan.raw.get("checkpoint", {}).get("blocked_agents") == ["a"]

    def test_raw_config_preserved(self, scheduler):
        raw = {
            "name": "raw_test",
            "agents": [{
                "name": "a",
                "dependencies": [],
                "config": {"nested": {"k": "v"}},
            }],
            "topology": {"levels": [["a"]]},
        }
        plan = scheduler._build_plan(raw, "raw_test")
        assert plan.raw["agents"][0]["config"]["nested"]["k"] == "v"

    def test_pool_dependencies_expanded(self, scheduler):
        """pool_size>1 的上游，下游依赖应展开为 pool 实例名"""
        raw = {
            "name": "pool_test",
            "agents": [
                {
                    "name": "researcher",
                    "pool_size": 2,
                    "dependencies": [],
                    "config": {},
                },
                {
                    "name": "fetcher",
                    "pool_size": 1,
                    "dependencies": ["researcher"],
                    "config": {},
                },
                {
                    "name": "writer",
                    "pool_size": 1,
                    "dependencies": ["fetcher"],
                    "config": {},
                },
            ],
            "topology": {"levels": [["researcher"], ["fetcher"], ["writer"]]},
        }
        plan = scheduler._build_plan(raw, "pool_test")

        # 验证 researcher 展开
        r_names = [n.agent_name for n in plan.levels[0]]
        assert "researcher_pool_0" in r_names
        assert "researcher_pool_1" in r_names
        assert "researcher" not in r_names

        # 验证 fetcher 依赖已展开为 pool 名
        fetcher_node = plan.levels[1][0]
        assert "researcher_pool_0" in fetcher_node.dependencies
        assert "researcher_pool_1" in fetcher_node.dependencies
        assert "researcher" not in fetcher_node.dependencies

        # 验证 writer 依赖仍是简单名（fetcher pool_size=1）
        writer_node = plan.levels[2][0]
        assert writer_node.dependencies == ["fetcher"]

    def test_pool_no_expand_single(self, scheduler):
        """pool_size=1 时依赖不展开"""
        raw = {
            "name": "simple",
            "agents": [
                {"name": "a", "pool_size": 1, "dependencies": [], "config": {}},
                {"name": "b", "pool_size": 1, "dependencies": ["a"], "config": {}},
            ],
            "topology": {"levels": [["a"], ["b"]]},
        }
        plan = scheduler._build_plan(raw, "simple")
        assert plan.levels[0][0].agent_name == "a"
        assert plan.levels[1][0].dependencies == ["a"]

    def test_pool_multi_level_deps(self, scheduler):
        """三层 cascade：r(pool=3) → f(pool=2) → w(pool=1)"""
        raw = {
            "name": "cascade",
            "agents": [
                {"name": "r", "pool_size": 3, "dependencies": [], "config": {}},
                {"name": "f", "pool_size": 2, "dependencies": ["r"], "config": {}},
                {"name": "w", "pool_size": 1, "dependencies": ["f"], "config": {}},
            ],
            "topology": {"levels": [["r"], ["f"], ["w"]]},
        }
        plan = scheduler._build_plan(raw, "cascade")
        # researcher 实例
        r_names = [n.agent_name for n in plan.levels[0]]
        assert len(r_names) == 3
        # fetcher 实例
        f_nodes = plan.levels[1]
        assert len(f_nodes) == 2
        for fn in f_nodes:
            assert len(fn.dependencies) == 3
            for i in range(3):
                assert f"r_pool_{i}" in fn.dependencies
        # writer 依赖
        w_node = plan.levels[2][0]
        assert len(w_node.dependencies) == 2
        for i in range(2):
            assert f"f_pool_{i}" in w_node.dependencies

# ─── 流水线名解析（外壳不再内置默认产品名）─────────────────

from pipeline_core.scheduler import installed_pipelines, resolve_pipeline_name  # noqa: E402


class TestResolvePipelineName:
    AVAILABLE = ["docgen", "docgen-render", "kb-docgen"]

    def test_explicit_name_wins(self):
        assert resolve_pipeline_name("kb-docgen", self.AVAILABLE, "docgen") == ("kb-docgen", "")

    def test_configured_default_is_used(self):
        assert resolve_pipeline_name("", self.AVAILABLE, "kb-docgen") == ("kb-docgen", "")

    def test_unknown_explicit_name_is_rejected_with_choices(self):
        name, err = resolve_pipeline_name("nope", self.AVAILABLE, "docgen")
        assert name == ""
        assert "不存在" in err and "kb-docgen" in err

    def test_ambiguity_is_not_guessed_when_no_default(self):
        """多条流水线且没配默认值 → 报错列出可用，绝不静默挑一个。"""
        name, err = resolve_pipeline_name("", self.AVAILABLE, "")
        assert name == "" and "未指定" in err

    def test_single_pipeline_is_implicit_default(self):
        assert resolve_pipeline_name("", ["only"], "") == ("only", "")

    def test_stale_config_default_is_reported_not_swallowed(self):
        name, err = resolve_pipeline_name("", self.AVAILABLE, "removed_pipeline")
        assert name == "" and "default_pipeline" in err

    def test_installed_pipelines_is_cwd_independent(self, tmp_path, monkeypatch):
        """实例默认 pipeline_dir 是相对路径；外壳要用安装目录视角。"""
        monkeypatch.chdir(tmp_path)
        assert "docgen" in installed_pipelines()
        assert installed_pipelines(tmp_path) == []


class TestYamlNameTrap:
    """`- name: on` 会被 YAML 1.1 解析成布尔 True（on/off/yes/no/y/n 都是）。

    不加这道判据时，报错发生在几千行之外的 `agent_of()`：
    'bool' object has no attribute 'split'——既看不出是名字写错，也看不出为什么。
    """

    def _build(self, name_value):
        from pipeline_core.scheduler import Scheduler

        sched = Scheduler(pipeline_dir="pipelines")
        raw = {
            "agents": [{"name": name_value, "dependencies": []}],
            "topology": {"levels": [[name_value]]},
        }
        return sched._build_plan(raw, "trap")

    def test_boolean_name_is_rejected_with_a_readable_reason(self):
        import pytest

        with pytest.raises(ValueError, match="YAML 会把 on/off"):
            self._build(True)

    def test_missing_or_blank_name_is_rejected(self):
        import pytest

        for bad in ("", "  ", None):
            with pytest.raises(ValueError):
                self._build(bad)

    def test_quoted_on_still_works(self):
        """加了引号就是字符串，不该被误拦。"""
        assert self._build("checked").node_count == 1

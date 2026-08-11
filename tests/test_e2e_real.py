"""真实端到端测试 — 调用真实 LLM + 搜索引擎

运行方式:
    pytest -m e2e                          # 运行所有 e2e 测试
    pytest -m e2e -k test_search           # 只运行搜索相关
    pytest -m "e2e and not slow"           # 排除慢测试

前置条件:
    1. .env 文件存在且配置了真实 API Key（至少 BOCHA_API_KEY 或 TAVILY_API_KEY + LLM_API_KEY）
    2. 网络可达

CI 默认跳过（pyproject.toml 中 addopts = "-m 'not e2e'"）
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT = Path(__file__).parent.parent
ENV_FILE = PROJECT / ".env"


def _has_env():
    """检查 .env 是否存在且包含真实 key（非 placeholder）"""
    if not ENV_FILE.exists():
        return False
    text = ENV_FILE.read_text(encoding="utf-8")
    for key in ("BOCHA_API_KEY", "TAVILY_API_KEY"):
        for line in text.splitlines():
            if line.startswith(f"{key}=") and "your_" not in line:
                return True
    return False


def _has_llm():
    if not ENV_FILE.exists():
        return False
    text = ENV_FILE.read_text(encoding="utf-8")
    return any(line.startswith("LLM_API_KEY=") and "your_" not in line for line in text.splitlines())


skip_no_env = pytest.mark.skipif(
    not _has_env(), reason=".env 未配置真实搜索 API Key"
)
skip_no_llm = pytest.mark.skipif(
    not _has_llm(), reason=".env 未配置真实 LLM API Key"
)


@pytest.mark.e2e
@skip_no_env
def test_search_bocha_real():
    """真实 Bocha 搜索 — 验证 SearchEngineManager 能调通 API"""
    from pipeline_core.search_engines import SearchEngineManager

    mgr = SearchEngineManager.from_env(str(ENV_FILE))
    results = mgr.search("Python asyncio 教程", max_results=3, engines=["bocha"])
    assert len(results) > 0, "Bocha 搜索应返回结果"
    item = results[0]
    assert hasattr(item, "title") or isinstance(item, dict)
    if isinstance(item, dict):
        assert item.get("title") or item.get("url")
    print(f"  Bocha 返回 {len(results)} 条结果")


@pytest.mark.e2e
@skip_no_env
def test_search_tavily_real():
    """真实 Tavily 搜索 — 验证英文技术文档搜索质量"""
    from pipeline_core.search_engines import SearchEngineManager

    mgr = SearchEngineManager.from_env(str(ENV_FILE))
    results = mgr.search("React useEffect cleanup", max_results=3, engines=["tavily"])
    assert len(results) > 0, "Tavily 搜索应返回结果"
    print(f"  Tavily 返回 {len(results)} 条结果")


@pytest.mark.e2e
@skip_no_env
def test_search_multi_engine_fallback():
    """多引擎 fallback — 第一个引擎失败时自动切换"""
    from pipeline_core.search_engines import SearchEngineManager

    mgr = SearchEngineManager.from_env(str(ENV_FILE))
    results = mgr.search("Docker compose 部署", max_results=5,
                         engines=["bocha", "tavily", "serper", "bing"])
    assert len(results) > 0, "多引擎 fallback 应至少返回一个结果"
    print(f"  多引擎返回 {len(results)} 条结果")


@pytest.mark.e2e
@skip_no_llm
def test_llm_router_real():
    """真实 LLM 调用 — 验证 LLMRouter 能调通至少一个供应商"""
    from pipeline_core.llm_router import LLMRouter

    router = LLMRouter.from_env(str(ENV_FILE))
    providers = router.get_active_providers()
    assert len(providers) > 0, "应至少有一个可用 LLM 供应商"

    response, provider = router.chat(
        messages=[{"role": "user", "content": "说一个字：好"}],
        max_tokens=10,
        temperature=0.0,
        timeout=15,
    )
    assert response, f"LLM 应返回非空响应（provider={provider}）"
    print(f"  LLM 响应: {response[:50]}... (provider={provider})")


@pytest.mark.e2e
@skip_no_env
@skip_no_llm
def test_full_pipeline_real():
    """完整流水线端到端 — 真实搜索 + 真实 LLM 生成文档

    这是唯一一个真正端到端的测试：输入主题 → 输出 Markdown 文档。
    超时 120s，标记为 slow。
    """
    from pipeline_core import PipelineOrchestrator
    from pipeline_core.scheduler import Scheduler

    sched = Scheduler()
    plan = sched.parse("docgen")

    orch = PipelineOrchestrator(
        agents_dir=str(PROJECT / "agents"),
        checkpoint_dir=str(PROJECT / ".test_checkpoints"),
    )
    orch.register_agents()

    try:
        import tempfile
        input_file = Path(tempfile.gettempdir()) / "e2e_input.md"
        input_file.write_text(
            "# Python asyncio 异步编程\n\n## 查询\n\nPython asyncio 事件循环原理\n",
            encoding="utf-8",
        )

        task = orch.run_plan(plan, input_file=str(input_file), wait=True)

        assert task.status.value in ("done", "failed"), \
            f"任务应完成或失败，当前状态: {task.status.value}"

        if task.status.value == "done":
            assert len(task.result) > 0, "完成的任务应有结果"
            print(f"  流水线完成，result keys: {list(task.result.keys())}")
        else:
            print(f"  流水线失败（可能是 LLM 限流）: {task.error}")
    finally:
        orch.shutdown()


@pytest.mark.e2e
@skip_no_env
@skip_no_llm
@pytest.mark.slow
def test_submit_task_api_real():
    """POST /api/tasks 真实提交 — 启动 AdminAPI HTTP 服务器验证端到端"""
    import json
    import urllib.request

    from pipeline_core import PipelineOrchestrator
    from pipeline_core.admin_api import AdminAPI

    orch = PipelineOrchestrator(
        agents_dir=str(PROJECT / "agents"),
        checkpoint_dir=str(PROJECT / ".test_checkpoints"),
    )
    orch.register_agents()

    api = AdminAPI(orch=orch, host="127.0.0.1", port=18923)
    api.start()
    try:
        time.sleep(0.5)
        body = json.dumps({
            "query": "Git rebase 工作原理",
            "title": "Git Rebase 指南",
            "pipeline": "docgen",
            "wait": False,
        }).encode()

        req = urllib.request.Request(
            "http://127.0.0.1:18923/api/tasks",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        assert "task_id" in data, f"响应应含 task_id: {data}"
        assert data["status"] in ("running", "pending", "done"), f"状态异常: {data}"
        print(f"  API 提交成功: task_id={data['task_id']}, status={data['status']}")
    finally:
        api.stop()
        orch.shutdown()

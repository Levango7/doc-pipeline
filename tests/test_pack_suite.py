"""出厂 pack 套件 —— 九条新任务型流水线的离线端到端判据 + pack 清册护栏。

背景（docs/product-spec.md §2 的四轴口径）："任务类型（真实跑通的 pack）"从
2 条（docgen 系 / api-report）扩到 ≥10 条，且**每条 pack 必须有至少一条端到端
测试真跑通**（不是 mock 断言）。本文件承担这条验收线：

- 九条新 pack（intel-brief / kb-brief / data-qc / alert-runbook / deck-brief /
  minutes-weekly / invoice-extract / k8s-patrol / spec-cases）各配一条 E2E：
  真 Scheduler → 真 DAGExecutor → 真 Agent → 真落盘，只在**外部 IO 边界**
  （HTTP 响应 / 搜索引擎）换成罐头，其余全走真件；判据落在交付物内容上。
- `TestPackInventory` 是清册护栏：把"≥10 条 pack、每条有 lock 且能过锁校验、
  每条有具名 E2E 判据"三件事钉成会变红的断言——新增 pack 忘了配判据、
  锁漂移未重锁、判据被删，都在这里暴露。
"""
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

PIPELINES = PROJECT / "pipelines"


class _Resp:
    """最小 requests.Response 替身（与 test_generic_agents 同形）。"""

    def __init__(self, body=b"{}", status=200, headers=None, encoding="utf-8"):
        self._body = body
        self.status_code = status
        self.headers = headers or {}
        self.encoding = encoding

    def iter_content(self, chunk_size=8192):
        return iter([self._body])

    def close(self):
        pass


def _json_resp(payload) -> _Resp:
    return _Resp(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                 headers={"Content-Type": "application/json"})


@pytest.fixture(scope="module")
def pack_orch(tmp_path_factory):
    """整包共用一个 Orchestrator：注册一次（约 20s 的 Agent 导入），九条 E2E 复用。

    每条 E2E 用独立 task_id（bus_data 的幂等记录按 checkout 绝对路径共享），
    并把 cwd 切到各自的 tmp 目录，交付物互不串台。
    """
    from pipeline_core import PipelineOrchestrator

    ck = tmp_path_factory.mktemp("pack_ck")
    orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                checkpoint_dir=str(ck))
    orch.register_agents()
    yield orch
    orch.shutdown()


@pytest.fixture(autouse=True)
def _deterministic_dns(monkeypatch):
    """把 url_guard 的 DNS 解析换成确定性映射：测试不出网、也不依赖外网 DNS。

    - 映射里的公网域名 → 固定公网 IP（守卫放行；真实请求仍由请求层罐头拦截）；
    - 其余主机（含 `k8s-api.internal` 这类内网名）→ 解析失败，守卫拒绝——
      恰好让 k8s-patrol 走 `allow_hosts` 放行路径，判据断的就是这条。
    与 tests/test_fetcher_security.py 同一取舍（那里也钉了 url_guard 的 DNS）。
    """
    import socket as _socket

    import artesian.url_guard as url_guard

    mapping = {
        "api.github.com": ["93.184.216.34"],
        "alerts.example.com": ["93.184.216.34"],
        "minutes.example.com": ["93.184.216.34"],
        "billing.example.com": ["93.184.216.34"],
        "reports.example.com": ["93.184.216.34"],
    }

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in mapping:
            raise _socket.gaierror(-2, f"mock dns: {host}")
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (ip, 80))
                for ip in mapping[host]]

    # 负缓存 TTL 60s：先清一次，避免其它测试缓存的解析失败污染本次映射
    url_guard.clear_dns_cache()
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)
    yield
    url_guard.clear_dns_cache()


def _plan(pack: str, verify_lock: bool = True):
    from pipeline_core.scheduler import Scheduler
    sched = Scheduler(pipeline_dir=str(PIPELINES), agents_dir=PROJECT / "agents")
    return sched.parse(pack, verify_lock=verify_lock)


def _input_file(tmp_path: Path, text: str) -> str:
    p = tmp_path / "input.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _run(orch, plan, tmp_path: Path, input_text: str, out_name: str = "out.md"):
    """跑一条真流水线；交付物路径由 pipeline.output 指到 tmp。"""
    out = tmp_path / out_name
    plan.raw.setdefault("pipeline", {})["output"] = str(out)
    inp = _input_file(tmp_path, input_text)
    task = orch.run_plan(plan, input_file=inp, wait=True,
                         task_id=f"pack-{uuid.uuid4().hex[:8]}")
    status = getattr(task.status, "value", str(task.status))
    steps = [(s.step_name, s.status, (s.error or "")[:120])
             for s in (task.steps or [])]
    assert status in ("done", "completed"), (status, task.error, steps)
    return task, out


# ═══════════════════════════════════════════════════════════════
# 1. intel-brief —— 检索 → 逐条要点渲染 → 落盘
# ═══════════════════════════════════════════════════════════════

class TestIntelBriefE2E:
    def _offline_yaml(self, tmp_path) -> Path:
        """把出厂 YAML 的引擎列表换成 mock（CI/本机都不许出网）。

        其余节点/连线/模板逐字不动——只动"外部搜索引擎"这一个物理边界，
        与 kb-docgen 离线测试替换 embedder 是同一取舍。
        """
        raw = yaml.safe_load((PIPELINES / "intel-brief.yaml").read_text(encoding="utf-8"))
        for node in raw["agents"]:
            if node["name"] == "researcher":
                node["config"]["search_engines"] = ["mock"]
        path = tmp_path / "intel-brief-offline.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return path

    def test_pipeline_runs_and_delivers_search_brief(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from pipeline_core.scheduler import Scheduler

        sched = Scheduler(pipeline_dir=str(tmp_path), agents_dir=PROJECT / "agents")
        plan = sched.parse_file(str(self._offline_yaml(tmp_path)), verify_lock=False)
        task, out = _run(pack_orch, plan, tmp_path,
                         "# 主题\nKafka 生产者配额\n")

        assert out.exists(), "检索简报未落盘"
        text = out.read_text(encoding="utf-8")
        assert "mock 搜索结果: Kafka 生产者配额" in text, text[:300]
        assert "](https://example.com/search" in text, "要点未渲染成带链接的条目"
        assert "{{" not in text, "模板未渲染"
        # 检索结果逐条渲染（foreach 聚合）：1 条查询 → 1 个条目
        assert text.count("- [") >= 1


# ═══════════════════════════════════════════════════════════════
# 2. kb-brief —— 本地资料 → 建库检索 → 摘录卡 → 落盘（全离线）
# ═══════════════════════════════════════════════════════════════

class TestKbBriefE2E:
    def test_local_corpus_digest_offline(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "kafka-quota.md").write_text(
            "# Kafka 配额设计\n\nproducer_byte_rate 限制客户端写入字节率，"
            "超限后请求被节流。\n", encoding="utf-8")
        (corpus / "kafka-partition.md").write_text(
            "# Kafka 分区策略\n\n分区数决定并行消费上限，副本跨机架放置。\n",
            encoding="utf-8")

        plan = _plan("kb-brief")  # 出厂 YAML 声明 embedder: hash，无需改写
        task, out = _run(pack_orch, plan, tmp_path, "\n".join([
            "# 主题", "Kafka 生产者配额", "", "# 资料",
            str(corpus / "kafka-quota.md"), str(corpus / "kafka-partition.md"), "",
        ]))

        kb_result = (task.result or {}).get("knowledge_base") or {}
        assert kb_result.get("hits", 0) > 0, "知识库节点没有产出命中"
        text = out.read_text(encoding="utf-8")
        assert "producer_byte_rate" in text, "摘录卡未接地到本地语料"
        assert "相关度:" in text, "摘录卡缺少出处/相关度标注"
        assert "{{" not in text


# ═══════════════════════════════════════════════════════════════
# 3. data-qc —— 接口数据 → 声明式判定统计 → 质量门 → 落盘
# ═══════════════════════════════════════════════════════════════

class TestDataQcE2E:
    CANNED = [
        {"number": 1, "title": "修复登录超时", "comments": 3},
        {"number": 2, "title": "", "comments": 1},           # 缺标题 → 异常
        {"number": 3, "title": "补充索引", "comments": -1},   # 评论数为负 → 异常
        {"number": 4, "title": "更新文档", "comments": 0},
        {"number": 5, "title": "增加重试", "comments": 2},
    ]

    def test_qc_counts_and_gate_pass(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = _plan("data-qc")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            task, out = _run(pack_orch, plan, tmp_path, "# 主题\n数据集质检\n")

        text = out.read_text(encoding="utf-8")
        assert "| 记录总数 | 5 |" in text, text[:300]
        assert "| 异常条数 | 2 |" in text, "where 判定结果与罐头数据不符"
        gate = (task.result or {}).get("quality_gate") or {}
        assert gate.get("status") == "pass", gate
        assert float(gate.get("overall_score", 0)) >= 45


# ═══════════════════════════════════════════════════════════════
# 4. alert-runbook —— 告警 JSON → 逐条处置手册 → 落盘
# ═══════════════════════════════════════════════════════════════

class TestAlertRunbookE2E:
    CANNED = {"alerts": [
        {"severity": "P1", "name": "API 5xx 激增", "service": "checkout",
         "instance": "pod-3", "started_at": "2026-10-09T10:12:00Z",
         "summary": "5xx 比例 12%", "runbook": "回滚最近一次发布并扩容"},
        {"severity": "P2", "name": "磁盘使用率 85%", "service": "logging",
         "instance": "node-7", "started_at": "2026-10-09T09:40:00Z",
         "summary": "/data 使用率 85%", "runbook": "清理历史索引并扩容"},
    ]}

    def test_runbook_per_alert(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = _plan("alert-runbook")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            _, out = _run(pack_orch, plan, tmp_path, "# 主题\n告警处置\n")

        text = out.read_text(encoding="utf-8")
        assert "## [P1] API 5xx 激增" in text
        assert "## [P2] 磁盘使用率 85%" in text
        assert "回滚最近一次发布并扩容" in text, "处置步骤未渲染"
        assert text.count("- 处置步骤:") == 2, "foreach 逐条展开条数不对"


# ═══════════════════════════════════════════════════════════════
# 5. deck-brief —— 要点 JSON → 分节渲染 → Markdown + pptx 交付
# ═══════════════════════════════════════════════════════════════

class TestDeckBriefE2E:
    CANNED = {"highlights": [
        {"heading": "季度增长", "conclusion": "ARR 环比 +18%",
         "evidence": "新增付费客户 42 家", "owner": "王强"},
        {"heading": "稳定性", "conclusion": "可用性 99.95%",
         "evidence": "P1 故障 0 起", "owner": "赵敏"},
    ]}

    def test_deck_delivers_md_and_pptx(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = _plan("deck-brief")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            task, out = _run(pack_orch, plan, tmp_path, "# 主题\n季度汇报\n")

        text = out.read_text(encoding="utf-8")
        assert "## 季度增长" in text and "## 稳定性" in text
        assert "{{" not in text

        renderer_result = (task.result or {}).get("renderer") or {}
        assert renderer_result.get("formats") == ["pptx"], (
            "节点级 formats 未生效（应只渲染 pptx）："
            f"{renderer_result.get('formats')}")
        pptx_path = Path(renderer_result["outputs"]["pptx"])
        assert pptx_path.exists(), f"pptx 未落到 {pptx_path}"

        pytest.importorskip("pptx")
        import pptx
        prs = pptx.Presentation(str(pptx_path))
        titles = [s.shapes.title.text for s in prs.slides if s.shapes.title]
        assert any("季度增长" in t for t in titles), titles


# ═══════════════════════════════════════════════════════════════
# 6. minutes-weekly —— 会议纪要 JSON → 逐议题周报 → 落盘
# ═══════════════════════════════════════════════════════════════

class TestMinutesWeeklyE2E:
    CANNED = {"agenda": [
        {"topic": "发布节奏", "decision": "改为双周发布",
         "owner": "张伟", "due": "2026-10-16", "status": "进行中"},
        {"topic": "告警降噪", "decision": "同源告警合并",
         "owner": "李娜", "due": "2026-10-20", "status": "待启动"},
    ]}

    def test_weekly_report_from_minutes(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = _plan("minutes-weekly")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            _, out = _run(pack_orch, plan, tmp_path, "# 主题\n本周研发周报\n")

        text = out.read_text(encoding="utf-8")
        assert "## 发布节奏" in text and "## 告警降噪" in text
        assert "- 负责人: 张伟" in text and "- 截止: 2026-10-20" in text
        assert "{{" not in text


# ═══════════════════════════════════════════════════════════════
# 7. invoice-extract —— 发票 JSON → 逐票要素台账 → Markdown + pptx
# ═══════════════════════════════════════════════════════════════

class TestInvoiceExtractE2E:
    CANNED = {"invoices": [
        {"no": "INV-2026-0901", "issuer": "云启科技", "currency": "CNY",
         "amount": 12800.5, "date": "2026-09-03", "tax": 768.03},
        {"no": "INV-2026-0902", "issuer": "海联数据", "currency": "CNY",
         "amount": 4300, "date": "2026-09-11", "tax": 258},
    ]}

    def test_element_ledger_and_pptx(self, pack_orch, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan = _plan("invoice-extract")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            task, out = _run(pack_orch, plan, tmp_path, "# 主题\n发票抽取\n")

        text = out.read_text(encoding="utf-8")
        assert "### 发票 INV-2026-0901" in text and "### 发票 INV-2026-0902" in text
        assert "12800.5" in text and "云启科技" in text, "要素未逐票抽取"
        assert "{{" not in text

        renderer_result = (task.result or {}).get("renderer") or {}
        assert renderer_result.get("formats") == ["pptx"], renderer_result
        assert Path(renderer_result["outputs"]["pptx"]).exists()


# ═══════════════════════════════════════════════════════════════
# 8. k8s-patrol —— 内网集群 API（allow_hosts 放行）→ 巡检报告
# ═══════════════════════════════════════════════════════════════

class TestK8sPatrolE2E:
    CANNED = {"items": [
        {"metadata": {"name": "k8s-node-1"},
         "status": {"conditions": [{"type": "Ready", "status": "True"}],
                    "nodeInfo": {"kubeletVersion": "v1.30.2",
                                 "containerRuntimeVersion": "containerd://1.7.18",
                                 "osImage": "Ubuntu 22.04.4 LTS"}}},
        {"metadata": {"name": "k8s-node-2"},
         "status": {"conditions": [{"type": "Ready", "status": "False"}],
                    "nodeInfo": {"kubeletVersion": "v1.30.2",
                                 "containerRuntimeVersion": "containerd://1.7.18",
                                 "osImage": "Ubuntu 22.04.4 LTS"}}},
    ]}

    def test_internal_host_goes_through_allowlist(self, pack_orch, tmp_path, monkeypatch):
        """内网地址必须走 allow_hosts 白名单放行——判据同时钉住 guard 路径。"""
        monkeypatch.chdir(tmp_path)
        plan = _plan("k8s-patrol")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_json_resp(self.CANNED)):
            task, out = _run(pack_orch, plan, tmp_path, "# 主题\n集群巡检\n")

        http_result = (task.result or {}).get("http_request") or {}
        assert http_result.get("guard") == "allowlist", (
            "内网主机应经 allow_hosts 放行（guard=allowlist），"
            f"实际 guard={http_result.get('guard')!r}")

        text = out.read_text(encoding="utf-8")
        assert "## 节点 k8s-node-1" in text and "## 节点 k8s-node-2" in text
        assert "kubelet: v1.30.2" in text
        assert "状态: False" in text, "巡检结论未如实反映节点状态"
        assert "{{" not in text


# ═══════════════════════════════════════════════════════════════
# 9. spec-cases —— 需求文本 → 规则解析 → 逐关键词用例清单（全离线）
# ═══════════════════════════════════════════════════════════════

class TestSpecCasesE2E:
    def test_rule_path_produces_cases(self, pack_orch, tmp_path, monkeypatch):
        """节点级 llm_enabled: false 必须生效：无 LLM 也逐字可复现。"""
        monkeypatch.chdir(tmp_path)
        plan = _plan("spec-cases")
        task, out = _run(pack_orch, plan, tmp_path,
                         "Kafka 接口文档，面向入门读者，覆盖生产者配额与分区策略\n")

        spec = ((task.result or {}).get("requirements_analyzer") or {}).get("spec") or {}
        assert spec.get("doc_type") == "api", spec
        text = out.read_text(encoding="utf-8")
        assert "## 用例 0：" in text, "用例清单未逐关键词渲染"
        assert "验证步骤" in text, "用例骨架缺验证步骤"
        assert "kafka" in text.lower() or "接口文档" in text, "范围关键词未进用例"
        assert "{{" not in text


# ═══════════════════════════════════════════════════════════════
# 清册护栏 —— "≥10 条 pack、每条有锁、每条有具名 E2E"
# ═══════════════════════════════════════════════════════════════

#: pack 名 → E2E 判据位置（本文件或既有测试文件里的类名）。
#: 新增 pack 必须在这里登记，否则 test_registry_covers_ten_packs 会红。
PACK_E2E = {
    "docgen": "tests/test_kb_pipeline_wiring.py::TestKbPipelineOfflineE2E",
    "api-report": "tests/test_generic_agents.py::TestApiReportEndToEnd",
    "intel-brief": "tests/test_pack_suite.py::TestIntelBriefE2E",
    "kb-brief": "tests/test_pack_suite.py::TestKbBriefE2E",
    "data-qc": "tests/test_pack_suite.py::TestDataQcE2E",
    "alert-runbook": "tests/test_pack_suite.py::TestAlertRunbookE2E",
    "deck-brief": "tests/test_pack_suite.py::TestDeckBriefE2E",
    "minutes-weekly": "tests/test_pack_suite.py::TestMinutesWeeklyE2E",
    "invoice-extract": "tests/test_pack_suite.py::TestInvoiceExtractE2E",
    "k8s-patrol": "tests/test_pack_suite.py::TestK8sPatrolE2E",
    "spec-cases": "tests/test_pack_suite.py::TestSpecCasesE2E",
}

#: 本文件（判据在这里就地可以 import 校验；其余文件按类名文本校验）
_THIS_FILE = "tests/test_pack_suite.py"


class TestPackInventory:
    """把 spec §2 的验收线第 1 条（pack ≥10 且每条有真 E2E）钉成护栏。"""

    def test_registry_covers_ten_packs(self):
        assert len(PACK_E2E) >= 10, (
            f"pack 清册只有 {len(PACK_E2E)} 条，验收线要求 ≥10；"
            "新增 pack 时同步登记到 PACK_E2E")

    @pytest.mark.parametrize("pack", sorted(PACK_E2E))
    def test_every_pack_has_yaml_lock_and_verifies(self, pack):
        """YAML 存在 + 配套 lock 存在 + 锁校验通过（改配置忘重锁会在这转红）。"""
        path = PIPELINES / f"{pack}.yaml"
        assert path.exists(), f"pack {pack!r} 没有 YAML"
        assert (PIPELINES / f"{pack}.lock").exists(), \
            f"pack {pack!r} 没有 lockfile（用 --write-lock 生成）"
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler(pipeline_dir=str(PIPELINES), agents_dir=PROJECT / "agents")
        plan = sched.parse(pack, verify_lock=True)  # 漂移即抛 LockfileMismatchError
        assert plan.node_count >= 2, f"pack {pack!r} 只有 {plan.node_count} 个节点"

    @pytest.mark.parametrize("pack", sorted(PACK_E2E))
    def test_every_pack_cites_a_real_e2e(self, pack):
        """每条 pack 的 E2E 判据必须真实存在，且带至少一个 test_ 方法。"""
        ref = PACK_E2E[pack]
        file_part, cls_name = ref.split("::")
        target = PROJECT / file_part
        assert target.exists(), f"{pack!r} 的判据文件不存在: {file_part}"

        if file_part == _THIS_FILE:
            cls = globals().get(cls_name)
            assert cls is not None, f"{pack!r} 的判据类 {cls_name} 不在本文件"
            meths = [m for m in dir(cls) if m.startswith("test_")]
            assert meths, f"{pack!r} 的判据类 {cls_name} 没有任何 test 方法"
        else:
            assert f"class {cls_name}" in target.read_text(encoding="utf-8"), \
                f"{pack!r} 的判据类 {cls_name} 不在 {file_part}"

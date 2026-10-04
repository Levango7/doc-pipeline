"""HTML 解析后端选择与降级可见性。

锁住三个曾经/将来会静默失效的点：
  1. selectolax 1.0 起 `selectolax.parser` 在导入期主动 raise ImportError，
     旧代码 `from selectolax.parser import HTMLParser` 在生产安装里必然失败，
     却被 `except Exception: pass` 吞成"走正则"，且 `--check` 因只探测顶层包
     仍报 OK。→ 后端可用性必须显式探测，降级必须计数+告警。
  2. 每个可用内核都要真的满足既有提取不变量（标题/去噪/代码缩进），
     否则"换内核"等于"悄悄降质"。
  3. 代码里不得再出现直接 import 某一内核的写法。
"""
import importlib.util
from pathlib import Path

import pytest

from agents import fetcher as fetcher_mod
from pipeline_core import selectolax_compat as compat

ARTICLE_HTML = (
    "<html><head><script>var secret_token=1;</script><style>p{color:red}</style></head><body>"
    "<nav><a href='/'>首页</a><a href='/2'>导航</a></nav>"
    "<article>"
    "<h2>核心结论</h2>"
    "<p>本段正文足够长，密度也足够高，应当被完整提取出来，用于验证 selectolax 各内核行为一致。</p>"
    "<pre><code class='language-python'>def f():\n    return 1</code></pre>"
    "<p>第二段正文同样足够长，覆盖文本密度筛选阈值，应当进入结果集合。</p>"
    "</article>"
    "<footer>版权所有 备案号码</footer>"
    "</body></html>"
)


@pytest.fixture
def agent(tmp_path):
    from pipeline_core.base_agent import AgentMeta
    config = {"temp_dir": str(tmp_path / "tmp"), "quiet": True, "retry": 1}
    return fetcher_mod.FetcherAgent(
        "fetcher", AgentMeta(name="fetcher", version="1.0"), config, None, None)


def _available_kernels() -> list[str]:
    """用生产同一条探测路径判定内核是否真的可用。

    注意：`find_spec("selectolax.parser")` 在 1.0 上仍然返回 spec
    （文件存在），但 import 它会主动 raise ImportError——只有 _load
    的导入式探测才是可信判据。
    """
    return [name for name in compat.MODULES if compat._load(name) is not None]


# ─── 后端探测 ────────────────────────────────────────────

class TestBackendProbe:
    def test_resolve_backend_returns_usable_kernel_when_selectolax_present(self):
        """selectolax 装了但没有可用内核时，必须返回 None（而不是假装 OK）。"""
        if importlib.util.find_spec("selectolax") is None:
            pytest.skip("环境未安装 selectolax")
        backend = compat.resolve_backend()
        assert backend in compat.MODULES
        assert compat._load(backend) is not None

    def test_probe_does_not_trust_top_level_import_only(self, monkeypatch):
        """回归护栏：只 import 顶层包不足以证明解析路径可用。

        复刻 selectolax 1.0 的场景——顶层包可导入、内核不可用。
        """
        monkeypatch.setattr(compat, "_load", lambda name: None)
        assert compat.resolve_backend() is None
        assert compat.get_parser("<html></html>") is None

    def test_environment_can_pin_backend(self, monkeypatch):
        auto = compat.resolve_backend()
        monkeypatch.setenv(compat.ENV_OVERRIDE, "lexbor")
        assert compat.requested_backend() == "lexbor"
        # 非法值退回自动顺序，结果与不设时一致
        monkeypatch.setenv(compat.ENV_OVERRIDE, "not-a-kernel")
        assert compat.resolve_backend() == auto
        # 合法但内核缺失时不得假装可用
        if compat._load("lexbor") is None:
            monkeypatch.setenv(compat.ENV_OVERRIDE, "lexbor")
            assert compat.resolve_backend() != "lexbor"

    def test_fetcher_source_no_direct_kernel_import(self):
        """fetcher 不得再直接 import 某个具体内核（那正是本次静默降级的根因）。"""
        src = Path(fetcher_mod.__file__).read_text(encoding="utf-8")
        assert "from selectolax.parser import" not in src
        assert "from selectolax.lexbor import" not in src


# ─── 每个内核的真实产出 ──────────────────────────────────

class TestKernelExtraction:
    @pytest.mark.parametrize("backend", _available_kernels() or ["modest"])
    def test_kernel_meets_extraction_invariants(self, agent, backend, monkeypatch):
        monkeypatch.setenv(compat.ENV_OVERRIDE, backend)
        monkeypatch.setattr(compat, "_cache", {})
        assert compat.resolve_backend() == backend, "固定后端未生效，测的就不是它"

        text = agent._extract_text(ARTICLE_HTML)
        # 正文两段都在
        assert "本段正文足够长" in text
        assert "第二段正文同样足够长" in text
        # 标题转成 markdown 层级
        assert "## 核心结论" in text
        # 噪声块被移除
        assert "secret_token" not in text
        assert "color:red" not in text
        assert "版权所有" not in text
        # 代码块保留缩进与围栏
        assert "```" in text
        assert "    return 1" in text
        # 未触发降级
        assert agent._parser_fallbacks == 0

    def test_regex_fallback_when_no_kernel(self, agent, monkeypatch):
        """完全没有内核时明确走正则，并且不产生"降级失败"计数噪声。"""
        monkeypatch.setattr(compat, "resolve_backend", lambda: None)
        text = agent._extract_text(ARTICLE_HTML)
        assert "本段正文足够长" in text
        assert agent._parser_fallbacks == 0


# ─── 降级可见性 ──────────────────────────────────────────

class TestFallbackIsVisible:
    """降级信号分两类，都必须可见，但只有"意外失败"计入 fetcher 计数：
      - 环境没有内核 → `_parser_backend == "regex"`（初始化时已如实声明，不算故障）
      - 内核构造/提取抛错 → `_parser_fallbacks` 计数 + 首次告警
    """

    class _Boom:
        def __init__(self, html: str) -> None:
            self._html = html

        def css(self, sel):
            raise RuntimeError("kernel exploded")

        def css_first(self, sel):
            raise RuntimeError("kernel exploded")

    def _force_boom_kernel(self, monkeypatch):
        monkeypatch.setattr(compat, "resolve_backend", lambda: "lexbor")
        monkeypatch.setattr(compat, "_load", lambda name: self._Boom)

    def test_kernel_error_is_counted_and_warned(self, agent, monkeypatch, caplog):
        """内核提取抛错时必须：计数 + 告警 + 回退正则拿到内容。"""
        self._force_boom_kernel(monkeypatch)
        with caplog.at_level("WARNING"):
            text = agent._extract_text(ARTICLE_HTML)
        assert agent._parser_fallbacks == 1
        assert "已降级为正则启发式" in caplog.text
        assert "kernel exploded" in caplog.text
        assert text  # 仍然回退到正则拿到内容

    def test_repeated_failures_warn_once(self, agent, monkeypatch, caplog):
        self._force_boom_kernel(monkeypatch)
        with caplog.at_level("WARNING"):
            agent._extract_text(ARTICLE_HTML)
            agent._extract_text(ARTICLE_HTML)
            agent._extract_text(ARTICLE_HTML)
        assert agent._parser_fallbacks == 3
        assert caplog.text.count("已降级为正则启发式") == 1

    def test_construction_failure_is_warned(self, monkeypatch, caplog):
        """内核类存在但构造失败：compat 必须告警并返回 None，让调用方走正则。"""
        class _BadCtor:
            def __init__(self, html):
                raise ValueError("ctor exploded")

        monkeypatch.setattr(compat, "resolve_backend", lambda: "lexbor")
        monkeypatch.setattr(compat, "_load", lambda name: _BadCtor)
        with caplog.at_level("WARNING"):
            assert compat.get_parser("<html></html>") is None
        assert "构造失败" in caplog.text
        assert "ctor exploded" in caplog.text

"""环境变量名双读判据 —— spec §7.1 env 前缀行（`DOC_PIPELINE_*` → `LOOM_*`）。

改名的对外风险面只有一个：MAOP 侧按 spec §7.3 第 6 步的约定设置
`DOC_PIPELINE_ROOT`（它侧零改动），所以旧名必须回退可读。判据钉三件事：

  1. 优先序：LOOM_* 压过 DOC_PIPELINE_*（新部署语义）；
  2. 回退：只设旧名依旧生效（MAOP 兼容）；
  3. 迁移提示只记一次（双读会被高频调用，不复读刷屏）。
"""
import importlib
import logging
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(PROJECT))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _clean(monkeypatch):
    """清探针名 + 重置"已警告"缓存（warning-once 判据的前置）。"""
    from pipeline_core import envs
    envs.reset_warning_cache()
    for name in ("LOOM_PROBE", "DOC_PIPELINE_PROBE"):
        monkeypatch.delenv(name, raising=False)
    return envs


class TestEnvsRead:
    def test_default_when_absent(self, monkeypatch):
        envs = _clean(monkeypatch)
        assert envs.read("PROBE", "fallback") == "fallback"

    def test_new_name_wins(self, monkeypatch):
        envs = _clean(monkeypatch)
        monkeypatch.setenv("DOC_PIPELINE_PROBE", "legacy")
        monkeypatch.setenv("LOOM_PROBE", "new")
        assert envs.read("PROBE") == "new"

    def test_legacy_fallback(self, monkeypatch):
        envs = _clean(monkeypatch)
        monkeypatch.setenv("DOC_PIPELINE_PROBE", "legacy")
        assert envs.read("PROBE") == "legacy"

    def test_legacy_warning_once(self, monkeypatch):
        envs = _clean(monkeypatch)
        monkeypatch.setenv("DOC_PIPELINE_PROBE", "legacy")
        cap = _Capture()
        logging.getLogger("pipeline_core.envs").addHandler(cap)
        try:
            envs.read("PROBE")
            envs.read("PROBE")
            envs.read("PROBE")
        finally:
            logging.getLogger("pipeline_core.envs").removeHandler(cap)
        assert len(cap.messages) == 1
        assert "DOC_PIPELINE_PROBE" in cap.messages[0]
        assert "LOOM_PROBE" in cap.messages[0]

    def test_empty_string_is_a_value(self, monkeypatch):
        """空串按"显式设置过"处理（与 os.environ 语义一致，调用方自己 strip）。"""
        envs = _clean(monkeypatch)
        monkeypatch.setenv("LOOM_PROBE", "")
        assert envs.read("PROBE", "fallback") == ""


class TestStatePathsDualRead:
    """运行态目录解析：新名优先、旧名回退（MAOP 类部署的真实路径）。"""

    def _state_paths(self):
        return importlib.import_module("pipeline_core.state_paths")

    def test_new_name(self, monkeypatch, tmp_path):
        sp = self._state_paths()
        monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "new"))
        assert sp.state_root() == tmp_path / "new"

    def test_legacy_fallback(self, monkeypatch, tmp_path):
        sp = self._state_paths()
        monkeypatch.delenv("LOOM_STATE_DIR", raising=False)
        monkeypatch.setenv("DOC_PIPELINE_STATE_DIR", str(tmp_path / "old"))
        assert sp.state_root() == tmp_path / "old"

    def test_new_wins_over_legacy(self, monkeypatch, tmp_path):
        sp = self._state_paths()
        monkeypatch.setenv("DOC_PIPELINE_STATE_DIR", str(tmp_path / "old"))
        monkeypatch.setenv("LOOM_STATE_DIR", str(tmp_path / "new"))
        assert sp.state_root() == tmp_path / "new"

    def test_versions_dir_dual_read(self, monkeypatch, tmp_path):
        sp = self._state_paths()
        monkeypatch.delenv("LOOM_VERSIONS_DIR", raising=False)
        monkeypatch.setenv("DOC_PIPELINE_VERSIONS_DIR", str(tmp_path / "v-old"))
        assert sp.versions_root() == str(tmp_path / "v-old")
        monkeypatch.setenv("LOOM_VERSIONS_DIR", str(tmp_path / "v-new"))
        assert sp.versions_root() == str(tmp_path / "v-new")

    def test_env_constants_are_new_names(self):
        sp = self._state_paths()
        assert sp.STATE_DIR_ENV == "LOOM_STATE_DIR"
        assert sp.VERSIONS_DIR_ENV == "LOOM_VERSIONS_DIR"
        assert sp.LEGACY_STATE_DIR_ENV == "DOC_PIPELINE_STATE_DIR"


class TestRootAndWebhooksDualRead:
    def test_paths_root_new_and_legacy(self, monkeypatch, tmp_path):
        import pipeline_core.paths as paths
        assert paths.ROOT_ENV == "LOOM_ROOT"
        assert paths.LEGACY_ROOT_ENV == "DOC_PIPELINE_ROOT"

        new_root = tmp_path / "new-root"
        (new_root / "pipelines").mkdir(parents=True)
        (new_root / "pipelines" / "x.yaml").write_text("name: x\n", encoding="utf-8")
        old_root = tmp_path / "old-root"
        monkeypatch.setenv("LOOM_ROOT", str(new_root))
        monkeypatch.setenv("DOC_PIPELINE_ROOT", str(old_root))
        assert paths.project_root() == new_root
        assert paths.pipelines_dir() == new_root / "pipelines"

        monkeypatch.delenv("LOOM_ROOT")
        assert paths.project_root() == old_root  # MAOP 兼容：旧名回退

    def test_webhooks_file_env(self, monkeypatch):
        from pipeline_core import webhooks
        assert webhooks.CONFIG_PATH_ENV == "LOOM_WEBHOOKS_FILE"
        assert webhooks.LEGACY_CONFIG_PATH_ENV == "DOC_PIPELINE_WEBHOOKS_FILE"
        monkeypatch.delenv("LOOM_WEBHOOKS_FILE", raising=False)
        monkeypatch.setenv("DOC_PIPELINE_WEBHOOKS_FILE", "old.yaml")
        assert webhooks.webhooks_file_env() == "old.yaml"
        monkeypatch.setenv("LOOM_WEBHOOKS_FILE", "new.yaml")
        assert webhooks.webhooks_file_env() == "new.yaml"


class TestIdentityStrings:
    """MCP/观测标识（spec §7.1 同名行）：serverInfo.name 与指标/日志前缀。"""

    def test_mcp_server_name_is_loom(self):
        from pipeline_core.mcp_server import SERVER_NAME
        assert SERVER_NAME == "loom"

    def test_metrics_namespace_is_loom(self):
        from pipeline_core.observability import MetricsRegistry
        reg = MetricsRegistry()
        assert reg.counter("requests") == "loom_requests"

    def test_logger_app_name_is_loom(self):
        import inspect

        from pipeline_core.observability import StructuredLogger
        sig = inspect.signature(StructuredLogger.__init__)
        assert sig.parameters["app_name"].default == "loom"

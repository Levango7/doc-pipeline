"""tests/test_bootstrap.py — 启动自检（StartupReport + 检查项）。"""


from pipeline_core.bootstrap import (
    CheckResult,
    StartupReport,
    quick_check,
    run_startup_check,
)


class TestStartupReport:
    def test_empty_report_no_errors(self):
        r = StartupReport()
        assert not r.has_errors
        assert r.ok_count == 0
        assert r.error_count == 0

    def test_add_check_and_count(self):
        r = StartupReport()
        r.add(CheckResult("a", "ok"))
        r.add(CheckResult("b", "warn"))
        r.add(CheckResult("c", "error"))
        assert r.ok_count == 1
        assert r.warn_count == 1
        assert r.error_count == 1
        assert r.has_errors
        assert r.has_warnings

    def test_summary_contains_status_line(self):
        r = StartupReport()
        r.add(CheckResult("py", "ok", "3.14"))
        s = r.summary()
        assert "OK" in s
        assert "py" in s

    def test_to_dict_structure(self):
        r = StartupReport()
        r.add(CheckResult("x", "ok", "fine"))
        d = r.to_dict()
        assert d["ok"] == 1
        assert d["has_errors"] is False
        assert d["checks"][0]["name"] == "x"


class TestStartupCheck:
    def test_run_returns_report(self, tmp_path):
        report = run_startup_check(project_root=str(tmp_path))
        assert isinstance(report, StartupReport)
        assert len(report.checks) > 0

    def test_quick_check_bool(self, tmp_path):
        # 无 .env / 无依赖时可能有 warn 但不应有 error（目录结构检查会失败）
        result = quick_check(project_root=str(tmp_path))
        assert isinstance(result, bool)

    def test_python_version_check_passes(self):
        report = StartupReport()
        from pipeline_core.bootstrap import _check_python_version
        _check_python_version(report)
        py_checks = [c for c in report.checks if c.name == "Python 版本"]
        assert len(py_checks) == 1
        assert py_checks[0].status in ("ok", "warn")

    def test_project_structure_missing_dir(self, tmp_path):
        report = StartupReport()
        from pipeline_core.bootstrap import _check_project_structure
        _check_project_structure(report, tmp_path)
        # 缺少 pipelines/ 等目录应报 error
        assert any(c.is_error for c in report.checks)

    def test_env_security_no_env(self, tmp_path):
        report = StartupReport()
        from pipeline_core.bootstrap import _check_env_security
        _check_env_security(report, tmp_path)
        # 无 .env 文件 → 不添加任何 check（早退）
        env_checks = [c for c in report.checks if c.name == ".env 安全"]
        assert env_checks == []

    def test_env_security_with_real_key(self, tmp_path):
        (tmp_path / ".env").write_text("BOCHA_API_KEY=sk-realkey123\n", encoding="utf-8")
        report = StartupReport()
        from pipeline_core.bootstrap import _check_env_security
        _check_env_security(report, tmp_path)
        env_checks = [c for c in report.checks if c.name == ".env 安全"]
        assert len(env_checks) == 1
        assert env_checks[0].status == "warn"

    def test_llm_without_credentials_is_warn_not_error(self, monkeypatch):
        """CI 直接采信 --check 的退出码，所以"没配 Key"必须是 warn 而不是 error。

        否则 keyless 环境永远 rc=1，CI 就只能像历史上那样不去读 rc、改用 grep
        一行文案来判断自检——那个 grep 只挡住了 selectolax 降级这一类故障。
        """
        import pipeline_core.llm_router as llm
        from pipeline_core.bootstrap import _check_llm_router

        class _NoProviderRouter:
            def get_active_providers(self):
                return []

        monkeypatch.setattr(llm, "get_router", lambda *a, **k: _NoProviderRouter())
        report = StartupReport()
        _check_llm_router(report)
        check = [c for c in report.checks if c.name == "LLM 路由器"][0]
        assert check.status == "warn", f"缺凭据不该算结构性故障：{check.status} / {check.message}"
        assert not report.has_errors, "keyless 自检必须 rc=0，CI 才敢采信退出码"

    def test_llm_router_crash_stays_error(self, monkeypatch):
        """缺凭据降级为 warn，但路由器真的炸了仍必须是 error——两者不能混为一谈。"""
        import pipeline_core.llm_router as llm
        from pipeline_core.bootstrap import _check_llm_router

        def _boom(*a, **k):
            raise RuntimeError("初始化不了")

        monkeypatch.setattr(llm, "get_router", _boom)
        report = StartupReport()
        _check_llm_router(report)
        check = [c for c in report.checks if c.name == "LLM 路由器"][0]
        assert check.status == "error"
        assert report.has_errors

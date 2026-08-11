"""Admin API handler — cancel/pause/resume 路由测试"""
from unittest.mock import MagicMock, patch

import pytest

from pipeline_core.admin_api import AdminHandler


class TestAdminHandlers:
    """直接测试 handler 方法（绕过 HTTP socket 初始化）"""

    @pytest.fixture
    def handler(self):
        h = AdminHandler.__new__(AdminHandler)
        h.orch = MagicMock()
        h.orch.cancel.return_value = True
        h.orch.pause.return_value = True
        h.orch.resume.return_value = True
        h._json = MagicMock()
        return h

    # ── Bug 2: cancel_task 调错方法名 ──

    def test_cancel_calls_orch_cancel_method(self, handler):
        """_handle_cancel_task 应调 orch.cancel() 而不是不存在的 cancel_task()"""
        handler._handle_cancel_task("task-001")
        handler.orch.cancel.assert_called_once_with("task-001")
        handler._json.assert_called_once_with({"cancelled": True, "task_id": "task-001"})

    def test_cancel_returns_false(self, handler):
        handler.orch.cancel.return_value = False
        handler._handle_cancel_task("task-missing")
        handler._json.assert_called_once_with({"cancelled": False, "task_id": "task-missing"})

    # ── Bug 3: pause/resume 是空操作 ──

    def test_pause_calls_orch_pause(self, handler):
        """_handle_pause_task 应调 orch.pause()"""
        handler._handle_pause_task("task-002")
        handler.orch.pause.assert_called_once_with("task-002")
        handler._json.assert_called_once_with({"paused": True, "task_id": "task-002"})

    def test_pause_returns_false(self, handler):
        handler.orch.pause.return_value = False
        handler._handle_pause_task("task-not-running")
        handler._json.assert_called_once_with({"paused": False, "task_id": "task-not-running"})

    def test_resume_calls_orch_resume(self, handler):
        """_handle_resume_task 应调 orch.resume()"""
        handler._handle_resume_task("task-003")
        handler.orch.resume.assert_called_once_with("task-003")
        handler._json.assert_called_once_with({"resumed": True, "task_id": "task-003"})

    def test_resume_returns_false(self, handler):
        handler.orch.resume.return_value = False
        handler._handle_resume_task("task-not-paused")
        handler._json.assert_called_once_with({"resumed": False, "task_id": "task-not-paused"})

    # ── 边界条件：orch 未设置 ──

    def test_no_orch_returns_500(self):
        handler = AdminHandler.__new__(AdminHandler)
        handler.orch = None
        handler._json = MagicMock()

        handler._handle_cancel_task("t1")
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 500

        handler._json.reset_mock()
        handler._handle_pause_task("t2")
        assert handler._json.call_args[0][1] == 500

        handler._json.reset_mock()
        handler._handle_resume_task("t3")
        assert handler._json.call_args[0][1] == 500

    # ── POST /api/tasks 提交新任务 ──

    def test_submit_task_missing_query(self, handler):
        """缺少 query 字段应返回 400"""
        handler._handle_submit_task(b'{"title": "test"}')
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    def test_submit_task_empty_query(self, handler):
        """空 query 应返回 400"""
        handler._handle_submit_task(b'{"query": ""}')
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    def test_submit_task_invalid_json(self, handler):
        """非法 JSON body 应返回 400"""
        handler._handle_submit_task(b'not json')
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    def test_submit_task_no_orch(self):
        """orch 未设置应返回 500"""
        h = AdminHandler.__new__(AdminHandler)
        h.orch = None
        h._json = MagicMock()
        h._handle_submit_task(b'{"query": "test"}')
        h._json.assert_called_once()
        assert h._json.call_args[0][1] == 500

    # ── GET /tasks/<id> 增强结果 ──

    def test_get_task_not_found(self, handler):
        """任务不存在应返回 404"""
        handler.orch.get_task.return_value = None
        handler._handle_get_task("nonexistent")
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 404

    def test_get_task_no_orch(self):
        """orch 未设置应返回 500"""
        h = AdminHandler.__new__(AdminHandler)
        h.orch = None
        h._json = MagicMock()
        h._handle_get_task("t1")
        h._json.assert_called_once()
        assert h._json.call_args[0][1] == 500

    # ── GET /api/cost 成本统计 ──

    def test_cost_stats_returns_summary(self, handler):
        """_handle_cost_stats 应返回 cost_tracker summary"""
        with patch("pipeline_core.cost_tracker.get_cost_tracker") as mock_ct:
            mock_ct.return_value.summary.return_value = {"total_cost": 1.5}
            handler._handle_cost_stats()
            handler._json.assert_called_once_with({"total_cost": 1.5})

    def test_cost_stats_on_error_returns_500(self, handler):
        with patch("pipeline_core.cost_tracker.get_cost_tracker") as mock_ct:
            mock_ct.return_value.summary.side_effect = RuntimeError("boom")
            handler._handle_cost_stats()
            handler._json.assert_called_once()
            assert handler._json.call_args[0][1] == 500

    # ── POST /api/cost/budget 设置预算 ──

    def test_set_budget_valid(self, handler):
        with patch("pipeline_core.cost_tracker.get_cost_tracker") as mock_ct:
            handler._handle_set_budget(b'{"max_cost": 100.0}')
            mock_ct.return_value.set_budget.assert_called_once_with(100.0)
            handler._json.assert_called_once_with({"budget_set": True, "max_cost": 100.0})

    def test_set_budget_invalid_json(self, handler):
        handler._handle_set_budget(b'not json')
        handler._json.assert_called_once()
        assert handler._json.call_args[0][1] == 400

    # ── POST /api/config/reload 配置热更新 ──

    def test_config_reload_no_orch(self):
        h = AdminHandler.__new__(AdminHandler)
        h.orch = None
        h._json = MagicMock()
        h._handle_config_reload()
        h._json.assert_called_once_with({"error": "orchestrator not set"}, 500)

    def test_config_reload_success(self, handler):
        handler.orch.config.reload = MagicMock()
        agent_info = {"name": "researcher"}
        handler.orch.registry.list.return_value = [agent_info]
        mock_agent = MagicMock()
        mock_agent.on_config_update = MagicMock()
        handler.orch.registry.get_instance.return_value = mock_agent
        handler._handle_config_reload()
        handler.orch.config.reload.assert_called_once()
        mock_agent.on_config_update.assert_called_once()
        result = handler._json.call_args[0][0]
        assert result["reloaded"] is True
        assert result["agents_notified"] == 1

    # ── GET /api/alerts 告警查询 ──

    def test_alerts_returns_list(self, handler):
        handler.path = "/api/alerts?level=warning&limit=10"
        with patch("pipeline_core.alert_manager.get_alerts") as mock_ga:
            mock_ga.return_value = [{"level": "warning", "message": "test"}]
            handler._handle_alerts()
            mock_ga.assert_called_once()
            kwargs = mock_ga.call_args.kwargs
            assert kwargs["level"] == "warning"
            assert kwargs["limit"] == 10
            result = handler._json.call_args[0][0]
            assert result["alerts"] == [{"level": "warning", "message": "test"}]

    # ── GET /api/logs 日志查询 ──

    def test_logs_no_log_dir(self, handler):
        """logs 目录不存在时返回空列表"""
        handler.path = "/api/logs?level=error&limit=50"
        with patch("pipeline_core.admin_api.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            handler._handle_logs()
            handler._json.assert_called_once_with({"logs": [], "count": 0})

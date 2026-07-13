"""Admin API handler — cancel/pause/resume 路由测试"""
from unittest.mock import MagicMock
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
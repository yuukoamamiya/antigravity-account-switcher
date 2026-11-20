import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import ConfigError
from switcher_ui import SwitcherApp


class CapturingLifecycle:
    def __init__(self):
        self.name = None
        self.target = None

    def start(self, name, target):
        self.name = name
        self.target = target


def make_app(**overrides):
    app = SwitcherApp.__new__(SwitcherApp)
    config = {"queryQuota": True, "refreshOnStartup": False, "quotaMaxWorkers": 1}
    app.core = SimpleNamespace(config=config, quota=Mock())
    app.events = queue.Queue()
    app.lifecycle = CapturingLifecycle()
    app.root = Mock()
    app.closing = False
    app.switching = False
    app.adding_current = False
    app.quota_refreshing = False
    app.account_refreshing = set()
    app.configuring_proxy = False
    app.accounts = []
    app.update_card = Mock()
    app.save_cache = Mock()
    app.set_quota_status = Mock()
    app.write_log = Mock()
    app.quota_successes = 0
    app.quota_failures = 0
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def synthetic_account(account_id="acc-1"):
    return SimpleNamespace(account_id=account_id, email=f"{account_id}@example.invalid")


class StartupRefreshOptionTests(unittest.TestCase):
    def test_should_refresh_on_startup_requires_flag_quota_and_accounts(self):
        accounts = [synthetic_account()]
        self.assertTrue(SwitcherApp.should_refresh_on_startup({"queryQuota": True, "refreshOnStartup": True}, accounts))
        self.assertFalse(SwitcherApp.should_refresh_on_startup({"queryQuota": True, "refreshOnStartup": False}, accounts))
        self.assertFalse(SwitcherApp.should_refresh_on_startup({"queryQuota": False, "refreshOnStartup": True}, accounts))
        self.assertFalse(SwitcherApp.should_refresh_on_startup({"queryQuota": True, "refreshOnStartup": True}, []))

    def test_toggle_persists_and_updates_shared_config(self):
        app = make_app()
        app.startup_refresh_var = Mock()
        app.startup_refresh_var.get.return_value = True
        with patch("switcher_ui.save_config_flag", return_value=True) as save:
            app.toggle_startup_refresh()
        save.assert_called_once_with("refreshOnStartup", True)
        self.assertIs(app.core.config["refreshOnStartup"], True)
        app.write_log.assert_called_once_with("启动时刷新已开启。")

    def test_toggle_rolls_back_and_reports_save_errors(self):
        app = make_app()
        app.startup_refresh_var = Mock()
        app.startup_refresh_var.get.return_value = True
        with patch("switcher_ui.save_config_flag", side_effect=ConfigError("synthetic refusal")), \
             patch("switcher_ui.messagebox.showerror") as showerror:
            app.toggle_startup_refresh()
        app.startup_refresh_var.set.assert_called_once_with(False)
        showerror.assert_called_once()
        self.assertIs(app.core.config["refreshOnStartup"], False)
        app.write_log.assert_not_called()


class SingleAccountRefreshTests(unittest.TestCase):
    def test_guards_prevent_duplicate_or_conflicting_refreshes(self):
        account = synthetic_account()
        for state in ({"closing": True}, {"quota_refreshing": True}, {"configuring_proxy": True}, {"adding_current": True}):
            app = make_app(**state)
            app.refresh_account(account)
            self.assertIsNone(app.lifecycle.target)
            self.assertEqual(app.account_refreshing, set())
            app.write_log.assert_not_called()

        app = make_app(account_refreshing={account.account_id})
        app.refresh_account(account)
        self.assertIsNone(app.lifecycle.target)
        app.write_log.assert_not_called()
        app.update_card.assert_not_called()

    def test_disabled_quota_queries_block_single_account_refresh(self):
        app = make_app()
        app.core.config["queryQuota"] = False
        account = synthetic_account()

        app.refresh_account(account)

        self.assertIsNone(app.lifecycle.target)
        app.core.quota.assert_not_called()
        self.assertEqual(app.account_refreshing, set())
        app.update_card.assert_not_called()
        app.write_log.assert_not_called()

    def test_enabled_quota_queries_allow_single_account_refresh(self):
        app = make_app()
        account = synthetic_account()
        app.core.quota.return_value = {
            "id": account.account_id, "success": True, "status": "ok", "attemptedAt": "2026-01-01T00:00:00Z",
            "fiveHour": None, "weekly": None, "error": None,
        }

        app.refresh_account(account)

        self.assertEqual(app.lifecycle.name, "agy-quota-single")
        self.assertIn(account.account_id, app.account_refreshing)

    def test_refresh_runs_worker_and_emits_result_before_done(self):
        app = make_app()
        account = synthetic_account()
        result = {"id": account.account_id, "success": True, "status": "ok", "attemptedAt": "2026-01-01T00:00:00Z",
                  "fiveHour": None, "weekly": None, "error": None}
        app.core.quota.return_value = result

        app.refresh_account(account)

        self.assertIn(account.account_id, app.account_refreshing)
        self.assertEqual(app.lifecycle.name, "agy-quota-single")
        app.update_card.assert_called_once_with(account.account_id)
        app.lifecycle.target(threading.Event())
        app.core.quota.assert_called_once_with(account)
        first = app.events.get_nowait()
        second = app.events.get_nowait()
        self.assertEqual(first, ("quota_result", result))
        self.assertEqual(second, ("quota_single_done", account.account_id))
        self.assertTrue(app.events.empty())

    def test_worker_failure_turns_into_classified_quota_result(self):
        app = make_app()
        account = synthetic_account()
        app.core.quota.side_effect = RuntimeError("synthetic failure")

        app.refresh_account(account)
        app.lifecycle.target(threading.Event())

        kind, result = app.events.get_nowait()
        self.assertEqual(kind, "quota_result")
        self.assertEqual(result["id"], account.account_id)
        self.assertIs(result["success"], False)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"], "synthetic failure")
        self.assertIsNone(result["fiveHour"])
        self.assertIsNone(result["weekly"])
        self.assertTrue(result["attemptedAt"])
        self.assertEqual(app.events.get_nowait(), ("quota_single_done", account.account_id))

    def test_quota_single_done_clears_state_and_reports_status(self):
        app = make_app(account_refreshing={"acc-1"})
        app.events.put(("quota_single_done", "acc-1"))

        app.poll_events()

        self.assertEqual(app.account_refreshing, set())
        app.save_cache.assert_called_once_with()
        app.update_card.assert_called_with("acc-1")
        status = app.set_quota_status.call_args.args[0]
        self.assertIn("已刷新账号 acc-1", status)
        app.write_log.assert_called_with("账号 acc-1 的单账号额度刷新已完成。")

    def test_switch_is_blocked_while_target_account_is_refreshing(self):
        account = synthetic_account()
        app = make_app(account_refreshing={account.account_id}, current_id=None)
        app.write_log = Mock()
        app.switch_account(account)
        self.assertFalse(app.switching)
        app.write_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()

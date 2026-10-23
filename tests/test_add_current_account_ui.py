import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from account_core import Account
from switcher_ui import SwitcherApp


class CapturingLifecycle:
    def __init__(self):
        self.target = None

    def start(self, _name, target):
        self.target = target


def account(account_id="7", email="sample@example.invalid"):
    payload = {"token": {"access_token": "sim-access", "refresh_token": "sim-refresh"}}
    return Account(account_id, email, Path("simulated.json"), Path("simulated.meta.json"), payload, b"{}", "sim-digest")


def app_with(core, accounts=None):
    app = SwitcherApp.__new__(SwitcherApp)
    app.core = core
    app.accounts = list(accounts or [])
    app.cards = {item.account_id: {"account": item, "items": {}} for item in app.accounts}
    app.cache = {}
    app.events = queue.Queue()
    app.lifecycle = CapturingLifecycle()
    app.closing = False
    app.switching = False
    app.adding_current = False
    app.current_id = None
    app.add_account_button = Mock()
    app.root = Mock()
    app.write_log = Mock()
    app.build_card = Mock(side_effect=lambda item: app.cards.__setitem__(item.account_id, {"account": item, "items": {}}))
    app.redraw_cards = Mock()
    app.update_cards = Mock()
    app.cache_item = lambda key: app.cache.setdefault(key, {})
    app.refresh_button = Mock()
    app.set_quota_status = Mock()
    app.save_cache = Mock()
    app.quota_refreshing = False
    app.quota_successes = 0
    app.quota_failures = 0
    return app


class AddCurrentAccountUiTests(unittest.TestCase):
    def dispatch_worker(self, app):
        app.lifecycle.target(threading.Event())
        app.poll_events()

    def test_success_updates_accounts_and_current_on_ui_thread(self):
        imported = account()
        core = SimpleNamespace(add_current_account=Mock(return_value=(imported, True)))
        app = app_with(core)
        app.add_current_account()
        self.assertTrue(app.adding_current)
        with patch("switcher_ui.messagebox.showinfo"):
            self.dispatch_worker(app)
        self.assertEqual(app.accounts, [imported])
        self.assertEqual(app.current_id, "7")
        self.assertFalse(app.adding_current)
        app.build_card.assert_called_once_with(imported)

    def test_duplicate_preserves_existing_account_object(self):
        existing = account()
        returned = account(email="replacement@example.invalid")
        core = SimpleNamespace(add_current_account=Mock(return_value=(returned, False)))
        app = app_with(core, [existing])
        app.add_current_account()
        with patch("switcher_ui.messagebox.showinfo"):
            self.dispatch_worker(app)
        self.assertIs(app.accounts[0], existing)
        self.assertEqual(app.current_id, "7")
        app.build_card.assert_not_called()

    def test_failure_restores_button_and_operation_flag(self):
        core = SimpleNamespace(add_current_account=Mock(side_effect=ValueError("simulated invalid credential")))
        app = app_with(core)
        app.add_current_account()
        with patch("switcher_ui.messagebox.showerror"):
            self.dispatch_worker(app)
        self.assertFalse(app.adding_current)
        app.add_account_button.configure.assert_any_call(state="normal")

    def test_add_and_switch_guards_prevent_conflicting_workers(self):
        core = SimpleNamespace(add_current_account=Mock())
        app = app_with(core)
        app.switching = True
        app.add_current_account()
        self.assertIsNone(app.lifecycle.target)
        app.switching = False
        app.adding_current = True
        app.switch_account(account())
        self.assertIsNone(app.lifecycle.target)
        app.adding_current = False
        app.closing = True
        app.add_current_account()
        self.assertIsNone(app.lifecycle.target)


if __name__ == "__main__":
    unittest.main()

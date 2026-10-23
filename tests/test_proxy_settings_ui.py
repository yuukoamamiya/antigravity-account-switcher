import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from switcher_ui import SwitcherApp


class CapturingLifecycle:
    def __init__(self):
        self.target = None

    def start(self, _name, target):
        self.target = target


def make_app(proxy=""):
    app = SwitcherApp.__new__(SwitcherApp)
    config = {"proxy": proxy, "queryQuota": True, "quotaMaxWorkers": 1}
    app.core = SimpleNamespace(config=config, quota=SimpleNamespace(config=config))
    app.events = queue.Queue()
    app.lifecycle = CapturingLifecycle()
    app.root = Mock()
    app.closing = False
    app.switching = False
    app.adding_current = False
    app.quota_refreshing = False
    app.configuring_proxy = False
    app.accounts = []
    app.refresh_button = Mock()
    app.add_account_button = Mock()
    app.proxy_settings_button = Mock()
    app.set_quota_status = Mock()
    app.write_log = Mock()
    app.quota_successes = 0
    app.quota_failures = 0
    return app


class ProxySettingsUiTests(unittest.TestCase):
    def test_cancel_leaves_runtime_and_does_not_persist(self):
        app = make_app("http://127.0.0.1:10809")
        with patch("switcher_ui.simpledialog.askstring", return_value=None), patch("switcher_ui.save_proxy_setting") as save:
            app.configure_proxy()
        save.assert_not_called()
        self.assertEqual(app.core.config["proxy"], "http://127.0.0.1:10809")
        self.assertFalse(app.configuring_proxy)

    def test_invalid_input_shows_error_and_keeps_runtime_for_retry(self):
        app = make_app("http://old.example:8080")
        with patch("switcher_ui.simpledialog.askstring", return_value="socks5://127.0.0.1:1080"), patch("switcher_ui.save_proxy_setting") as save, patch("switcher_ui.messagebox.showerror") as error:
            app.configure_proxy()
        save.assert_not_called()
        error.assert_called_once()
        self.assertEqual(app.core.config["proxy"], "http://old.example:8080")
        self.assertFalse(app.configuring_proxy)

    def test_remote_password_is_never_prefilled(self):
        self.assertEqual(SwitcherApp.proxy_input_value("http://user:simulated-secret@proxy.example:8080"), "")
        self.assertEqual(SwitcherApp.proxy_input_value("http://user:simulated-secret@127.0.0.1:10809"), "")
        self.assertEqual(SwitcherApp.proxy_input_value("http://127.0.0.1:10809"), "10809")

    def test_saved_setting_updates_shared_config_used_by_next_refresh(self):
        app = make_app()
        with patch("switcher_ui.simpledialog.askstring", return_value="10809"), patch("switcher_ui.save_proxy_setting", return_value="http://127.0.0.1:10809"), patch("switcher_ui.messagebox.showinfo"):
            app.configure_proxy()
        self.assertEqual(app.core.config["proxy"], "http://127.0.0.1:10809")
        self.assertIs(app.core.quota.config, app.core.config)
        app.start_quota_refresh()
        batch = Mock()
        with patch("switcher_ui.run_quota_batch", batch):
            app.lifecycle.target(threading.Event())
        self.assertIs(batch.call_args.args[1], app.core.quota)
        self.assertEqual(app.core.quota.config["proxy"], "http://127.0.0.1:10809")

    def test_refresh_closing_and_other_operations_guard_proxy_entry(self):
        for state in ({"quota_refreshing": True}, {"closing": True}, {"switching": True}, {"adding_current": True}):
            app = make_app()
            for key, value in state.items():
                setattr(app, key, value)
            with patch("switcher_ui.simpledialog.askstring") as ask, patch("switcher_ui.save_proxy_setting") as save:
                app.configure_proxy()
            ask.assert_not_called()
            save.assert_not_called()
        app = make_app()
        app.configuring_proxy = True
        app.start_quota_refresh()
        self.assertIsNone(app.lifecycle.target)

    def test_closing_while_dialog_is_open_discards_returned_value(self):
        app = make_app()

        def close_during_dialog(*_args, **_kwargs):
            app.closing = True
            return "10809"

        with patch("switcher_ui.simpledialog.askstring", side_effect=close_during_dialog), patch("switcher_ui.save_proxy_setting") as save:
            app.configure_proxy()
        save.assert_not_called()
        self.assertEqual(app.core.config["proxy"], "")

    def test_operation_buttons_and_theme_include_proxy_control(self):
        app = make_app()
        app.update_operation_buttons()
        app.proxy_settings_button.configure.assert_called_with(state="normal")
        app.quota_refreshing = True
        app.update_operation_buttons()
        app.proxy_settings_button.configure.assert_called_with(state="disabled")
        app.quota_refreshing = False
        app.configuring_proxy = True
        app.update_operation_buttons()
        app.add_account_button.configure.assert_called_with(state="disabled")
        app.refresh_button.configure.assert_called_with(state="disabled")
        app.proxy_settings_button.configure.assert_called_with(state="disabled")
        app.closing = True
        app.update_operation_buttons()
        app.proxy_settings_button.configure.assert_called_with(state="disabled")

        for name in ("root", "header", "summary", "body", "footer", "logo", "title_label", "quota_status", "canvas", "log_text", "log_title", "scrollbar", "refresh_button", "add_account_button", "proxy_settings_button"):
            setattr(app, name, Mock())
        app.summary_labels = []
        app.redraw_cards = Mock()
        app.dark = False
        app.apply_theme()
        app.proxy_settings_button.configure.assert_any_call(bg=app.colors["panel"], fg=app.colors["text"], activebackground=app.colors["border"], activeforeground=app.colors["text"])


if __name__ == "__main__":
    unittest.main()

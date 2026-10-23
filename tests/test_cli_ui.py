import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import antigravity_account_switcher as entrypoint
from switcher_ui import SwitcherApp


class CliAndUiGuardTests(unittest.TestCase):
    def test_help_does_not_read_configuration_or_acquire_mutex(self):
        output = io.StringIO()
        with patch.object(entrypoint, "load_config") as load_config, patch.object(entrypoint, "SingleInstance") as mutex:
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                entrypoint.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        load_config.assert_not_called()
        mutex.assert_not_called()

    def test_closing_gui_rejects_switch_and_quota_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_core = SimpleNamespace(
                config={"logFile": str(Path(directory) / "simulated.log"), "queryQuota": True},
                current_account_id=lambda _accounts: None,
                credentials_dir=Path(directory),
            )
            with patch("switcher_ui.tk.Tk", return_value=Mock()), \
                    patch.object(SwitcherApp, "system_dark", return_value=False), \
                    patch.object(SwitcherApp, "build_ui"), \
                    patch.object(SwitcherApp, "apply_theme"), \
                    patch.object(SwitcherApp, "update_cards"), \
                    patch.object(SwitcherApp, "install_native_drag_guard"), \
                    patch.object(SwitcherApp, "write_log"):
                app = SwitcherApp(fake_core, [], {})
            app.closing = True
            app.lifecycle.start = Mock()
            app.switch_account(SimpleNamespace(account_id="simulated", email="simulated@example.invalid"))
            app.start_quota_refresh()
            app.lifecycle.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

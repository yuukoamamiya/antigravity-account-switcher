import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from portable_runtime import application_root, portable_paths


class PortableRuntimeTests(unittest.TestCase):
    def test_frozen_root_tracks_executable_not_cwd_or_bundle_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            moved = Path(directory) / "Portable Folder"
            moved.mkdir()
            exe = moved / "AntigravityAccountSwitcherPortable.exe"
            self.assertEqual(application_root(frozen=True, executable=exe), moved.resolve())
            paths = portable_paths(moved)
            self.assertEqual(paths["config"], moved / "Data/settings/account-switcher.config.json")

    def test_existing_legacy_local_config_is_read_in_place_then_new_data_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_config = root / "Data/settings/account-switcher.config.json"
            local_config = root / "local-data/account-switcher.config.json"
            legacy = root / "account-switcher.config.json"
            local_config.parent.mkdir(parents=True)
            local_config.write_text(json.dumps({"antigravityExecutable": "legacy.exe"}), encoding="utf-8")
            with patch.multiple(config, CONFIG_PATH=data_config, LOCAL_CONFIG_PATH=local_config, LEGACY_CONFIG_PATH=legacy):
                loaded = config.load_config(environ={})
                self.assertEqual(loaded["antigravityExecutable"], "legacy.exe")
                self.assertEqual(config.active_config_path(), local_config)
                data_config.parent.mkdir(parents=True)
                data_config.write_text(json.dumps({"antigravityExecutable": "new.exe"}), encoding="utf-8")
                self.assertEqual(config.load_config(environ={})["antigravityExecutable"], "new.exe")
                self.assertTrue(local_config.exists())

    def test_smoke_flag_precedes_windows_and_user_resource_initialization(self):
        import antigravity_account_switcher as entrypoint
        with patch.object(entrypoint, "run_portable_smoke", return_value=0) as smoke, \
             patch.object(entrypoint, "SingleInstance", side_effect=AssertionError("mutex touched")), \
             patch.object(entrypoint.sys, "platform", "linux"):
            self.assertEqual(entrypoint.main(["--smoke-test", "--smoke-test-report", "ignored.json"]), 0)
            smoke.assert_called_once_with("ignored.json")

    def test_smoke_writes_structured_report_and_uses_only_temporary_io(self):
        import antigravity_account_switcher as entrypoint

        class FakeRoot:
            def withdraw(self): pass
            def update_idletasks(self): pass
            def destroy(self): pass

        import tkinter
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch.object(tkinter, "Tk", return_value=FakeRoot()):
                self.assertEqual(entrypoint.run_portable_smoke(str(report)), 0)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(value["ok"])
            self.assertTrue(value["checks"]["tempIo"])


if __name__ == "__main__":
    unittest.main()

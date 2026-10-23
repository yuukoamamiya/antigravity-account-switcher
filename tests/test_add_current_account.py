import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from account_core import AccountCore, CREDENTIAL_TARGET
from config import validate_config


class FakeCredentialManager:
    def __init__(self, value):
        self.value = value

    def read(self, target):
        assert target == CREDENTIAL_TARGET
        return self.value


class AddCurrentAccountTests(unittest.TestCase):
    def make_core(self, directory, value):
        config = validate_config({"credentialsDirectory": str(directory)}, environ={})
        manager = FakeCredentialManager(value)
        with patch("account_core.CredentialManager", return_value=manager):
            core = AccountCore(config)
        return core

    def test_missing_and_invalid_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for raw, message in ((None, "未找到"), (b"\xff", "UTF-8"), (b"[]", "JSON 对象"), (b'{"token":{}}', "access_token")):
                core = self.make_core(Path(folder) / str(raw), raw)
                with self.assertRaisesRegex(ValueError, message):
                    core.add_current_account()

    def test_import_preserves_original_payload_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "accounts"
            raw = json.dumps({"token": {"access_token": "sim-access", "refresh_token": "sim-refresh"}, "email": "sample@example.invalid", "future": {"kept": True}}, separators=(",", ":")).encode()
            core = self.make_core(directory, raw)
            account, added = core.add_current_account()
            self.assertTrue(added)
            self.assertEqual(account.account_id, "1")
            self.assertEqual(account.path.read_bytes(), raw)
            self.assertEqual(json.loads(account.path.read_bytes())["future"], {"kept": True})
            self.assertEqual(json.loads(account.meta_path.read_text(encoding="utf-8"))["email"], "sample@example.invalid")

    def test_duplicate_keeps_newer_local_refresh_token_and_does_not_add_file(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "accounts"
            directory.mkdir()
            path = directory / "account-1.json"
            latest = {"token": {"access_token": "locally-refreshed", "refresh_token": "same-refresh"}, "local": "new"}
            path.write_text(json.dumps(latest), encoding="utf-8")
            raw = json.dumps({"token": {"access_token": "windows-access", "refresh_token": "same-refresh"}}).encode()
            core = self.make_core(directory, raw)
            account, added = core.add_current_account()
            self.assertFalse(added)
            self.assertEqual(account.payload, latest)
            self.assertEqual(account.path.read_text(encoding="utf-8"), json.dumps(latest))
            self.assertEqual(sorted(p.name for p in directory.glob("account-*.json")), ["account-1.json"])

    def test_missing_directory_and_orphaned_ids_are_handled(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "missing"
            raw = b'{"token":{"access_token":"a","refresh_token":"r"}}'
            core = self.make_core(directory, raw)
            self.assertEqual(core.scan_accounts(), [])
            directory.mkdir()
            (directory / "account-1.meta.json").write_text("{}", encoding="utf-8")
            (directory / "account-2.json").write_text("invalid", encoding="utf-8")
            account, added = core.add_current_account()
            self.assertTrue(added)
            self.assertEqual(account.account_id, "3")

    def test_metadata_write_failure_removes_partial_account_file(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "accounts"
            core = self.make_core(directory, b'{"token":{"access_token":"a","refresh_token":"r"}}')
            with patch("account_core.atomic_save_json", side_effect=OSError("simulated metadata failure")):
                with self.assertRaises(OSError):
                    core.add_current_account()
            self.assertEqual(list(directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

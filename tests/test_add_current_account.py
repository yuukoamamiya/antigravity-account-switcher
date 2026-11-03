import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from account_core import AccountCore, CREDENTIAL_TARGET, _NoRedirectHandler
from config import validate_config


class FakeCredentialManager:
    def __init__(self, value):
        self.value = value

    def read(self, target):
        assert target == CREDENTIAL_TARGET
        return self.value


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.value[:size]

    def geturl(self):
        return "https://www.googleapis.com/oauth2/v2/userinfo"


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.handlers = []
        self.calls = []

    def add_handler(self, handler):
        self.handlers.append(handler)

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class AddCurrentAccountTests(unittest.TestCase):
    def make_core(self, directory, value):
        config = validate_config({"credentialsDirectory": str(directory)}, environ={})
        manager = FakeCredentialManager(value)
        with patch("account_core.CredentialManager", return_value=manager):
            core = AccountCore(config)
        core.quota_client.opener_factory = lambda: FakeOpener(FakeResponse(b"{}"))
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

    def test_token_only_import_uses_bounded_google_userinfo_and_preserves_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "accounts"
            raw = b'{"token":{"access_token":"sim-access","refresh_token":"sim-refresh"}}'
            core = self.make_core(directory, raw)
            opener = FakeOpener(FakeResponse(b'{"email":"resolved@example.invalid"}'))
            core.quota_client.opener_factory = lambda: opener
            account, added = core.add_current_account()
            self.assertTrue(added)
            self.assertEqual(account.email, "resolved@example.invalid")
            self.assertEqual(account.path.read_bytes(), raw)
            request, timeout = opener.calls[0]
            self.assertEqual(request.full_url, "https://www.googleapis.com/oauth2/v2/userinfo")
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer sim-access")
            self.assertEqual(timeout, 20)
            self.assertTrue(opener.handlers)  # redirect blocking was installed

    def test_duplicate_backfills_email_metadata_without_replacing_local_token(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "accounts"
            directory.mkdir()
            path = directory / "account-1.json"
            latest = {"token": {"access_token": "local-access", "refresh_token": "same-refresh"}}
            original = json.dumps(latest).encode()
            path.write_bytes(original)
            incoming = b'{"token":{"access_token":"current-access","refresh_token":"same-refresh"}}'
            core = self.make_core(directory, incoming)
            opener = FakeOpener(FakeResponse(b'{"email":"backfilled@example.invalid"}'))
            core.quota_client.opener_factory = lambda: opener
            account, added = core.add_current_account()
            self.assertFalse(added)
            self.assertEqual(account.email, "backfilled@example.invalid")
            self.assertEqual(path.read_bytes(), original)
            meta = json.loads(account.meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["email"], "backfilled@example.invalid")
            self.assertEqual(meta["payload_sha256"], account.digest)

    def test_userinfo_failure_leaves_placeholder_and_does_not_fail_import(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = b'{"token":{"access_token":"sim-access","refresh_token":"sim-refresh"}}'
            core = self.make_core(Path(folder) / "accounts", raw)
            opener = FakeOpener(error=OSError("simulated offline"))
            core.quota_client.opener_factory = lambda: opener
            account, added = core.add_current_account()
            self.assertTrue(added)
            self.assertEqual(account.email, "账号 1")
            self.assertEqual(account.path.read_bytes(), raw)

    def test_redirect_handler_refuses_redirects_and_nested_email_is_supported(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://attacker.invalid/"))
        self.assertEqual(AccountCore._display_email({"user_info": {"email": "nested@example.invalid"}}), "nested@example.invalid")

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

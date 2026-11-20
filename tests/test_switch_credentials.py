from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from account_core import CREDENTIAL_TARGET, Account, AccountCore
from common import sha256
from config import validate_config
from quota_client import OAuthCredentialError, QuotaError


class FakeCredentialManager:
    """In-memory credential store recording writes, deletes, and metadata."""

    def __init__(self, records=None):
        self.records = {target: dict(record) for target, record in (records or {}).items()}
        self.writes = []
        self.removes = []
        self.reads = 0
        self.fail_next_write = None
        self.next_reads = []

    def read(self, target):
        self.reads += 1
        if self.next_reads:
            return self.next_reads.pop(0)
        record = self.records.get(target)
        return record["blob"] if record else None

    def read_record(self, target):
        record = self.records.get(target)
        return dict(record) if record else None

    def write(self, target, data):
        self.write_record(target, data)

    def write_record(
        self,
        target,
        data,
        *,
        flags=0,
        comment="Antigravity Account Switcher",
        persist=2,
        user_name="antigravity",
        target_alias=None,
    ):
        if self.fail_next_write is not None:
            error = self.fail_next_write
            self.fail_next_write = None
            raise error
        metadata = {
            "flags": flags,
            "comment": comment,
            "persist": persist,
            "user_name": user_name,
            "target_alias": target_alias,
        }
        self.writes.append({"target": target, "data": data, **metadata})
        self.records[target] = {"type": 1, **metadata, "blob": data}

    def remove(self, target):
        self.removes.append(target)
        self.records.pop(target, None)
        return True


class SwitchCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = validate_config({"credentialsDirectory": str(self.directory)}, environ={})
        self.payload = {"token": {"access_token": "simulated-access", "refresh_token": "simulated-refresh"}}
        self.data = b'{"token": {"access_token": "simulated-access", "refresh_token": "simulated-refresh"}}\n'
        self.account = Account(
            account_id="1",
            email="simulated@example.invalid",
            path=self.directory / "account-1.json",
            meta_path=self.directory / "account-1.meta.json",
            payload=self.payload,
            data=self.data,
            digest=sha256(self.data),
        )

    def make_core(self, credentials, validator=None):
        with patch("account_core.CredentialManager", return_value=credentials):
            core = AccountCore(self.config)
        core.log_lines = []
        core.log_fn = lambda message, level="INFO": core.log_lines.append((message, level))
        core.stop_antigravity = MagicMock(return_value=False)
        core.antigravity_running = MagicMock(return_value=False)
        core.quota_client.ensure_access_token = validator or (lambda *_args, **_kwargs: "simulated-access")
        return core

    @staticmethod
    def snapshot(blob=b"original-credential", **overrides):
        record = {
            "flags": 0,
            "type": 1,
            "comment": "Antigravity Account Switcher",
            "persist": 2,
            "user_name": "antigravity",
            "target_alias": None,
            "blob": blob,
        }
        record.update(overrides)
        return record

    def test_rejected_credentials_abort_before_stopping_or_writing(self):
        credentials = FakeCredentialManager()

        def reject(*_args, **_kwargs):
            raise OAuthCredentialError("Google OAuth 刷新失败（HTTP 400），账号凭据可能已失效。")

        core = self.make_core(credentials, reject)
        with self.assertRaises(RuntimeError) as context:
            core.switch(self.account)
        self.assertIn("已取消切换", str(context.exception))
        core.stop_antigravity.assert_not_called()
        self.assertEqual(credentials.writes, [])
        self.assertEqual(credentials.reads, 0)

    def test_offline_validation_failure_still_writes_credentials(self):
        credentials = FakeCredentialManager()

        def offline(*_args, **_kwargs):
            raise QuotaError("Google 接口请求超时或失败。")

        core = self.make_core(credentials, offline)
        core.switch(self.account)
        self.assertEqual([write["data"] for write in credentials.writes], [self.data])
        self.assertEqual(credentials.records[CREDENTIAL_TARGET]["blob"], self.data)
        self.assertTrue(
            any(level == "WARN" and "无法在切换前验证" in message for message, level in core.log_lines)
        )

    def test_read_back_mismatch_restores_snapshot(self):
        snapshot = self.snapshot()
        credentials = FakeCredentialManager({CREDENTIAL_TARGET: snapshot})
        core = self.make_core(credentials)
        credentials.next_reads.append(b"corrupted-credential")
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            core.switch(self.account)
        self.assertEqual(credentials.records[CREDENTIAL_TARGET]["blob"], snapshot["blob"])
        self.assertEqual(credentials.writes[-1]["data"], snapshot["blob"])

    def test_write_failure_restores_snapshot(self):
        snapshot = self.snapshot()
        credentials = FakeCredentialManager({CREDENTIAL_TARGET: snapshot})
        core = self.make_core(credentials)
        credentials.fail_next_write = OSError(13, "simulated write failure")
        with self.assertRaises(OSError):
            core.switch(self.account)
        self.assertEqual(credentials.records[CREDENTIAL_TARGET]["blob"], snapshot["blob"])
        self.assertEqual(len(credentials.writes), 1)

    def test_write_failure_without_previous_credential_removes_target(self):
        credentials = FakeCredentialManager()
        core = self.make_core(credentials)
        credentials.fail_next_write = OSError(13, "simulated write failure")
        with self.assertRaises(OSError):
            core.switch(self.account)
        self.assertEqual(credentials.removes, [CREDENTIAL_TARGET])
        self.assertNotIn(CREDENTIAL_TARGET, credentials.records)

    def test_snapshot_metadata_is_restored_exactly(self):
        snapshot = self.snapshot(
            blob=b"original-credential",
            flags=48,
            persist=3,
            user_name="simulated-user",
            comment="Simulated comment",
            target_alias="simulated-alias",
        )
        credentials = FakeCredentialManager({CREDENTIAL_TARGET: snapshot})
        core = self.make_core(credentials)
        credentials.fail_next_write = OSError(13, "simulated write failure")
        with self.assertRaises(OSError):
            core.switch(self.account)
        restored = credentials.writes[-1]
        self.assertEqual(restored["target"], CREDENTIAL_TARGET)
        self.assertEqual(restored["data"], snapshot["blob"])
        for key in ("flags", "comment", "persist", "user_name", "target_alias"):
            self.assertEqual(restored[key], snapshot[key])
            self.assertEqual(credentials.records[CREDENTIAL_TARGET][key], snapshot[key])


if __name__ == "__main__":
    unittest.main()

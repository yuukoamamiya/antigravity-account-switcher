"""Tests for local account rename, ordering, and backed-up deletion."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from account_core import AccountCore
from config import validate_config


class FakeCredentialManager:
    def __init__(self):
        self.value = None

    def read(self, _target):
        return self.value

    def read_record(self, _target):
        if self.value is None:
            return None
        return {
            "flags": 0,
            "type": 1,
            "comment": "Antigravity Account Switcher",
            "persist": 2,
            "user_name": "antigravity",
            "target_alias": None,
            "blob": self.value,
        }

    def write(self, _target, data):
        self.value = data

    def write_record(self, _target, data, **_metadata):
        self.write(_target, data)

    def remove(self, _target):
        self.value = None
        return True


def make_core(directory: str, credentials: FakeCredentialManager | None = None):
    folder = Path(directory)
    config = validate_config(
        {
            "credentialsDirectory": str(folder / "credentials"),
            "backupDirectory": str(folder / "backup"),
            "logFile": str(folder / "logs" / "account-switcher.log"),
            "quotaCacheFile": str(folder / "cache" / "quota-cache.json"),
        },
        environ={},
    )
    fake = credentials or FakeCredentialManager()
    with patch("account_core.CredentialManager", return_value=fake):
        core = AccountCore(config)
    return core, fake


def write_account(core: AccountCore, account_id: str, email: str, *, refresh: str = "simulated-refresh") -> None:
    core.credentials_dir.mkdir(parents=True, exist_ok=True)
    payload = {"token": {"access_token": f"simulated-access-{account_id}", "refresh_token": refresh}}
    (core.credentials_dir / f"account-{account_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (core.credentials_dir / f"account-{account_id}.meta.json").write_text(
        json.dumps({"email": email, "payload_sha256": f"stored-{account_id}"}), encoding="utf-8"
    )


class RenameAccountTests(unittest.TestCase):
    def test_rename_updates_metadata_and_preserves_existing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            write_account(core, "1", "old@example.invalid")
            meta_path = core.credentials_dir / "account-1.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["order"] = 3
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            account = core.scan_accounts()[0]
            self.assertEqual(account.order, 3)

            renamed = core.rename_account(account, "  New Display Name  ")

            self.assertIs(renamed, account)
            self.assertEqual(account.email, "New Display Name")
            stored = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["email"], "New Display Name")
            self.assertEqual(stored["order"], 3)
            self.assertEqual(stored["payload_sha256"], "stored-1")
            self.assertEqual(core.scan_accounts()[0].email, "New Display Name")

    def test_rename_accepts_254_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            write_account(core, "1", "old@example.invalid")
            account = core.scan_accounts()[0]

            core.rename_account(account, "x" * 254)

            self.assertEqual(account.email, "x" * 254)

    def test_rename_rejects_invalid_names_without_writing(self):
        cases = (
            ("", "账号名称不能为空。"),
            ("   ", "账号名称不能为空。"),
            (None, "账号名称不能为空。"),
            (42, "账号名称不能为空。"),
            ("x" * 255, "账号名称不能超过 254 个字符。"),
            ("line\nbreak", "账号名称不能包含控制字符。"),
            ("null\x00byte", "账号名称不能包含控制字符。"),
            ("del\x7fchar", "账号名称不能包含控制字符。"),
        )
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            write_account(core, "1", "old@example.invalid")
            meta_path = core.credentials_dir / "account-1.meta.json"
            before = meta_path.read_bytes()
            account = core.scan_accounts()[0]
            for value, message in cases:
                with self.subTest(value=type(value).__name__), self.assertRaisesRegex(ValueError, message):
                    core.rename_account(account, value)
            self.assertEqual(meta_path.read_bytes(), before)
            self.assertEqual(account.email, "old@example.invalid")


class AccountOrderTests(unittest.TestCase):
    def test_explicit_order_is_persisted_and_unordered_accounts_come_last(self):
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            for account_id in ("1", "2", "3"):
                write_account(core, account_id, f"{account_id}@example.invalid")
            write_account(core, "4", "4@example.invalid")

            written = core.set_account_order(["3", "1", "2"])

            self.assertEqual(written, 3)
            self.assertEqual([account.account_id for account in core.scan_accounts()], ["3", "1", "2", "4"])
            self.assertEqual(core.scan_accounts()[0].order, 0)
            self.assertEqual(core.scan_accounts()[2].order, 2)

    def test_invalid_orders_are_rejected_before_any_metadata_is_written(self):
        cases = (
            ([], "账号顺序无效。"),
            (["1", "1"], "账号顺序无效。"),
            (["1", ""], "账号顺序无效。"),
            (["1", 2], "账号顺序无效。"),
            (["../1"], "账号顺序无效。"),
            (["1\\2"], "账号顺序无效。"),
            (["c:1"], "账号顺序无效。"),
            ("1", "账号顺序无效。"),
            (["9"], "未知账号：9"),
            (["1", "9"], "未知账号：9"),
        )
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            for account_id in ("1", "2", "3"):
                write_account(core, account_id, f"{account_id}@example.invalid")
            meta_paths = sorted(core.credentials_dir.glob("*.meta.json"))
            before = {path: path.read_bytes() for path in meta_paths}
            for ordered_ids, message in cases:
                with self.subTest(ordered_ids=ordered_ids), self.assertRaisesRegex(ValueError, message):
                    core.set_account_order(ordered_ids)
            self.assertEqual({path: path.read_bytes() for path in meta_paths}, before)

    def test_scan_ignores_non_integer_and_boolean_order_values(self):
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            write_account(core, "1", "1@example.invalid")
            write_account(core, "2", "2@example.invalid")
            for account_id, value in (("1", True), ("2", "3")):
                meta_path = core.credentials_dir / f"account-{account_id}.meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["order"] = value
                meta_path.write_text(json.dumps(meta), encoding="utf-8")

            accounts = core.scan_accounts()

            self.assertEqual([account.account_id for account in accounts], ["1", "2"])
            self.assertEqual([account.order for account in accounts], [None, None])


class DeleteAccountTests(unittest.TestCase):
    def test_refuses_current_account_matching_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "1", "1@example.invalid")
            account = core.scan_accounts()[0]
            fake.value = account.data

            with self.assertRaisesRegex(RuntimeError, "不能删除当前正在使用的账号"):
                core.delete_account(account)

            self.assertTrue(account.path.exists())
            self.assertFalse(core.backup_dir.exists())

    def test_refuses_current_account_matching_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "1", "1@example.invalid", refresh="simulated-shared-refresh")
            account = core.scan_accounts()[0]
            fake.value = json.dumps(
                {"token": {"access_token": "simulated-rotated", "refresh_token": "simulated-shared-refresh"}}
            ).encode("utf-8")

            with self.assertRaisesRegex(RuntimeError, "不能删除当前正在使用的账号"):
                core.delete_account(account)

            self.assertTrue(account.path.exists())

    def test_delete_moves_credentials_and_metadata_into_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "2", "2@example.invalid")
            account = core.scan_accounts()[0]
            self.assertIsNone(fake.value)
            credential_bytes = account.path.read_bytes()
            meta_bytes = account.meta_path.read_bytes()

            backup_dir = core.delete_account(account)

            self.assertFalse(account.path.exists())
            self.assertFalse(account.meta_path.exists())
            self.assertTrue(backup_dir.is_dir())
            self.assertTrue(backup_dir.is_relative_to(core.backup_dir))
            self.assertEqual((backup_dir / "account-2.json").read_bytes(), credential_bytes)
            self.assertEqual((backup_dir / "account-2.meta.json").read_bytes(), meta_bytes)
            self.assertEqual(core.scan_accounts(), [])

    def test_delete_without_metadata_backs_up_only_the_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "2", "2@example.invalid")
            account = core.scan_accounts()[0]
            account.meta_path.unlink()
            self.assertIsNone(fake.value)

            backup_dir = core.delete_account(account)

            self.assertEqual(sorted(path.name for path in backup_dir.iterdir()), ["account-2.json"])

    def test_failed_second_move_restores_the_first_file(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "2", "2@example.invalid")
            account = core.scan_accounts()[0]
            self.assertIsNone(fake.value)
            original_move = AccountCore._move_file
            calls = []

            def flaky_move(source, destination):
                calls.append((source, destination))
                if len(calls) == 2:
                    raise OSError("simulated move failure")
                original_move(source, destination)

            with patch.object(AccountCore, "_move_file", side_effect=flaky_move):
                with self.assertRaisesRegex(RuntimeError, "删除账号失败：simulated move failure"):
                    core.delete_account(account)

            self.assertTrue(account.path.exists())
            self.assertTrue(account.meta_path.exists())
            backup_files = [path for path in (core.backup_dir / "credentials").rglob("*") if path.is_file()]
            self.assertEqual(backup_files, [])

    def test_delete_waits_for_account_lock_held_by_another_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            core, fake = make_core(directory)
            write_account(core, "2", "2@example.invalid")
            account = core.scan_accounts()[0]
            self.assertIsNone(fake.value)
            holding = threading.Event()
            release = threading.Event()
            done = threading.Event()
            outcome = {}

            def hold_account_lock():
                with core.operation_locks.hold(account.account_id):
                    holding.set()
                    release.wait(2)

            def delete():
                try:
                    outcome["backup"] = core.delete_account(account)
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            holder = threading.Thread(target=hold_account_lock)
            holder.start()
            self.assertTrue(holding.wait(1))
            remover = threading.Thread(target=delete)
            remover.start()
            try:
                self.assertFalse(done.wait(0.2))
            finally:
                release.set()
                holder.join(2)
                remover.join(2)

            self.assertFalse(holder.is_alive())
            self.assertFalse(remover.is_alive())
            self.assertIsNone(outcome.get("error"))
            backup_dir = outcome["backup"]
            self.assertTrue((backup_dir / "account-2.json").exists())
            self.assertTrue((backup_dir / "account-2.meta.json").exists())
            self.assertFalse(account.path.exists())
            self.assertFalse(account.meta_path.exists())

    def test_credential_read_failure_cancels_deletion_without_touching_files(self):
        with tempfile.TemporaryDirectory() as directory:
            core, _fake = make_core(directory)
            write_account(core, "2", "2@example.invalid")
            account = core.scan_accounts()[0]

            with patch.object(core.credentials, "read", side_effect=OSError("simulated read failure")):
                with self.assertRaisesRegex(RuntimeError, "无法确认当前账号，已取消删除：simulated read failure"):
                    core.delete_account(account)

            self.assertTrue(account.path.exists())
            self.assertTrue(account.meta_path.exists())
            self.assertFalse(core.backup_dir.exists())


if __name__ == "__main__":
    unittest.main()

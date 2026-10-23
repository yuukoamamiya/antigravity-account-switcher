from __future__ import annotations

import subprocess
import unittest
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from process_control import ProcessInfo, ProcessQueryError, find_processes
from runtime_control import AccountOperationLocks


class ProcessControlTests(unittest.TestCase):
    def test_tasklist_timeout_fails_closed_and_does_not_write_credentials(self) -> None:
        from antigravity_account_switcher import AccountCore

        core = AccountCore.__new__(AccountCore)
        core.operation_locks = AccountOperationLocks()
        core.credential_switch_lock = threading.Lock()
        core.config = {"closeTimeoutSeconds": 0}
        core.log_fn = lambda *_args: None
        core.credentials = Mock()
        account = SimpleNamespace(account_id="simulated", data=b"simulated", digest=hashlib.sha256(b"simulated").hexdigest())

        with patch("process_control.subprocess.run", side_effect=subprocess.TimeoutExpired("tasklist.exe", 5)):
            with self.assertRaises(ProcessQueryError):
                core.switch(account)
        core.credentials.write.assert_not_called()

    def test_tasklist_nonzero_fails_closed_and_does_not_write_credentials(self) -> None:
        from antigravity_account_switcher import AccountCore

        core = AccountCore.__new__(AccountCore)
        core.operation_locks = AccountOperationLocks()
        core.credential_switch_lock = threading.Lock()
        core.config = {"closeTimeoutSeconds": 0}
        core.log_fn = lambda *_args: None
        core.credentials = Mock()
        account = SimpleNamespace(account_id="simulated", data=b"simulated", digest="digest")

        with patch("process_control.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout=b"")):
            with self.assertRaises(ProcessQueryError):
                core.switch(account)
        core.credentials.write.assert_not_called()

    def test_taskkill_failure_after_natural_exit_is_success(self) -> None:
        from antigravity_account_switcher import AccountCore

        core = AccountCore.__new__(AccountCore)
        core.operation_locks = AccountOperationLocks()
        core.credential_switch_lock = threading.Lock()
        core.config = {"closeTimeoutSeconds": 0}
        core.log_fn = lambda *_args: None
        running = [ProcessInfo("Antigravity.exe", 1234)]

        def query(_name: str):
            if running:
                running.clear()
                return [ProcessInfo("Antigravity.exe", 1234)]
            return []

        with patch("account_core.find_processes", side_effect=query), patch(
            "account_core.terminate_process",
            return_value=SimpleNamespace(returncode=128),
        ):
            self.assertTrue(core.stop_antigravity())

    def test_close_failure_does_not_write_credentials(self) -> None:
        from antigravity_account_switcher import AccountCore

        core = AccountCore.__new__(AccountCore)
        core.operation_locks = AccountOperationLocks()
        core.credential_switch_lock = threading.Lock()
        core.credentials = Mock()
        core.log_fn = lambda *_args: None
        account = SimpleNamespace(account_id="simulated", data=b"simulated", digest="digest")
        with patch.object(core, "stop_antigravity", side_effect=RuntimeError("cannot close")):
            with self.assertRaisesRegex(RuntimeError, "cannot close"):
                core.switch(account)
        core.credentials.write.assert_not_called()

    def test_already_stopped_process_allows_verified_credential_write(self) -> None:
        from antigravity_account_switcher import AccountCore

        core = AccountCore.__new__(AccountCore)
        core.operation_locks = AccountOperationLocks()
        core.credential_switch_lock = threading.Lock()
        core.credentials = Mock()
        core.log_fn = lambda *_args: None
        account = SimpleNamespace(account_id="simulated", data=b"simulated", digest=hashlib.sha256(b"simulated").hexdigest())
        core.credentials.read.return_value = account.data
        with patch.object(core, "stop_antigravity", return_value=False), patch.object(
            core, "antigravity_running", return_value=False
        ):
            core.switch(account)
        core.credentials.write.assert_called_once_with("gemini:antigravity", account.data)

    def test_process_listing_requires_success_and_parses_exact_image(self) -> None:
        result = SimpleNamespace(returncode=0, stdout=b'"Antigravity.exe","1234","Console","1","50 K"\r\n"Other.exe","2"\r\n')
        with patch("process_control.subprocess.run", return_value=result):
            self.assertEqual(find_processes("Antigravity.exe"), [ProcessInfo("Antigravity.exe", 1234)])

        with patch("process_control.subprocess.run", return_value=SimpleNamespace(returncode=5, stdout=b"")):
            with self.assertRaises(ProcessQueryError):
                find_processes("Antigravity.exe")

        with patch(
            "process_control.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=b'"Antigravity.exe"\r\n'),
        ):
            with self.assertRaises(ProcessQueryError):
                find_processes("Antigravity.exe")


if __name__ == "__main__":
    unittest.main()

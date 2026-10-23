from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from process_control import MAX_WINDOWS_PID, ProcessInfo, ProcessQueryError, find_processes, terminate_process


def tasklist_result(stdout: bytes) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout)


class ProcessListingTests(unittest.TestCase):
    def test_empty_output_is_unknown_state(self) -> None:
        with patch("process_control.subprocess.run", return_value=tasklist_result(b"")):
            with self.assertRaises(ProcessQueryError):
                find_processes("Antigravity.exe")

    def test_non_csv_output_is_unknown_state(self) -> None:
        for output in (b"tasklist failed unexpectedly", b'"System.exe"\r\n'):
            with self.subTest(output=output), patch(
                "process_control.subprocess.run", return_value=tasklist_result(output)
            ):
                with self.assertRaises(ProcessQueryError):
                    find_processes("Antigravity.exe")

    def test_valid_process_rows_without_target_return_empty(self) -> None:
        output = b'"System.exe","4"\r\n"Idle","0"\r\n"Other.exe","22"\r\n'
        with patch("process_control.subprocess.run", return_value=tasklist_result(output)):
            self.assertEqual(find_processes("Antigravity.exe"), [])

    def test_chinese_image_name_from_oem_encoded_output_is_parsed(self) -> None:
        output = '"应用程序.exe","1234"\r\n'.encode("cp936")
        with patch("process_control.subprocess.run", return_value=tasklist_result(output)), patch(
            "process_control._tasklist_encoding", return_value="cp936"
        ):
            self.assertEqual(find_processes("应用程序.exe"), [ProcessInfo("应用程序.exe", 1234)])

    def test_target_process_pid_must_be_valid_nonzero_dword(self) -> None:
        bad_rows = (
            b'"Antigravity.exe","0"\r\n',
            f'"Antigravity.exe","{MAX_WINDOWS_PID + 1}"\r\n'.encode(),
            b'"Antigravity.exe","abc"\r\n',
            b'"Antigravity.exe"\r\n',
        )
        for output in bad_rows:
            with self.subTest(output=output), patch(
                "process_control.subprocess.run", return_value=tasklist_result(output)
            ):
                with self.assertRaises(ProcessQueryError):
                    find_processes("Antigravity.exe")

    def test_matching_process_is_found_and_pid_zero_is_allowed_for_other_image(self) -> None:
        output = b'"Idle","0"\r\n"Antigravity.exe","1234","Console","1","50 K"\r\n'
        with patch("process_control.subprocess.run", return_value=tasklist_result(output)):
            self.assertEqual(find_processes("Antigravity.exe"), [ProcessInfo("Antigravity.exe", 1234)])

    def test_terminate_process_rejects_pid_outside_allowed_range_without_running_taskkill(self) -> None:
        with patch("process_control.subprocess.run") as run:
            for pid in (0, MAX_WINDOWS_PID + 1, -1, True):
                with self.subTest(pid=pid), self.assertRaises(ValueError):
                    terminate_process(pid)  # type: ignore[arg-type]
            run.assert_not_called()

    def test_terminate_process_accepts_valid_pid_with_mocked_taskkill(self) -> None:
        with patch(
            "process_control.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            result = terminate_process(MAX_WINDOWS_PID)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", str(MAX_WINDOWS_PID), "/T"])


if __name__ == "__main__":
    unittest.main()

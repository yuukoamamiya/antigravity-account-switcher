"""Fail-closed process discovery and shutdown helpers for Windows."""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass

MAX_WINDOWS_PID = 0xFFFFFFFF


class ProcessQueryError(RuntimeError):
    """Raised when process state cannot be determined reliably."""


@dataclass(frozen=True)
class ProcessInfo:
    image_name: str
    pid: int


def find_processes(image_name: str, *, timeout: float = 5) -> list[ProcessInfo]:
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessQueryError(f"无法查询 {image_name} 进程状态：{exc}") from exc
    if result.returncode != 0:
        raise ProcessQueryError(f"tasklist 查询失败（退出码 {result.returncode}）。")
    try:
        rows = csv.reader(io.StringIO(result.stdout.decode("utf-8-sig", errors="strict")), strict=True)
        found: list[ProcessInfo] = []
        row_count = 0
        for row in rows:
            if not row:
                continue
            if len(row) < 2:
                raise ValueError("进程行缺少映像名或 PID")
            row_count += 1
            if not row[0].strip():
                raise ValueError("进程行的映像名为空")
            pid_text = row[1].strip()
            if not pid_text.isdecimal():
                raise ValueError("进程行包含非数字 PID")
            pid = int(pid_text)
            if pid > MAX_WINDOWS_PID:
                raise ValueError("进程行包含超出 Windows PID 范围的值")
            is_target = row[0].casefold() == image_name.casefold()
            if is_target and pid == 0:
                raise ValueError("目标进程包含无效 PID 0")
            if is_target:
                found.append(ProcessInfo(row[0], pid))
        if row_count == 0:
            raise ValueError("tasklist 输出为空")
        return found
    except (csv.Error, UnicodeDecodeError, ValueError) as exc:
        raise ProcessQueryError(f"无法解析 tasklist 的进程状态：{exc}") from exc


def terminate_process(pid: int, *, force: bool = False, timeout: float = 10) -> subprocess.CompletedProcess:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= MAX_WINDOWS_PID:
        raise ValueError(f"PID 必须在 1 到 {MAX_WINDOWS_PID} 之间。")
    command = ["taskkill.exe"]
    if force:
        command.append("/F")
    command.extend(["/PID", str(pid), "/T"])
    try:
        return subprocess.run(
            command,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法关闭进程 PID {pid}：{exc}") from exc

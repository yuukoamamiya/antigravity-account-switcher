"""Fail-closed process discovery and shutdown helpers for Windows."""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass


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
        rows = csv.reader(io.StringIO(result.stdout.decode("utf-8-sig", errors="replace")))
        found = []
        for row in rows:
            if not row:
                continue
            if row[0].casefold() != image_name.casefold():
                continue
            if len(row) < 2:
                raise ValueError("匹配的进程行缺少 PID")
            pid = int(row[1].replace(",", ""))
            if pid <= 0:
                raise ValueError("匹配的进程行包含无效 PID")
            found.append(ProcessInfo(row[0], pid))
        return found
    except (csv.Error, ValueError) as exc:
        raise ProcessQueryError(f"无法解析 tasklist 的进程状态：{exc}") from exc


def terminate_process(pid: int, *, force: bool = False, timeout: float = 10) -> subprocess.CompletedProcess:
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

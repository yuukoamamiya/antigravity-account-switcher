"""Local account discovery, credential persistence, and switching."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import platform
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cache_store import atomic_save_json
from common import now_iso, parse_time, read_json, safe_text, sha256
from config import resolve_path
from process_control import find_processes, terminate_process
from quota_client import QuotaClient
from runtime_control import AccountOperationLocks

CREDENTIAL_TARGET = "gemini:antigravity"


def antigravity_user_agent(executable: str) -> str:
    """Identify quota requests as the installed Antigravity Hub client."""
    version = "2.24.0"
    try:
        version_api = ctypes.WinDLL("version", use_last_error=True)
        version_api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version_api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version_api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        version_api.GetFileVersionInfoW.restype = wintypes.BOOL
        version_api.VerQueryValueW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
        version_api.VerQueryValueW.restype = wintypes.BOOL
        ignored = wintypes.DWORD()
        size = version_api.GetFileVersionInfoSizeW(executable, ctypes.byref(ignored))
        if size:
            data = ctypes.create_string_buffer(size)
            if version_api.GetFileVersionInfoW(executable, 0, size, data):
                info = wintypes.LPVOID()
                info_size = wintypes.UINT()
                if version_api.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(info_size)) and info_size.value >= 16:
                    words = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD))
                    version = f"{words[2] >> 16}.{words[2] & 0xffff}.{words[3] >> 16}"
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    architecture = "arm64" if "arm" in platform.machine().lower() else "amd64"
    return f"antigravity/hub/{version} (aidev_client; os_type=windows; arch={architecture})"


@dataclass
class Account:
    account_id: str
    email: str
    path: Path
    meta_path: Path
    payload: dict[str, Any]
    data: bytes
    digest: str


class CredentialFileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CredentialRecord(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", CredentialFileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", wintypes.LPVOID),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class CredentialManager:
    """Small ctypes wrapper around Windows Credential Manager generic blobs."""

    def __init__(self) -> None:
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.advapi.CredReadW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
        self.advapi.CredReadW.restype = wintypes.BOOL
        self.advapi.CredWriteW.argtypes = [ctypes.POINTER(CredentialRecord), wintypes.DWORD]
        self.advapi.CredWriteW.restype = wintypes.BOOL
        self.advapi.CredFree.argtypes = [wintypes.LPVOID]
        self.advapi.CredFree.restype = None

    def read(self, target: str) -> bytes | None:
        pointer = wintypes.LPVOID()
        if not self.advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:
                return None
            raise OSError(error, f"CredReadW failed ({error})")
        try:
            credential = ctypes.cast(pointer, ctypes.POINTER(CredentialRecord)).contents
            size = int(credential.CredentialBlobSize)
            if size <= 0 or not credential.CredentialBlob:
                return b""
            return ctypes.string_at(credential.CredentialBlob, size)
        finally:
            self.advapi.CredFree(pointer)

    def write(self, target: str, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data) if data else None
        credential = CredentialRecord()
        credential.Type = 1
        credential.TargetName = target
        credential.Comment = "Antigravity Account Switcher"
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(buffer, wintypes.LPVOID) if buffer is not None else None
        credential.Persist = 2
        credential.UserName = "antigravity"
        if not self.advapi.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise OSError(error, f"CredWriteW failed ({error})")


class AccountCore:
    def __init__(self, config: dict[str, Any], log_fn: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self.credentials_dir = resolve_path(str(config["credentialsDirectory"]))
        # The quota service can return availability placeholders when it sees
        # a third-party user agent instead of the native Antigravity client.
        self.user_agent = antigravity_user_agent(str(config.get("antigravityExecutable", "")))
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self.credentials = CredentialManager()
        self.operation_locks = AccountOperationLocks()
        self.credential_switch_lock = threading.Lock()
        # Endpoint/project hints are routing-only and intentionally stay in
        # memory; the persisted quota cache schema contains display data only.
        self._quota_route_cache: dict[str, Any] = {}
        self.quota_client = QuotaClient(
            config, self.user_agent, self.save_account, self._quota_route_cache,
        )

    def log(self, message: str, level: str = "INFO") -> None:
        self.log_fn(safe_text(message), level)

    def scan_accounts(self) -> list[Account]:
        if not self.credentials_dir.exists():
            raise FileNotFoundError(f"凭据目录不存在：{self.credentials_dir}")
        accounts: list[Account] = []
        for path in sorted(self.credentials_dir.glob("account-*.json"), key=lambda p: p.name.lower()):
            if path.name.endswith(".meta.json"):
                continue
            match = re.fullmatch(r"account-(.+)\.json", path.name, re.IGNORECASE)
            if not match:
                continue
            account_id = match.group(1)
            try:
                payload = read_json(path)
                token = payload.get("token", {})
                if not token.get("access_token") or not token.get("refresh_token"):
                    raise ValueError("缺少 token 字段")
                meta_path = path.with_name(path.stem + ".meta.json")
                email = ""
                if meta_path.exists():
                    try:
                        email = str(read_json(meta_path).get("email") or "")
                    except Exception:
                        pass
                data = path.read_bytes()
                accounts.append(
                    Account(
                        account_id=account_id,
                        email=email or f"账号 {account_id}",
                        path=path,
                        meta_path=meta_path,
                        payload=payload,
                        data=data,
                        digest=sha256(data),
                    )
                )
            except Exception as exc:
                self.log(f"跳过无效账号文件 {path.name}：{safe_text(exc)}", "WARN")
        return accounts

    def current_account_id(self, accounts: list[Account]) -> str | None:
        try:
            current = self.credentials.read(CREDENTIAL_TARGET)
            if current is None:
                return None
            current_digest = sha256(current)
            for account in accounts:
                if account.digest == current_digest:
                    return account.account_id
            try:
                current_payload = json.loads(current.decode("utf-8"))
                refresh_token = current_payload.get("token", {}).get("refresh_token")
                if refresh_token:
                    for account in accounts:
                        if account.payload.get("token", {}).get("refresh_token") == refresh_token:
                            return account.account_id
            except Exception:
                pass
        except Exception as exc:
            self.log(f"无法识别当前 Windows 凭据：{safe_text(exc)}", "WARN")
        return None

    def save_account(self, account: Account) -> None:
        with self.operation_locks.hold(account.account_id):
            self._save_account_locked(account)

    def _save_account_locked(self, account: Account) -> None:
        text = json.dumps(account.payload, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=account.path.name + ".", suffix=".tmp", dir=account.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            os.replace(temporary, account.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        account.data = account.path.read_bytes()
        account.digest = sha256(account.data)
        meta: dict[str, Any] = {}
        if account.meta_path.exists():
            try:
                value = read_json(account.meta_path)
                if isinstance(value, dict):
                    meta = value
            except Exception:
                pass
        meta["payload_sha256"] = account.digest
        meta["payload_bytes"] = len(account.data)
        meta["refreshed_at"] = now_iso()
        atomic_save_json(account.meta_path, meta)

    @staticmethod
    def antigravity_running() -> bool:
        return bool(find_processes("Antigravity.exe"))

    def stop_antigravity(self) -> bool:
        processes = find_processes("Antigravity.exe")
        if not processes:
            return False
        self.log("正在关闭 Antigravity。")
        for process in processes:
            result = terminate_process(process.pid)
            # taskkill can report failure when the process exited between the
            # tasklist snapshot and taskkill; verify state before treating it as an error.
            if result.returncode != 0 and not self.antigravity_running():
                self.log("Antigravity 已自然退出。")
                return True
        deadline = time.monotonic() + int(self.config.get("closeTimeoutSeconds", 15))
        while time.monotonic() < deadline:
            if not self.antigravity_running():
                self.log("Antigravity 已完全退出。")
                return True
            time.sleep(0.25)
        self.log("Antigravity 未能正常退出，执行强制关闭。", "WARN")
        remaining = find_processes("Antigravity.exe")
        for process in remaining:
            result = terminate_process(process.pid, force=True)
            if result.returncode != 0 and self.antigravity_running():
                raise RuntimeError(f"无法关闭 Antigravity（PID {process.pid}，taskkill 退出码 {result.returncode}）。")
        time.sleep(0.5)
        if self.antigravity_running():
            raise RuntimeError("无法关闭 Antigravity，请先手动关闭它。")
        return True

    def switch(self, account: Account) -> None:
        with self.credential_switch_lock:
            with self.operation_locks.hold(account.account_id):
                self.stop_antigravity()
                if self.antigravity_running():
                    raise RuntimeError("Antigravity 仍在运行，已中止凭据写入。")
                self.log("确认 Antigravity 已退出，开始写入 Windows 凭据。")
                self.credentials.write(CREDENTIAL_TARGET, account.data)
                read_back = self.credentials.read(CREDENTIAL_TARGET)
                if read_back is None or sha256(read_back) != account.digest:
                    raise RuntimeError("凭据写入后的校验失败。")

    def quota(self, account: Account) -> dict[str, Any]:
        with self.operation_locks.hold(account.account_id):
            return self.quota_client.quota(account)

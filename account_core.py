"""Local account discovery, credential persistence, and switching."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import base64
import errno
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cache_store import atomic_save_json
from common import now_iso, parse_time, read_json, safe_text, sha256
from config import resolve_path, save_oauth_client_secret
from process_control import find_processes, terminate_process
from quota_client import OAuthCredentialError, QuotaClient
from runtime_control import AccountOperationLocks

CREDENTIAL_TARGET = "gemini:antigravity"
ERROR_NOT_SAME_DEVICE = 17
# Official Antigravity Hub identity verified to receive every quota-summary
# group. Google withholds groups (and may reject requests) from older hub
# versions, so the switcher always presents this baseline rather than probing a
# possibly older local install. The version and Piper changelist are paired.
HUB_CLIENT_VERSION = "2.28.0"
HUB_CLIENT_CHANGELIST = "1008300195"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward credential-bearing requests through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def antigravity_user_agent(_executable: str = "") -> str:
    """Return the official Antigravity Hub User-Agent required upstream."""
    architecture = "arm64" if "arm" in platform.machine().lower() else "amd64"
    return (
        f"antigravity/hub/{HUB_CLIENT_VERSION} "
        f"(aidev_client; os_type=windows; arch={architecture}; cl={HUB_CLIENT_CHANGELIST})"
    )


@dataclass
class Account:
    account_id: str
    email: str
    path: Path
    meta_path: Path
    payload: dict[str, Any]
    data: bytes
    digest: str
    order: int | None = None


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
        self.advapi.CredDeleteW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self.advapi.CredDeleteW.restype = wintypes.BOOL
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

    def read_record(self, target: str) -> dict[str, Any] | None:
        """Read a credential plus the metadata required to rewrite it exactly."""
        pointer = wintypes.LPVOID()
        if not self.advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:
                return None
            raise OSError(error, f"CredReadW failed ({error})")
        try:
            credential = ctypes.cast(pointer, ctypes.POINTER(CredentialRecord)).contents
            size = int(credential.CredentialBlobSize)
            blob = ctypes.string_at(credential.CredentialBlob, size) if size > 0 and credential.CredentialBlob else b""
            return {
                "flags": int(credential.Flags),
                "type": int(credential.Type),
                "comment": credential.Comment or None,
                "persist": int(credential.Persist),
                "user_name": credential.UserName or None,
                "target_alias": credential.TargetAlias or None,
                "blob": blob,
            }
        finally:
            self.advapi.CredFree(pointer)

    def write_record(
        self,
        target: str,
        data: bytes,
        *,
        flags: int = 0,
        comment: str = "Antigravity Account Switcher",
        persist: int = 2,
        user_name: str = "antigravity",
        target_alias: str | None = None,
    ) -> None:
        """Write a generic credential using the supplied metadata."""
        buffer = ctypes.create_string_buffer(data) if data else None
        credential = CredentialRecord()
        credential.Flags = flags
        credential.Type = 1
        credential.TargetName = target
        credential.Comment = comment
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(buffer, wintypes.LPVOID) if buffer is not None else None
        credential.Persist = persist
        credential.TargetAlias = target_alias
        credential.UserName = user_name
        if not self.advapi.CredWriteW(ctypes.byref(credential), 0):
            error = ctypes.get_last_error()
            raise OSError(error, f"CredWriteW failed ({error})")

    def write(self, target: str, data: bytes) -> None:
        self.write_record(target, data)

    def remove(self, target: str) -> bool:
        """Delete a credential, treating an already-missing target as success."""
        if self.advapi.CredDeleteW(target, 1, 0):
            return True
        error = ctypes.get_last_error()
        if error == 1168:
            return True
        raise OSError(error, f"CredDeleteW failed ({error})")


class AccountCore:
    def __init__(self, config: dict[str, Any], log_fn: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self.credentials_dir = resolve_path(str(config["credentialsDirectory"]))
        self.backup_dir = resolve_path(str(config.get("backupDirectory", "Data/backup")))
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
            save_client_secret=self._save_client_secret,
        )
        self.quota_client.log_route = lambda message: self.log(message, "WARN")

    def _save_client_secret(self, secret: str) -> None:
        """Persist a runtime-discovered OAuth client secret for later launches."""
        self.config["oauthClientSecret"] = secret
        try:
            save_oauth_client_secret(secret)
            self.log("已将 OAuth 客户端参数保存到本地配置。")
        except Exception as exc:
            self.log(f"无法保存 OAuth 客户端参数：{safe_text(exc)}", "WARN")

    def log(self, message: str, level: str = "INFO") -> None:
        self.log_fn(safe_text(message), level)

    def scan_accounts(self) -> list[Account]:
        if not self.credentials_dir.exists():
            return []
        accounts: list[Account] = []
        for path in self.credentials_dir.glob("account-*.json"):
            if path.name.endswith(".meta.json"):
                continue
            match = re.fullmatch(r"account-(.+)\.json", path.name, re.IGNORECASE)
            if not match:
                continue
            account_id = match.group(1)
            try:
                with self.operation_locks.hold(account_id):
                    data = path.read_bytes()
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("凭据内容必须是 JSON 对象")
                    token = payload.get("token", {})
                    if not isinstance(token, dict) or not token.get("access_token") or not token.get("refresh_token"):
                        raise ValueError("缺少 token 字段")
                    meta_path = path.with_name(path.stem + ".meta.json")
                    email = ""
                    order: int | None = None
                    if meta_path.exists():
                        try:
                            meta = read_json(meta_path)
                            if isinstance(meta, dict):
                                email = str(meta.get("email") or "")
                                stored_order = meta.get("order")
                                if isinstance(stored_order, int) and not isinstance(stored_order, bool):
                                    order = stored_order
                        except Exception:
                            pass
                    accounts.append(
                        Account(
                            account_id=account_id,
                            email=email or f"账号 {account_id}",
                            path=path,
                            meta_path=meta_path,
                            payload=payload,
                            data=data,
                            digest=sha256(data),
                            order=order,
                        )
                    )
            except Exception as exc:
                self.log(f"跳过无效账号文件 {path.name}：{safe_text(exc)}", "WARN")
        accounts.sort(key=lambda account: (account.order is None, account.order or 0, account.path.name.lower()))
        return accounts

    @staticmethod
    def _display_email(payload: dict[str, Any]) -> str:
        """Return an email suitable for display; JWT claims are deliberately unverified."""
        for value in (payload.get("email"), payload.get("user_email")):
            if isinstance(value, str) and "@" in value:
                return value.strip()[:254]
        for key in ("user", "user_info", "userInfo", "profile", "account"):
            user = payload.get(key)
            if isinstance(user, dict):
                value = user.get("email") or user.get("user_email")
                if isinstance(value, str) and "@" in value:
                    return value.strip()[:254]
        token = payload.get("token")
        id_token = token.get("id_token") if isinstance(token, dict) else None
        if isinstance(id_token, str):
            try:
                segment = id_token.split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
                email = claims.get("email") if isinstance(claims, dict) else None
                if isinstance(email, str) and "@" in email:
                    return email.strip()[:254]
            except (IndexError, ValueError, UnicodeError, json.JSONDecodeError):
                pass
        return ""

    def _userinfo_email(self, access_token: str) -> str:
        """Resolve an email from Google's userinfo endpoint using configured proxy policy."""
        request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            # QuotaClient's opener applies the application's explicit proxy and
            # disables environment proxy discovery when no proxy is configured.
            opener = self.quota_client.opener_factory()
            opener.add_handler(_NoRedirectHandler())
            with opener.open(
                request, timeout=float(self.config.get("requestTimeoutSeconds", 20))
            ) as response:
                if response.geturl() != request.full_url:
                    return ""
                raw = response.read(65537)
            if len(raw) > 65536:
                return ""
            value = json.loads(raw.decode("utf-8"))
            return self._display_email(value) if isinstance(value, dict) else ""
        except urllib.error.HTTPError as exc:
            exc.close()
            return ""
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError):
            # Account import remains useful when userinfo is unavailable.
            return ""

    def _save_email_metadata_locked(self, account: Account, email: str) -> None:
        account.email = email
        meta: dict[str, Any] = {"payload_sha256": account.digest, "payload_bytes": len(account.data)}
        if account.meta_path.exists():
            try:
                existing = read_json(account.meta_path)
                if isinstance(existing, dict):
                    meta.update(existing)
            except Exception:
                pass
        meta["email"] = email
        atomic_save_json(account.meta_path, meta)

    def rename_account(self, account: Account, display_name: str) -> Account:
        """Change only the local display name stored in account metadata."""
        if not isinstance(display_name, str):
            raise ValueError("账号名称不能为空。")
        name = display_name.strip()
        if not name:
            raise ValueError("账号名称不能为空。")
        if len(name) > 254:
            raise ValueError("账号名称不能超过 254 个字符。")
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise ValueError("账号名称不能包含控制字符。")
        with self.operation_locks.hold(account.account_id):
            self._save_email_metadata_locked(account, name)
        self.log(f"账号 {account.account_id} 的显示名称已更新。")
        return account

    def set_account_order(self, ordered_ids: list[str]) -> int:
        """Persist an explicit display order, validating every id before writing."""
        if not isinstance(ordered_ids, list) or not ordered_ids:
            raise ValueError("账号顺序无效。")
        seen: set[str] = set()
        for account_id in ordered_ids:
            if (
                not isinstance(account_id, str)
                or not account_id
                or account_id in seen
                or any(marker in account_id for marker in ("/", "\\", "..", ":"))
            ):
                raise ValueError("账号顺序无效。")
            seen.add(account_id)
        for account_id in ordered_ids:
            if not (self.credentials_dir / f"account-{account_id}.json").is_file():
                raise ValueError(f"未知账号：{account_id}")
        written = 0
        for index, account_id in enumerate(ordered_ids):
            meta_path = self.credentials_dir / f"account-{account_id}.meta.json"
            with self.operation_locks.hold(account_id):
                meta: dict[str, Any] = {}
                if meta_path.exists():
                    try:
                        existing = read_json(meta_path)
                        if isinstance(existing, dict):
                            meta = existing
                    except Exception:
                        pass
                meta["order"] = index
                atomic_save_json(meta_path, meta)
            written += 1
        return written

    @staticmethod
    def _is_cross_device(error: OSError) -> bool:
        if error.errno == errno.EXDEV:
            return True
        return os.name == "nt" and getattr(error, "winerror", None) == ERROR_NOT_SAME_DEVICE

    @classmethod
    def _move_file(cls, source: Path, destination: Path) -> None:
        """Move a file, falling back to copy+unlink across volumes."""
        try:
            os.replace(source, destination)
        except OSError as exc:
            if not cls._is_cross_device(exc):
                raise
            shutil.copy2(source, destination)
            os.unlink(source)

    def delete_account(self, account: Account) -> Path:
        """Move an account's credential files into the backup directory."""
        current = self.credentials.read(CREDENTIAL_TARGET)
        if current is not None:
            matched = sha256(current) == account.digest
            if not matched:
                try:
                    current_payload = json.loads(current.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    current_payload = None
                current_refresh = (
                    current_payload.get("token", {}).get("refresh_token")
                    if isinstance(current_payload, dict) and isinstance(current_payload.get("token"), dict)
                    else None
                )
                matched = bool(current_refresh) and current_refresh == account.payload.get("token", {}).get("refresh_token")
            if matched:
                raise RuntimeError("不能删除当前正在使用的账号，请先切换到其他账号。")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.backup_dir / "credentials" / f"{stamp}-account-{account.account_id}"
        sources = [account.path]
        if account.meta_path.exists():
            sources.append(account.meta_path)
        moved: list[tuple[Path, Path]] = []
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for source in sources:
                destination = backup_dir / source.name
                self._move_file(source, destination)
                moved.append((source, destination))
        except Exception as exc:
            for source, destination in reversed(moved):
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    self._move_file(destination, source)
                except Exception as rollback_exc:
                    self.log(f"恢复账号文件失败：{safe_text(rollback_exc)}", "WARN")
            raise RuntimeError(f"删除账号失败：{safe_text(exc)}") from None
        self.log(f"账号 {account.account_id} 的凭据已移动到备份目录。")
        return backup_dir

    def add_current_account(self, display_name: str | None = None) -> tuple[Account, bool]:
        """Import the current Windows credential without replacing an existing account."""
        # Serialize with credential switching so the blob cannot be read halfway
        # through a concurrent switch operation.
        with self.credential_switch_lock:
            raw = self.credentials.read(CREDENTIAL_TARGET)
            if raw is None or not raw:
                raise ValueError("未找到当前登录账号凭据。")
            try:
                text = raw.decode("utf-8")
                payload = json.loads(text)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("当前登录账号凭据不是有效的 UTF-8 JSON。") from exc
            if not isinstance(payload, dict):
                raise ValueError("当前登录账号凭据必须是 JSON 对象。")
            token = payload.get("token")
            if not isinstance(token, dict):
                raise ValueError("当前登录账号凭据缺少有效 token 对象。")
            access_token = token.get("access_token")
            refresh_token = token.get("refresh_token")
            if not isinstance(access_token, str) or not access_token.strip() or not isinstance(refresh_token, str) or not refresh_token.strip():
                raise ValueError("当前登录账号凭据缺少 access_token 或 refresh_token。")

            accounts = self.scan_accounts()
            for account in accounts:
                stored_refresh = account.payload.get("token", {}).get("refresh_token")
                if stored_refresh == refresh_token:
                    email = display_name.strip()[:254] if isinstance(display_name, str) and display_name.strip() else self._display_email(payload)
                    if not email and account.email.startswith("账号 "):
                        email = self._display_email(account.payload)
                    if not email and account.email.startswith("账号 "):
                        email = self._userinfo_email(access_token)
                    if email and email != account.email:
                        with self.operation_locks.hold(account.account_id):
                            self._save_email_metadata_locked(account, email)
                    # Return the on-disk object so callers retain the latest
                    # locally refreshed token state and metadata.
                    return account, False

            self.credentials_dir.mkdir(parents=True, exist_ok=True)
            number = 1
            while True:
                account_id = str(number)
                path = self.credentials_dir / f"account-{account_id}.json"
                meta_path = self.credentials_dir / f"account-{account_id}.meta.json"
                if not path.exists() and not meta_path.exists():
                    break
                number += 1

            email = display_name.strip()[:254] if isinstance(display_name, str) and display_name.strip() else self._display_email(payload)
            if not email:
                email = self._userinfo_email(access_token)
            account = Account(account_id, email or f"账号 {account_id}", path, meta_path, payload, raw, sha256(raw))
            meta = {"payload_sha256": account.digest, "payload_bytes": len(raw)}
            if email:
                meta["email"] = email

            with self.operation_locks.hold(account_id):
                # Link a fully written same-directory temporary file into place
                # atomically, while refusing to replace a concurrent account.
                fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.credentials_dir)
                published = False
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if os.name == "nt":
                        # MoveFileW is an atomic same-volume rename and fails
                        # when the destination exists. Unlike hard links, it
                        # also works on FAT/exFAT portable drives.
                        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                        kernel32.MoveFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
                        kernel32.MoveFileW.restype = wintypes.BOOL
                        if not kernel32.MoveFileW(temporary, str(path)):
                            error = ctypes.get_last_error()
                            raise OSError(error, f"MoveFileW failed ({error})", str(path))
                    else:
                        # link() provides exclusive publication on POSIX.
                        os.link(temporary, path)
                        os.unlink(temporary)
                    published = True
                    atomic_save_json(meta_path, meta)
                except Exception:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
                    if published:
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    raise
            account.data = path.read_bytes()
            account.digest = sha256(account.data)
            return account, True

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
                try:
                    self.quota_client.ensure_access_token(account, force=True)
                except OAuthCredentialError as exc:
                    raise RuntimeError(f"账号凭据已失效，已取消切换：{safe_text(exc)}") from None
                except Exception as exc:
                    self.log(f"无法在切换前验证账号凭据，将直接写入本地凭据：{safe_text(exc)}", "WARN")
                self.stop_antigravity()
                if self.antigravity_running():
                    raise RuntimeError("Antigravity 仍在运行，已中止凭据写入。")
                snapshot = self.credentials.read_record(CREDENTIAL_TARGET)
                self.log("已备份当前 Windows 凭据以便回滚。")
                self.log("确认 Antigravity 已退出，开始写入 Windows 凭据。")
                try:
                    self.credentials.write(CREDENTIAL_TARGET, account.data)
                    read_back = self.credentials.read(CREDENTIAL_TARGET)
                    if read_back is None or sha256(read_back) != sha256(account.data):
                        raise RuntimeError("凭据写入后的校验失败。")
                except Exception:
                    self._restore_credential_snapshot(snapshot)
                    raise

    def _restore_credential_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        """Best-effort restore of the credential that preceded a failed write."""
        try:
            if snapshot is None:
                self.credentials.remove(CREDENTIAL_TARGET)
                read_back = self.credentials.read(CREDENTIAL_TARGET)
                if read_back:
                    raise RuntimeError("删除后仍能读到凭据。")
                return
            self.credentials.write_record(
                CREDENTIAL_TARGET,
                snapshot["blob"],
                flags=snapshot["flags"],
                comment=snapshot["comment"],
                persist=snapshot["persist"],
                user_name=snapshot["user_name"],
                target_alias=snapshot["target_alias"],
            )
            read_back = self.credentials.read(CREDENTIAL_TARGET)
            if read_back is None or sha256(read_back) != sha256(snapshot["blob"]):
                raise RuntimeError("回读内容与备份不一致。")
        except Exception as exc:
            self.log(f"回滚原凭据失败：{safe_text(exc)}", "WARN")

    def quota(self, account: Account) -> dict[str, Any]:
        with self.operation_locks.hold(account.account_id):
            return self.quota_client.quota(account)

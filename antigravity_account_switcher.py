"""Antigravity account switcher.

Windows-only, standard-library GUI. Account credential files are intentionally
kept local and are never included in logs or network request bodies.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import os
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import messagebox


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "local-data" / "account-switcher.config.json"
LEGACY_CONFIG_PATH = ROOT / "account-switcher.config.json"
CREDENTIAL_TARGET = "gemini:antigravity"
OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
QUOTA_ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)
PROJECT_ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
)

GWLP_WNDPROC = -4
WM_SETREDRAW = 0x000B
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080


def safe_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已隐藏]", text)
    text = re.sub(
        r"(?i)(access_token|refresh_token|id_token)\s*[=:]\s*[^\s,&]+",
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "credentialsDirectory": "local-data/credentials",
        "antigravityExecutable": "Antigravity.exe",
        "proxy": "",
        "noProxy": "localhost,127.0.0.1",
        "logFile": "local-data/account-switcher.log",
        "quotaCacheFile": "local-data/quota-cache.json",
        "queryQuota": True,
        "requestTimeoutSeconds": 20,
        "closeTimeoutSeconds": 15,
        "oauthClientId": OAUTH_CLIENT_ID,
        "oauthClientSecret": os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", ""),
    }
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if config_path.exists():
        try:
            loaded = read_json(config_path)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception:
            pass
    return config


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


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
    machine = platform.machine().lower()
    architecture = "arm64" if "arm" in machine else "amd64"
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


class GoogleError(RuntimeError):
    pass


class AccountCore:
    def __init__(self, config: dict[str, Any], log_fn: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self.credentials_dir = resolve_path(str(config["credentialsDirectory"]))
        # The quota service can return availability placeholders when it sees
        # a third-party user agent instead of the native Antigravity client.
        self.user_agent = antigravity_user_agent(str(config.get("antigravityExecutable", "")))
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self.credentials = CredentialManager()

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
        account.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def antigravity_running() -> bool:
        try:
            result = subprocess.run(
                ["tasklist.exe", "/FI", "IMAGENAME eq Antigravity.exe", "/NH"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5,
            )
            return b"Antigravity.exe".lower() in result.stdout.lower()
        except Exception:
            return False

    def stop_antigravity(self) -> bool:
        if not self.antigravity_running():
            return False
        self.log("正在关闭 Antigravity。")
        subprocess.run(
            ["taskkill.exe", "/IM", "Antigravity.exe", "/T"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
        deadline = time.monotonic() + int(self.config.get("closeTimeoutSeconds", 15))
        while time.monotonic() < deadline:
            if not self.antigravity_running():
                self.log("Antigravity 已完全退出。")
                return True
            time.sleep(0.25)
        self.log("Antigravity 未能正常退出，执行强制关闭。", "WARN")
        subprocess.run(
            ["taskkill.exe", "/F", "/IM", "Antigravity.exe", "/T"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
        time.sleep(0.5)
        if self.antigravity_running():
            raise RuntimeError("无法关闭 Antigravity，请先手动关闭它。")
        return True

    def switch(self, account: Account) -> None:
        self.stop_antigravity()
        if self.antigravity_running():
            raise RuntimeError("Antigravity 仍在运行，已中止凭据写入。")
        self.log("确认 Antigravity 已退出，开始写入 Windows 凭据。")
        self.credentials.write(CREDENTIAL_TARGET, account.data)
        read_back = self.credentials.read(CREDENTIAL_TARGET)
        if read_back is None or sha256(read_back) != account.digest:
            raise RuntimeError("凭据写入后的校验失败。")

    def _opener(self) -> urllib.request.OpenerDirector:
        proxy = str(self.config.get("proxy") or "").strip()
        if proxy:
            return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _json_request(self, url: str, access_token: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with self._opener().open(request, timeout=int(self.config.get("requestTimeoutSeconds", 20))) as response:
                value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise GoogleError("Google 返回的数据格式不正确。")
                return value
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google 接口返回 HTTP {exc.code}。") from None
        except urllib.error.URLError as exc:
            raise GoogleError(f"Google 接口请求失败：{safe_text(exc.reason)}") from None
        except TimeoutError:
            raise GoogleError("Google 接口请求超时。") from None
        except json.JSONDecodeError:
            raise GoogleError("Google 接口返回的数据无法解析。") from None

    def _refresh_access_token(self, account: Account) -> str:
        token = account.payload["token"]
        expiry = parse_time(token.get("expiry"))
        if token.get("access_token") and expiry and expiry > datetime.now(timezone.utc) + timedelta(seconds=60):
            return str(token["access_token"])
        form = urllib.parse.urlencode(
            {
                "client_id": self.config.get("oauthClientId", OAUTH_CLIENT_ID),
                "client_secret": self.config.get("oauthClientSecret") or os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", ""),
                "refresh_token": token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AntigravityAccountSwitcher/1.0"},
            method="POST",
        )
        try:
            with self._opener().open(request, timeout=int(self.config.get("requestTimeoutSeconds", 20))) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = "未知错误"
            try:
                error_value = json.loads(exc.read().decode("utf-8")).get("error")
                if error_value:
                    code = str(error_value)
            except Exception:
                pass
            raise GoogleError(f"Google OAuth 刷新失败：{code}") from None
        except urllib.error.URLError as exc:
            raise GoogleError(f"Google OAuth 请求失败：{safe_text(exc.reason)}") from None
        except (TimeoutError, json.JSONDecodeError):
            raise GoogleError("Google OAuth 请求超时或响应无法解析。") from None
        access_token = value.get("access_token") if isinstance(value, dict) else None
        if not access_token:
            raise GoogleError("Google OAuth 响应中没有 access_token。")
        token["access_token"] = access_token
        token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=int(value.get("expires_in", 3600)))).isoformat()
        if value.get("refresh_token"):
            token["refresh_token"] = value["refresh_token"]
        self.save_account(account)
        self.log(f"账号 {account.account_id} 的 token 已在本地凭据文件中更新。")
        return str(access_token)

    def _project_id(self, access_token: str) -> str | None:
        body = {"metadata": {"ideType": "ANTIGRAVITY"}}
        for endpoint in PROJECT_ENDPOINTS:
            try:
                response = self._json_request(endpoint, access_token, body)
                project = response.get("cloudaicompanionProject")
                if project:
                    return str(project)
            except GoogleError:
                continue
        return None

    @staticmethod
    def _bucket(raw: dict[str, Any], group: dict[str, Any]) -> dict[str, Any] | None:
        if raw.get("remainingFraction") is None:
            return None
        try:
            fraction = float(raw["remainingFraction"])
        except (TypeError, ValueError):
            return None
        if fraction > 1:
            fraction /= 100
        return {
            "remainingFraction": max(0.0, min(1.0, fraction)),
            "resetTime": str(raw.get("resetTime") or ""),
            "displayName": str(raw.get("displayName") or ""),
            "description": str(raw.get("description") or ""),
            "window": str(raw.get("window") or ""),
            "groupName": str(group.get("displayName") or ""),
        }

    def quota(self, account: Account) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": account.account_id,
            "success": False,
            "fiveHour": None,
            "weekly": None,
            "error": None,
            "attemptedAt": now_iso(),
        }
        try:
            access_token = self._refresh_access_token(account)
            body: dict[str, Any] = {}
            project = self._project_id(access_token)
            if project:
                body["project"] = project
            last_error = "额度接口未返回可识别数据。"
            for endpoint in QUOTA_ENDPOINTS:
                try:
                    response = self._json_request(endpoint, access_token, body)
                    groups = response.get("groups")
                    if not isinstance(groups, list) or not groups:
                        raise GoogleError("Google 返回的额度分组为空。")
                    five_hour = None
                    weekly = None
                    for group in groups:
                        if not isinstance(group, dict):
                            continue
                        for raw in group.get("buckets") or []:
                            if not isinstance(raw, dict):
                                continue
                            bucket = self._bucket(raw, group)
                            if bucket is None:
                                continue
                            # Google sometimes mentions the 5-hour limit in a
                            # weekly bucket's description (for example when
                            # the 5-hour limit has been exhausted).  Classify
                            # from the explicit window/name fields first so
                            # that description text cannot move a bucket into
                            # the wrong UI column.
                            primary_label = " ".join(
                                str(bucket.get(key) or "")
                                for key in ("window", "displayName")
                            ).lower()
                            if re.search(r"5\s*-?\s*h|5\s*hour|five.?hour|hourly", primary_label) and five_hour is None:
                                five_hour = bucket
                            elif re.search(r"week|weekly|7\s*-?\s*d|1\s*-?\s*w", primary_label) and weekly is None:
                                weekly = bucket
                            elif not primary_label:
                                fallback_label = " ".join(
                                    str(bucket.get(key) or "")
                                    for key in ("description", "groupName")
                                ).lower()
                                if re.search(r"5\s*-?\s*h|5\s*hour|five.?hour|hourly", fallback_label) and five_hour is None:
                                    five_hour = bucket
                                elif re.search(r"week|weekly|7\s*-?\s*d|1\s*-?\s*w", fallback_label) and weekly is None:
                                    weekly = bucket
                    if five_hour is None and weekly is None:
                        raise GoogleError("Google 返回了额度数据，但未识别出 5 小时或一周窗口。")
                    result.update(success=True, fiveHour=five_hour, weekly=weekly, error=None)
                    return result
                except GoogleError as exc:
                    last_error = str(exc)
            raise GoogleError(last_error)
        except Exception as exc:
            result["error"] = safe_text(exc)
            return result


class SwitcherApp:
    def __init__(self, core: AccountCore, accounts: list[Account], cache: dict[str, Any]) -> None:
        self.core = core
        self.accounts = accounts
        self.cache = cache
        self.current_id = core.current_account_id(accounts)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.switch_lock = threading.Lock()
        self.switching = False
        self.quota_refreshing = False
        self.quota_successes = 0
        self.quota_failures = 0
        self.layout_redraw_job: str | None = None
        self.pending_canvas_width = 0
        self.native_hwnd: int | None = None
        self.native_user32: Any | None = None
        self.native_old_wndproc: Any | None = None
        self.native_wndproc: Any | None = None
        self.native_enum_proc: Any | None = None
        self.native_redraw_windows: list[int] = []
        self.native_dragging = False
        self.dark = self.system_dark()
        self.colors = self.theme_colors()
        self.cards: dict[str, dict[str, Any]] = {}
        self.log_path = resolve_path(str(core.config["logFile"]))

        self.root = tk.Tk()
        self.root.title("Antigravity 账号切换器")
        self.root.geometry("980x760")
        self.root.minsize(850, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_ui()
        self.apply_theme()
        self.update_cards()
        self.root.update_idletasks()
        self.install_native_drag_guard()
        self.write_log(f"已加载 {len(accounts)} 个账号。")
        self.root.after(150, self.poll_events)
        self.root.after(1000, self.tick)
        if bool(core.config.get("queryQuota", True)) and accounts:
            self.root.after(250, self.start_quota_refresh)

    @staticmethod
    def system_dark() -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0]) == 0
        except Exception:
            return False

    def theme_colors(self) -> dict[str, str]:
        if self.dark:
            return {
                "form": "#121316", "panel": "#191b20", "card": "#1d2026", "input": "#15171b",
                "text": "#f4f4f5", "secondary": "#a1a1aa", "muted": "#71717a", "accent": "#9b8cff",
                "accent_hover": "#ad9fff", "success": "#4ade80", "warning": "#fbbf24", "border": "#30343d",
                "bar_bg": "#353a45",
            }
        return {
            "form": "#f6f6f8", "panel": "#fbfbfc", "card": "#ffffff", "input": "#ffffff",
            "text": "#18181b", "secondary": "#626773", "muted": "#8b919e", "accent": "#6857d9",
            "accent_hover": "#5746c8", "success": "#16a34a", "warning": "#a16207", "border": "#e6e7eb",
            "bar_bg": "#e6e7eb",
        }

    def build_ui(self) -> None:
        colors = self.colors
        self.root.configure(bg=colors["form"])
        self.header = tk.Frame(self.root, bg=colors["form"], height=86)
        self.header.pack(fill="x", padx=24, pady=(22, 8))
        self.header.grid_columnconfigure(1, weight=1)
        self.logo = tk.Label(self.header, text="A", font=("Segoe UI", 13, "bold"), width=2, height=1, padx=4, pady=4)
        self.logo.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        self.title_label = tk.Label(self.header, text="Antigravity 账号", font=("Microsoft YaHei UI", 17, "bold"), anchor="w")
        self.title_label.grid(row=0, column=1, sticky="w")
        self.subtitle_label = tk.Label(self.header, text="本地凭据切换 · 不自动重启 · 数据只保存在本机", font=("Microsoft YaHei UI", 9), anchor="w")
        self.subtitle_label.grid(row=1, column=1, sticky="w", pady=(5, 0))
        self.refresh_button = tk.Button(self.header, text="刷新额度", command=self.start_quota_refresh, width=11, relief="flat", cursor="hand2", bd=0)
        self.refresh_button.grid(row=0, column=2, rowspan=2, padx=(18, 0))
        self.quota_status = tk.Label(self.header, text="额度查询未开始", font=("Microsoft YaHei UI", 8), anchor="e")
        self.quota_status.grid(row=2, column=2, sticky="e", pady=(4, 0))

        self.summary = tk.Frame(self.root, bg=colors["form"], height=58)
        self.summary.pack(fill="x", padx=24, pady=(0, 14))
        self.summary.grid_columnconfigure(0, weight=1)
        self.summary.grid_columnconfigure(1, weight=1)
        self.summary.grid_columnconfigure(2, weight=1)
        self.summary_labels: list[tuple[tk.Label, tk.Label]] = []
        self.summary_value_labels: list[tk.Label] = []
        for index, (caption, value) in enumerate((("已发现账号", str(len(self.accounts))), ("当前账号", self.current_account_text()), ("额度状态", "等待查询"))):
            tile = tk.Frame(self.summary, padx=14, pady=8)
            tile.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0))
            label = tk.Label(tile, text=caption, font=("Microsoft YaHei UI", 8), anchor="w")
            label.pack(anchor="w")
            value_label = tk.Label(tile, text=value, font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
            value_label.pack(anchor="w", pady=(2, 0))
            self.summary_labels.append((label, value_label))
            self.summary_value_labels.append(value_label)

        self.body = tk.Frame(self.root, bg=colors["form"])
        self.body.pack(fill="both", expand=True, padx=24)
        # The account list is one Canvas instead of a tree of native Tk
        # child-windows.  This keeps Windows window-move repainting cheap.
        self.canvas = tk.Canvas(self.body, highlightthickness=0, bd=0, bg=colors["form"])
        self.scrollbar = tk.Scrollbar(self.body, orient="vertical", command=self.canvas.yview)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        for account in self.accounts:
            self.build_card(account)
        self.redraw_cards()

        self.footer = tk.Frame(self.root, bg=colors["form"], height=116)
        self.footer.pack(fill="x", padx=24, pady=(10, 18))
        self.log_title = tk.Label(self.footer, text="最近操作", font=("Microsoft YaHei UI", 8, "bold"), anchor="w")
        self.log_title.pack(anchor="w")
        self.log_text = tk.Text(self.footer, height=4, state="disabled", relief="flat", wrap="none", font=("Consolas", 8), padx=8, pady=5)
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))

    def build_card(self, account: Account) -> None:
        self.cards[account.account_id] = {"account": account, "items": {}}

    def redraw_cards(self) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 780)
        if not self.accounts:
            text_id = self.canvas.create_text(
                18,
                18,
                anchor="nw",
                text=f"没有找到账号文件。请把 account-*.json 放入：\n{self.core.credentials_dir}",
                font=("Microsoft YaHei UI", 10),
                fill=self.colors["secondary"],
            )
            self.canvas.configure(scrollregion=(0, 0, width, 90))
            self.empty_accounts_item = text_id
            return
        for index, account in enumerate(self.accounts):
            self.create_card_items(account, index, width)
        total_height = len(self.accounts) * 202 + 2
        self.canvas.configure(scrollregion=(0, 0, width, total_height))
        self.update_cards()

    def create_card_items(self, account: Account, index: int, width: int) -> None:
        colors = self.colors
        entry = self.cards[account.account_id]
        x0 = 2
        x1 = max(width - 2, 700)
        y0 = index * 202 + 2
        y1 = y0 + 188
        button_x = x1 - 142
        quota_start = x0 + 302
        quota_gap = 18
        quota_width = max(150, (button_x - quota_start - quota_gap) // 2)
        weekly_x = quota_start + quota_width + quota_gap
        items: dict[str, Any] = {}

        items["card"] = self.canvas.create_rectangle(x0, y0, x1, y1, fill=colors["card"], outline=colors["border"])
        items["strip"] = self.canvas.create_rectangle(x0 + 1, y0 + 1, x0 + 5, y1 - 1, fill=colors["accent"], outline="")
        items["account_label"] = self.canvas.create_text(x0 + 35, y0 + 28, anchor="nw", text=f"ACCOUNT {account.account_id}", font=("Consolas", 8), fill=colors["muted"])
        items["email"] = self.canvas.create_text(x0 + 35, y0 + 54, anchor="nw", text=account.email, font=("Microsoft YaHei UI", 11, "bold"), fill=colors["text"])
        items["status"] = self.canvas.create_text(x0 + 35, y0 + 96, anchor="nw", text="", font=("Microsoft YaHei UI", 8), fill=colors["secondary"])

        for prefix, panel_x, title in (("five", quota_start, "5 小时窗口"), ("weekly", weekly_x, "一周窗口")):
            panel_y = y0 + 12
            panel_w = quota_width
            items[f"{prefix}_panel"] = self.canvas.create_rectangle(panel_x, panel_y, panel_x + panel_w, panel_y + 78, fill=colors["panel"], outline="")
            items[f"{prefix}_title"] = self.canvas.create_text(panel_x + 14, panel_y + 12, anchor="nw", text=title, font=("Microsoft YaHei UI", 8), fill=colors["muted"])
            items[prefix] = self.canvas.create_text(panel_x + 14, panel_y + 39, anchor="nw", text="暂无数据", font=("Microsoft YaHei UI", 9, "bold"), fill=colors["text"])
            bar_x = panel_x + 14
            bar_y = panel_y + 66
            bar_w = max(panel_w - 28, 20)
            items[f"{prefix}_bar_bg"] = self.canvas.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + 5, fill=colors["bar_bg"], outline="")
            items[f"{prefix}_bar"] = self.canvas.create_rectangle(bar_x, bar_y, bar_x, bar_y + 5, fill=colors["accent"], outline="")
            items[f"{prefix}_bar_bounds"] = (bar_x, bar_y, bar_w, 5)

        items["updated"] = self.canvas.create_text(quota_start + 4, y0 + 112, anchor="nw", text="", font=("Microsoft YaHei UI", 8), fill=colors["muted"])
        items["error"] = self.canvas.create_text(x0 + 35, y0 + 154, anchor="nw", text="", width=max(button_x - x0 - 55, 300), font=("Microsoft YaHei UI", 8), fill=colors["warning"])
        button_tag = f"account-button:{account.account_id}"
        items["button"] = self.canvas.create_rectangle(button_x, y0 + 62, button_x + 126, y0 + 101, fill=colors["accent"], outline="", tags=(button_tag,))
        items["button_text"] = self.canvas.create_text(button_x + 63, y0 + 81, anchor="center", text="切换到此账号", font=("Microsoft YaHei UI", 9), fill="white", tags=(button_tag,))
        self.canvas.tag_bind(button_tag, "<Button-1>", lambda _event, target=account: self.switch_account(target))
        entry["items"] = items

    def apply_theme(self) -> None:
        self.colors = self.theme_colors()
        colors = self.colors
        self.root.configure(bg=colors["form"])
        for widget in (self.header, self.summary, self.body, self.footer):
            widget.configure(bg=colors["form"])
        self.logo.configure(bg=colors["accent"], fg="white")
        self.title_label.configure(bg=colors["form"], fg=colors["text"])
        self.subtitle_label.configure(bg=colors["form"], fg=colors["secondary"])
        self.quota_status.configure(bg=colors["form"], fg=colors["secondary"])
        self.canvas.configure(bg=colors["form"])
        self.log_text.configure(bg=colors["input"], fg=colors["text"], insertbackground=colors["text"])
        self.log_title.configure(bg=colors["form"], fg=colors["secondary"])
        self.scrollbar.configure(bg=colors["panel"], troughcolor=colors["form"], activebackground=colors["border"])
        self.refresh_button.configure(bg=colors["accent"], fg="white", activebackground=colors["accent_hover"], activeforeground="white")
        for label, value_label in self.summary_labels:
            tile = label.master
            tile.configure(bg=colors["panel"])
            label.configure(bg=colors["panel"], fg=colors["muted"])
            value_label.configure(bg=colors["panel"], fg=colors["text"])
        self.redraw_cards()

    def install_native_drag_guard(self) -> None:
        """Freeze Tk child-window painting during native move/resize gestures."""
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_wndproc = user32.GetWindowLongPtrW
            set_wndproc = user32.SetWindowLongPtrW
            call_wndproc = user32.CallWindowProcW
            def_wndproc = user32.DefWindowProcW
            send_message = user32.SendMessageW
            redraw_window = user32.RedrawWindow
            enum_children = user32.EnumChildWindows

            get_wndproc.argtypes = [wintypes.HWND, ctypes.c_int]
            get_wndproc.restype = ctypes.c_void_p
            set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            set_wndproc.restype = ctypes.c_void_p
            call_wndproc.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            call_wndproc.restype = ctypes.c_ssize_t
            def_wndproc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            def_wndproc.restype = ctypes.c_ssize_t
            send_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            send_message.restype = ctypes.c_ssize_t
            redraw_window.argtypes = [wintypes.HWND, wintypes.LPVOID, wintypes.HRGN, wintypes.UINT]
            redraw_window.restype = wintypes.BOOL
            enum_children.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
            enum_children.restype = wintypes.BOOL

            hwnd = int(self.root.winfo_id())
            old_wndproc = get_wndproc(hwnd, GWLP_WNDPROC)
            if not old_wndproc:
                return
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            enum_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            self.native_hwnd = hwnd
            self.native_user32 = user32
            self.native_old_wndproc = old_wndproc
            self.native_wndproc = callback_type(self.native_window_proc)
            self.native_enum_proc = enum_callback_type(self.native_enum_child)
            set_wndproc(hwnd, GWLP_WNDPROC, ctypes.cast(self.native_wndproc, ctypes.c_void_p))
            self.native_call_wndproc = call_wndproc
            self.native_def_wndproc = def_wndproc
            self.native_send_message = send_message
            self.native_redraw_window = redraw_window
            self.native_enum_children = enum_children
        except Exception:
            self.native_hwnd = None
            self.native_user32 = None
            self.native_old_wndproc = None
            self.native_wndproc = None
            self.native_enum_proc = None

    def native_enum_child(self, hwnd: int, _lparam: int) -> bool:
        self.native_redraw_windows.append(int(hwnd))
        return True

    def set_native_redraw(self, enabled: bool) -> None:
        if self.native_hwnd is None or self.native_user32 is None:
            return
        if not enabled:
            self.native_redraw_windows = [self.native_hwnd]
            self.native_enum_children(self.native_hwnd, self.native_enum_proc, 0)
        for hwnd in list(self.native_redraw_windows):
            self.native_send_message(hwnd, WM_SETREDRAW, 1 if enabled else 0, 0)
            if enabled:
                self.native_redraw_window(
                    hwnd,
                    None,
                    None,
                    RDW_INVALIDATE | RDW_ERASE | RDW_UPDATENOW | RDW_ALLCHILDREN,
                )
        if enabled:
            self.native_redraw_windows.clear()

    def native_window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_ENTERSIZEMOVE and not self.native_dragging:
            self.native_dragging = True
            self.set_native_redraw(False)
        result = self.native_call_wndproc(self.native_old_wndproc, hwnd, message, wparam, lparam)
        if message == WM_EXITSIZEMOVE and self.native_dragging:
            self.set_native_redraw(True)
            self.native_dragging = False
            try:
                self.root.after_idle(self.apply_layout_redraw)
            except tk.TclError:
                pass
        return int(result)

    def uninstall_native_drag_guard(self) -> None:
        if self.native_user32 is None or self.native_hwnd is None:
            return
        try:
            if self.native_dragging:
                self.set_native_redraw(True)
                self.native_dragging = False
            if self.native_old_wndproc:
                self.native_user32.SetWindowLongPtrW(
                    self.native_hwnd,
                    GWLP_WNDPROC,
                    self.native_old_wndproc,
                )
        except Exception:
            pass
        finally:
            self.native_hwnd = None
            self.native_user32 = None
            self.native_old_wndproc = None
            self.native_wndproc = None
            self.native_enum_proc = None

    def on_mousewheel(self, event: tk.Event[tk.Misc]) -> str:
        units = -int(getattr(event, "delta", 0) / 120)
        if units:
            self.canvas.yview_scroll(units, "units")
        return "break"

    def on_canvas_configure(self, event: tk.Event[tk.Misc]) -> None:
        self.pending_canvas_width = int(getattr(event, "width", 0) or 0)
        self.schedule_layout_redraw()

    def schedule_layout_redraw(self) -> None:
        if self.layout_redraw_job is not None:
            try:
                self.root.after_cancel(self.layout_redraw_job)
            except tk.TclError:
                pass
        self.layout_redraw_job = self.root.after(180, self.apply_layout_redraw)

    def apply_layout_redraw(self) -> None:
        self.layout_redraw_job = None
        self.redraw_cards()

    def current_account_text(self) -> str:
        for account in self.accounts:
            if account.account_id == self.current_id:
                return account.email or account.account_id
        return self.current_id or "未识别"

    def update_summary(self, quota_status: str | None = None) -> None:
        if not hasattr(self, "summary_value_labels"):
            return
        self.summary_value_labels[0].configure(text=str(len(self.accounts)))
        self.summary_value_labels[1].configure(text=self.current_account_text())
        if quota_status is not None:
            self.summary_value_labels[2].configure(text=quota_status)

    def set_quota_status(self, text: str) -> None:
        self.quota_status.configure(text=text)
        self.update_summary(text)

    def draw_quota_bar(self, account_id: str, bucket_name: str) -> None:
        entry = self.cards.get(account_id)
        if not entry:
            return
        items = entry["items"]
        prefix = "five" if bucket_name == "fiveHour" else "weekly"
        bucket = self.cache_item(account_id).get(bucket_name)
        colors = self.colors
        self.canvas.itemconfigure(items[f"{prefix}_bar_bg"], fill=colors["bar_bg"])
        if not isinstance(bucket, dict):
            x, y, _width, height = items[f"{prefix}_bar_bounds"]
            self.canvas.coords(items[f"{prefix}_bar"], x, y, x, y + height)
            return
        try:
            fraction = float(bucket.get("remainingFraction"))
        except (TypeError, ValueError):
            x, y, _width, height = items[f"{prefix}_bar_bounds"]
            self.canvas.coords(items[f"{prefix}_bar"], x, y, x, y + height)
            return
        fraction = max(0.0, min(1.0, fraction))
        x, y, width, height = items[f"{prefix}_bar_bounds"]
        fill_width = max(1, round(width * fraction)) if fraction > 0 else 0
        fill = colors["warning"] if fraction <= 0.2 else colors["accent"]
        self.canvas.itemconfigure(items[f"{prefix}_bar"], fill=fill)
        self.canvas.coords(items[f"{prefix}_bar"], x, y, x + fill_width, y + height)

    def set_canvas_text_if_changed(self, item_id: int, text: str) -> None:
        if self.canvas.itemcget(item_id, "text") != text:
            self.canvas.itemconfigure(item_id, text=text)

    def update_countdowns(self) -> None:
        """Update only the countdown labels; quota bars remain untouched."""
        for account in self.accounts:
            entry = self.cards.get(account.account_id)
            if not entry:
                continue
            item = self.cache_item(account.account_id)
            items = entry["items"]
            self.set_canvas_text_if_changed(items["five"], self.format_bucket(item.get("fiveHour")))
            self.set_canvas_text_if_changed(items["weekly"], self.format_bucket(item.get("weekly")))

    def write_log(self, message: str, level: str = "INFO") -> None:
        message = safe_text(message)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message}"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
        if hasattr(self, "events"):
            self.events.put(("log", line))

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def cache_item(self, account_id: str) -> dict[str, Any]:
        value = self.cache.setdefault(account_id, {"fiveHour": None, "weekly": None, "observedAt": None, "lastAttemptAt": None, "error": None})
        return value

    @staticmethod
    def format_countdown(value: Any) -> str:
        reset = parse_time(value)
        if reset is None:
            return "刷新时间未知"
        seconds = int((reset - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            return "即将刷新"
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        if days:
            return f"还有 {days}天 {hours:02d}:{minutes:02d}:{seconds:02d} 刷新"
        return f"还有 {hours:02d}:{minutes:02d}:{seconds:02d} 刷新"

    def format_bucket(self, bucket: Any) -> str:
        if not isinstance(bucket, dict) or bucket.get("remainingFraction") is None:
            return "暂无数据"
        percent = round(float(bucket["remainingFraction"]) * 100)
        return f"剩余 {percent}%  ·  {self.format_countdown(bucket.get('resetTime'))}"

    @staticmethod
    def observed_text(value: Any) -> str:
        parsed = parse_time(value)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S") if parsed else "暂无成功数据"

    def update_card(self, account_id: str) -> None:
        entry = self.cards.get(account_id)
        if not entry:
            return
        colors = self.colors
        item = self.cache_item(account_id)
        items = entry["items"]
        current = account_id == self.current_id
        self.set_canvas_text_if_changed(items["status"], "当前已写入 Windows 凭据" if current else "可切换")
        self.canvas.itemconfigure(items["status"], fill=colors["success"] if current else colors["secondary"])
        five_bucket = item.get("fiveHour")
        weekly_bucket = item.get("weekly")
        self.set_canvas_text_if_changed(items["five"], self.format_bucket(five_bucket))
        self.set_canvas_text_if_changed(items["weekly"], self.format_bucket(weekly_bucket))
        self.draw_quota_bar(account_id, "fiveHour")
        self.draw_quota_bar(account_id, "weekly")
        self.set_canvas_text_if_changed(items["updated"], f"数据更新时间：{self.observed_text(item.get('observedAt'))}")
        error = str(item.get("error") or "")
        if error and item.get("observedAt"):
            error = f"查询失败（已保留上次数据）：{error}"
        elif error:
            error = f"查询失败：{error}"
        self.set_canvas_text_if_changed(items["error"], error)
        button_color = colors["success"] if current else colors["accent"]
        self.canvas.itemconfigure(items["strip"], fill=button_color if current else colors["accent"])
        self.canvas.itemconfigure(items["button"], fill=button_color)
        self.canvas.itemconfigure(items["button_text"], text="当前账号" if current else "切换到此账号")

    def update_cards(self) -> None:
        for account in self.accounts:
            self.update_card(account.account_id)
        self.update_summary()

    def switch_account(self, account: Account) -> None:
        if self.switching or account.account_id == self.current_id:
            return
        self.switching = True
        self.update_cards()
        self.write_log(f"开始切换到账号 {account.account_id}（{account.email}）。")

        def worker() -> None:
            try:
                self.core.switch(account)
                self.events.put(("switch_ok", account))
            except Exception as exc:
                self.events.put(("switch_error", safe_text(exc)))

        threading.Thread(target=worker, name="agy-switch", daemon=True).start()

    def start_quota_refresh(self) -> None:
        if self.quota_refreshing or not bool(self.core.config.get("queryQuota", True)):
            return
        self.quota_refreshing = True
        self.quota_successes = 0
        self.quota_failures = 0
        self.refresh_button.configure(state="disabled")
        self.set_quota_status("正在查询额度……")
        self.write_log(f"开始查询 {len(self.accounts)} 个账号的额度。")

        def worker() -> None:
            for account in list(self.accounts):
                self.events.put(("quota_result", self.core.quota(account)))
            self.events.put(("quota_done", None))

        threading.Thread(target=worker, name="agy-quota", daemon=True).start()

    def poll_events(self) -> None:
        handled_event = False
        try:
            while True:
                kind, value = self.events.get_nowait()
                handled_event = True
                if kind == "log":
                    self.append_log(value)
                elif kind == "switch_ok":
                    account: Account = value
                    self.current_id = account.account_id
                    self.switching = False
                    self.update_cards()
                    self.write_log(f"账号 {account.account_id} 已切换成功。Antigravity 未自动重新启动。")
                    messagebox.showinfo("切换成功", f"已切换到：{account.email}\n\n请按你原来的方式重新启动 Antigravity。", parent=self.root)
                elif kind == "switch_error":
                    self.switching = False
                    self.update_cards()
                    self.write_log(f"切换失败：{value}", "ERROR")
                    messagebox.showerror("切换失败", f"{value}\n\n凭据写入只会在确认 Antigravity 退出后执行。", parent=self.root)
                elif kind == "quota_result":
                    result = value
                    item = self.cache_item(result["id"])
                    item["lastAttemptAt"] = result["attemptedAt"]
                    if result["success"]:
                        self.quota_successes += 1
                        item["fiveHour"] = result.get("fiveHour")
                        item["weekly"] = result.get("weekly")
                        item["observedAt"] = result["attemptedAt"]
                        item["error"] = None
                        self.write_log(f"账号 {result['id']} 的额度已更新。")
                    else:
                        self.quota_failures += 1
                        item["error"] = result.get("error") or "未知错误"
                        self.write_log(f"账号 {result['id']} 的额度查询失败：{item['error']}", "WARN")
                    self.update_card(result["id"])
                elif kind == "quota_done":
                    self.quota_refreshing = False
                    self.refresh_button.configure(state="normal")
                    stamp = datetime.now().strftime("%H:%M:%S")
                    if self.quota_failures and self.quota_successes:
                        status = f"部分更新 · {stamp}"
                    elif self.quota_failures:
                        status = f"查询失败 · {stamp}"
                    else:
                        status = f"额度更新完成 · {stamp}"
                    self.set_quota_status(status)
                    self.save_cache()
        except queue.Empty:
            pass
        self.root.after(80 if handled_event else 350, self.poll_events)

    def save_cache(self) -> None:
        path = resolve_path(str(self.core.config["quotaCacheFile"]))
        try:
            path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            self.write_log(f"额度缓存保存失败：{safe_text(exc)}", "WARN")

    def tick(self) -> None:
        self.update_countdowns()
        new_dark = self.system_dark()
        if new_dark != self.dark:
            self.dark = new_dark
            self.apply_theme()
            self.update_cards()
        self.root.after(1000, self.tick)

    def close(self) -> None:
        self.uninstall_native_drag_guard()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def load_cache(config: dict[str, Any]) -> dict[str, Any]:
    path = resolve_path(str(config["quotaCacheFile"]))
    if not path.exists():
        return {}
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def run_self_test(core: AccountCore, accounts: list[Account]) -> int:
    credential_present = False
    credential_bytes = 0
    try:
        value = core.credentials.read(CREDENTIAL_TARGET)
        credential_present = value is not None
        credential_bytes = len(value or b"")
    except Exception:
        pass
    print(json.dumps({
        "accountCount": len(accounts),
        "accounts": [{"id": account.account_id, "email": account.email} for account in accounts],
        "credentialPresent": credential_present,
        "credentialBytes": credential_bytes,
        "currentAccount": core.current_account_id(accounts),
    }, ensure_ascii=False, indent=2))
    return 0


def run_quota_only(core: AccountCore, accounts: list[Account]) -> int:
    print(json.dumps([core.quota(account) for account in accounts], ensure_ascii=False, separators=(",", ":")))
    return 0


def main() -> int:
    if sys.platform != "win32":
        print("此工具仅支持 Windows。", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--quota-only", action="store_true")
    args, _unknown = parser.parse_known_args()
    config = load_config()
    core = AccountCore(config)
    try:
        accounts = core.scan_accounts()
    except Exception as exc:
        print(safe_text(exc), file=sys.stderr)
        return 1
    if args.self_test:
        return run_self_test(core, accounts)
    if args.quota_only:
        return run_quota_only(core, accounts)

    app = SwitcherApp(core, accounts, load_cache(config))
    core.log_fn = app.write_log
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Application defaults, local configuration loading, and schema validation."""

from __future__ import annotations

import json
import ipaddress
import math
import os
from pathlib import Path
from typing import Any

from cache_store import atomic_save_json
from portable_runtime import application_root, portable_paths

ROOT = application_root()
PORTABLE_PATHS = portable_paths(ROOT)
RESOURCE_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PORTABLE_PATHS["config"]
LOCAL_CONFIG_PATH = ROOT / "local-data" / "account-switcher.config.json"
LEGACY_CONFIG_PATH = ROOT / "account-switcher.config.json"
OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"

DEFAULT_CONFIG: dict[str, Any] = {
    "credentialsDirectory": "Data/credentials",
    "antigravityExecutable": "Antigravity.exe",
    "proxy": "",
    "noProxy": "localhost,127.0.0.1",
    "logFile": "Data/logs/account-switcher.log",
    "quotaCacheFile": "Data/cache/quota-cache.json",
    "queryQuota": True,
    "requestTimeoutSeconds": 20,
    "quotaDeadlineSeconds": 45,
    "quotaMaxWorkers": 2,
    "closeTimeoutSeconds": 15,
    "oauthClientId": OAUTH_CLIENT_ID,
    "oauthClientSecret": "",
}
_STRING_FIELDS = ("credentialsDirectory", "antigravityExecutable", "proxy", "noProxy", "logFile", "quotaCacheFile", "oauthClientId", "oauthClientSecret")
_POSITIVE_NUMBER_FIELDS = ("requestTimeoutSeconds", "quotaDeadlineSeconds", "closeTimeoutSeconds")


class ConfigError(ValueError):
    """Invalid local configuration with an actionable message."""


def normalize_proxy_setting(value: str) -> str:
    """Normalize a proxy entered as a local port or an explicit HTTP URL."""
    if not isinstance(value, str):
        raise ConfigError("代理设置必须是文本。")
    text = value.strip()
    if not text:
        return ""
    if text.isdecimal():
        try:
            port = int(text)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            raise ConfigError("本地代理端口必须是 1 到 65535 之间的整数。")
        return f"http://127.0.0.1:{port}"
    from urllib.parse import urlsplit

    # urlsplit intentionally strips some control characters; reject them before
    # parsing so the original input can never be persisted with hidden chars.
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError("代理地址无效；请输入 1 到 65535 的端口，或带端口的 HTTP 代理 URL（不支持 SOCKS）。")
    try:
        parsed = urlsplit(text)
        port = parsed.port
        hostname = parsed.hostname or ""
        if ":" in hostname:
            ipaddress.IPv6Address(hostname)
            valid_host = True
        else:
            try:
                ipaddress.IPv4Address(hostname)
                valid_host = True
            except ValueError:
                labels = hostname.rstrip(".").split(".")
                valid_host = not (all(label.isdecimal() for label in labels) and len(labels) == 4) and bool(hostname) and all(
                    label and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
                    and all(character.isalnum() or character == "-" for character in label)
                    for label in labels
                )
        valid = (parsed.scheme.lower() == "http" and bool(parsed.hostname)
                 and port is not None and 1 <= port <= 65535
                 and valid_host and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("代理地址无效；请输入 1 到 65535 的端口，或带端口的 HTTP 代理 URL（不支持 SOCKS）。")
    return text


def save_proxy_setting(value: str, path: str | os.PathLike[str] | None = None) -> str:
    """Persist only the proxy field, merging against the current on-disk object.

    Environment-derived secrets are intentionally never used when constructing
    the file. Existing file fields, including unknown fields, remain untouched.
    """
    proxy = normalize_proxy_setting(value)
    config_path = Path(path) if path is not None else active_config_path()
    try:
        source_path = config_path
        if path is None and not config_path.exists():
            source_path = next((candidate for candidate in config_candidates() if candidate.exists()), config_path)
            config_path = source_path
        if source_path.exists():
            raw = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ConfigError("配置文件内容必须是 JSON 对象，代理设置未保存。")
            merged = raw
        else:
            merged = dict(DEFAULT_CONFIG)
        merged["proxy"] = proxy
        atomic_save_json(config_path, merged)
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ConfigError(f"无法保存代理设置到配置文件：{exc}") from exc
    return proxy


def validate_config(value: Any, *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("配置内容必须是 JSON 对象。")
    result = dict(DEFAULT_CONFIG)
    result.update(value)
    for field in _STRING_FIELDS:
        if not isinstance(result.get(field), str):
            raise ConfigError(f"配置项 {field} 必须是字符串。")
    for field in ("credentialsDirectory", "antigravityExecutable", "logFile", "quotaCacheFile"):
        if not result[field].strip():
            raise ConfigError(f"配置项 {field} 不能为空。")
    for field in _POSITIVE_NUMBER_FIELDS:
        number = result.get(field)
        try:
            valid = not isinstance(number, bool) and isinstance(number, (int, float)) and math.isfinite(float(number)) and number > 0
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ConfigError(f"配置项 {field} 必须是大于 0 的数字。")
    workers = result.get("quotaMaxWorkers")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ConfigError("配置项 quotaMaxWorkers 必须是 1 到 4 之间的整数。")
    if not isinstance(result.get("queryQuota"), bool):
        raise ConfigError("配置项 queryQuota 必须是 true 或 false。")
    if not result["oauthClientId"].strip():
        result["oauthClientId"] = OAUTH_CLIENT_ID
    env = os.environ if environ is None else environ
    result["oauthClientSecret"] = env.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET") or result["oauthClientSecret"]
    return result


def load_config(path: str | os.PathLike[str] | None = None, *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else active_config_path()
    if not config_path.exists():
        return validate_config({}, environ=environ)
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}：{exc}") from exc
    try:
        return validate_config(loaded, environ=environ)
    except ConfigError as exc:
        raise ConfigError(f"配置文件 {config_path} 无效：{exc}") from exc


def config_candidates() -> tuple[Path, ...]:
    """Return supported config locations in precedence order."""
    return (CONFIG_PATH, LOCAL_CONFIG_PATH, LEGACY_CONFIG_PATH)


def active_config_path() -> Path:
    return next((candidate for candidate in config_candidates() if candidate.exists()), CONFIG_PATH)


def resolve_path(value: str, *, root: Path = ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path

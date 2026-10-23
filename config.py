"""Application defaults, local configuration loading, and schema validation."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "local-data" / "account-switcher.config.json"
LEGACY_CONFIG_PATH = ROOT / "account-switcher.config.json"
OAUTH_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"

DEFAULT_CONFIG: dict[str, Any] = {
    "credentialsDirectory": "local-data/credentials",
    "antigravityExecutable": "Antigravity.exe",
    "proxy": "",
    "noProxy": "localhost,127.0.0.1",
    "logFile": "local-data/account-switcher.log",
    "quotaCacheFile": "local-data/quota-cache.json",
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
    config_path = Path(path) if path is not None else (CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH)
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


def resolve_path(value: str, *, root: Path = ROOT) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path

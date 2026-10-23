"""Validation and atomic persistence helpers for local JSON cache data."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


_BUCKET_TEXT_FIELDS = (
    "resetTime",
    "displayName",
    "description",
    "window",
    "groupName",
)


def _optional_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _validated_bucket(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    fraction = value.get("remainingFraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    fraction = float(fraction)
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        return None

    bucket: dict[str, Any] = {"remainingFraction": fraction}
    for field in _BUCKET_TEXT_FIELDS:
        item = value.get(field)
        if item is None:
            bucket[field] = "" if field == "resetTime" else None
        elif isinstance(item, str):
            if field == "resetTime" and item and _optional_time(item) is None:
                return None
            bucket[field] = item
        else:
            return None
    return bucket


def validate_cache(value: Any) -> dict[str, dict[str, Any]]:
    """Return valid per-account cache data, dropping malformed records/fields."""
    if not isinstance(value, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for account_id, raw in value.items():
        if not isinstance(account_id, str) or not account_id or not isinstance(raw, dict):
            continue
        item = {
            "fiveHour": _validated_bucket(raw.get("fiveHour")),
            "weekly": _validated_bucket(raw.get("weekly")),
            "observedAt": _optional_time(raw.get("observedAt")),
            "lastAttemptAt": _optional_time(raw.get("lastAttemptAt")),
            "error": raw.get("error") if isinstance(raw.get("error"), str) else None,
        }
        result[account_id] = item
    return result


def atomic_save_json(path: str | os.PathLike[str], value: Any) -> None:
    """Serialize JSON then atomically replace *path* using a sibling temp file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

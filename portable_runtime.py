"""Portable installation paths shared by source and frozen builds."""
from __future__ import annotations

import sys
from pathlib import Path


def application_root(*, frozen: bool | None = None, executable: str | Path | None = None,
                     source_file: str | Path | None = None) -> Path:
    """Return the writable portable root (beside the EXE when frozen)."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return Path(executable if executable is not None else sys.executable).resolve().parent
    return Path(source_file if source_file is not None else __file__).resolve().parent


def portable_paths(root: Path) -> dict[str, Path]:
    data = root / "Data"
    return {
        "root": root,
        "data": data,
        "config": data / "settings" / "account-switcher.config.json",
        "credentials": data / "credentials",
        "log": data / "logs" / "account-switcher.log",
        "quota_cache": data / "cache" / "quota-cache.json",
        "other": root / "Other",
    }

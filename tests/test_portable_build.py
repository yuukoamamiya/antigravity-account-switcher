"""Checks for architecture validation in the portable build tooling."""
from __future__ import annotations

import struct
import importlib.util
import zipfile
from pathlib import Path

import pytest

_build_spec = importlib.util.spec_from_file_location(
    "portable_build_tool", Path(__file__).resolve().parents[1] / "packaging" / "build_portable.py"
)
assert _build_spec is not None and _build_spec.loader is not None
_build_module = importlib.util.module_from_spec(_build_spec)
_build_spec.loader.exec_module(_build_module)
pe_machine = _build_module.pe_machine
source_snapshot = _build_module.source_snapshot


def _write_pe(path: Path, machine: int) -> None:
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, machine)
    path.write_bytes(data)


def test_pe_machine_reads_amd64_header(tmp_path: Path) -> None:
    executable = tmp_path / "app.exe"
    _write_pe(executable, 0x8664)

    assert pe_machine(executable) == 0x8664


def test_pe_machine_does_not_accept_32_bit_or_arm64(tmp_path: Path) -> None:
    executable = tmp_path / "app.exe"
    for machine in (0x14C, 0xAA64):
        _write_pe(executable, machine)
        assert pe_machine(executable) != 0x8664


def test_pe_machine_rejects_non_pe_file(tmp_path: Path) -> None:
    executable = tmp_path / "app.exe"
    executable.write_bytes(b"not an executable")

    with pytest.raises(SystemExit, match="not a Windows PE"):
        pe_machine(executable)


def test_source_snapshot_uses_allowlisted_history_free_inputs(tmp_path: Path) -> None:
    archive_path = tmp_path / "version-source.zip"

    digest = source_snapshot(archive_path)

    assert len(digest) == 64
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    assert "AntigravityAccountSwitcherPortable-source/LICENSE" in names
    assert any(name.endswith("/packaging/build_portable.py") for name in names)
    assert any(name.endswith("/packaging/portable.spec") for name in names)
    assert any(name.endswith("/packaging/licenses/Tcl-8.6.15-license.terms") for name in names)
    assert any(name.endswith("/.gitignore") for name in names)
    assert not any(any(blocked in name.split("/") for blocked in ("reference", "local-reference", "local-data", "credentials", ".git")) for name in names)

"""Build and validate the Windows x64 portable distribution."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import struct
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
PRODUCT = "AntigravityAccountSwitcherPortable"
EXE_NAME = PRODUCT + ".exe"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def safe_remove_tree(target: Path, parent: Path) -> None:
    target_resolved = target.resolve()
    parent_resolved = parent.resolve()
    require(
        target_resolved != parent_resolved and parent_resolved in target_resolved.parents,
        f"Refusing to remove a directory outside its designated parent: {target}",
    )
    shutil.rmtree(target_resolved)


def pe_machine(path: Path) -> int:
    data = path.read_bytes()
    require(data[:2] == b"MZ", "Output executable is not a Windows PE file")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    require(data[pe_offset : pe_offset + 4] == b"PE\0\0", "Invalid PE header")
    return struct.unpack_from("<H", data, pe_offset + 4)[0]


def source_snapshot(destination: Path) -> str:
    """Create a history-free source archive from an explicit public-file allowlist."""
    # This is a deliberate path allowlist; the repository tree is never zipped wholesale.
    top_level = {
        "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
        "VERSION", "requirements-build.txt", "account-switcher.config.example.json",
        "antigravity_account_switcher.py", "account_core.py", "cache_store.py", "common.py",
        "config.py", "process_control.py", "quota_client.py", "runtime_control.py",
        "switcher_ui.py", "portable_runtime.py", "启动账号切换器.cmd", "启动账号切换器.vbs",
        ".gitignore", "pytest.ini",
        ".github/workflows/ci.yml", ".github/workflows/portable-release.yml",
    }
    allowed: list[tuple[str, bytes]] = []
    candidates = [(name, ROOT / name) for name in top_level]
    candidates.extend((path.relative_to(ROOT).as_posix(), path) for path in (ROOT / "tests").glob("test_*.py"))
    candidates.append(("tests/conftest.py", ROOT / "tests" / "conftest.py"))
    candidates.extend((path.relative_to(ROOT).as_posix(), path) for path in (ROOT / "packaging").glob("*.py"))
    candidates.extend((path.relative_to(ROOT).as_posix(), path) for path in (ROOT / "packaging").glob("*.spec"))
    candidates.extend((path.relative_to(ROOT).as_posix(), path) for path in (ROOT / "packaging" / "licenses").glob("*"))
    blocked_parts = {".git", ".pytest_cache", "reference", "local-reference", "local-data", "Data", "credentials", "dist", "build", ".build-env"}
    for name, path in candidates:
        if set(Path(name).parts) & blocked_parts:
            continue
        if path.is_file() and "credential" not in name.lower() and not name.endswith((".log", ".pyc", ".pyo")):
            allowed.append((name, path.read_bytes()))
    require(any(n == "LICENSE" for n, _ in allowed), "LICENSE is required before packaging")
    require(any(n == "README.md" for n, _ in allowed), "README.md is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(allowed):
            archive.writestr(f"{PRODUCT}-source/{name}", content)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def copy_licenses(other: Path) -> None:
    other.mkdir(parents=True, exist_ok=True)
    license_file = ROOT / "LICENSE"
    require(license_file.is_file(), "LICENSE is required before packaging")
    shutil.copy2(license_file, other / "LICENSE")
    candidates = [
        Path(sys.prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "licenses" / "Python-3.14.6-license.txt",
    ]
    python_license = next((candidate for candidate in candidates if candidate.is_file()), None)
    require(python_license is not None, "Could not locate the Python runtime license")
    shutil.copy2(python_license, other / "Python-LICENSE.txt")
    pyinstaller_dist = importlib.metadata.distribution("pyinstaller")
    pyinstaller_license = next(
        (pyinstaller_dist.locate_file(p) for p in pyinstaller_dist.files or [] if str(p).endswith("licenses/COPYING.txt")),
        None,
    )
    require(pyinstaller_license is not None and pyinstaller_license.is_file(), "Could not locate the PyInstaller license and bootloader exception")
    shutil.copy2(pyinstaller_license, other / "PyInstaller-COPYING.txt")
    packaging_dist = importlib.metadata.distribution("packaging")
    apache_license = next(
        (packaging_dist.locate_file(p) for p in packaging_dist.files or [] if str(p).endswith("licenses/LICENSE.APACHE")),
        None,
    )
    require(apache_license is not None and apache_license.is_file(), "Could not locate the Apache 2.0 license for embedded PyInstaller runtime hooks")
    shutil.copy2(apache_license, other / "Apache-2.0.txt")
    license_sources = ROOT / "packaging" / "licenses"
    shutil.copy2(license_sources / "CPython-3.14.6-license.rst", other / "Python-Third-Party-Licenses.rst")
    shutil.copy2(license_sources / "Tcl-8.6.15-license.terms", other / "Tcl-LICENSE.txt")
    shutil.copy2(license_sources / "SOURCES.md", other / "LICENSE-SOURCES.md")
    tk_root = Path(sys.base_prefix) / "tcl"
    require(tk_root.is_dir(), "Could not locate Tcl/Tk runtime license files")
    license_files = sorted(
        p for p in tk_root.glob("tk*/license.terms") if p.is_file()
    )
    require(license_files, "Could not locate Tcl/Tk license terms")
    shutil.copy2(license_files[0], other / "Tk-LICENSE.txt")
    (other / "THIRD-PARTY-NOTICES.txt").write_text(
        "This portable application bundles Python 3.14.6, Tcl/Tk 8.6.15, and the PyInstaller bootloader/runtime hooks. Their license texts and pinned source URLs with SHA-256 hashes are included in this folder.\n"
        "Python-Third-Party-Licenses.rst contains the CPython third-party notices; Tcl-LICENSE.txt and Tk-LICENSE.txt contain their license terms.\n"
        "PyInstaller-COPYING.txt contains the GPL terms and bootloader exception; Apache-2.0.txt covers embedded PyInstaller runtime hooks.\n",
        encoding="utf-8",
    )


def main() -> None:
    require(sys.platform == "win32", "Build must run on Windows")
    require(struct.calcsize("P") * 8 == 64, "Build requires 64-bit Python")
    require(sys.version_info[:2] == (3, 14), "Build requires CPython 3.14.x")
    require(os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() in {"AMD64", "ARM64"}, "Unsupported host architecture")
    try:
        import PyInstaller
    except ImportError as exc:
        raise SystemExit("Install requirements-build.txt in the active build environment") from exc
    require(PyInstaller.__version__ == "6.21.0", f"Expected PyInstaller 6.21.0, found {PyInstaller.__version__}")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    require(version and all(c.isalnum() or c in ".-" for c in version), "Invalid VERSION")
    DIST.mkdir(exist_ok=True)
    source_archive = DIST / f"{PRODUCT}-{version}-source.zip"
    zip_path = DIST / f"{PRODUCT}-{version}-win10-x64-portable.zip"
    sums_path = DIST / "SHA256SUMS"
    smoke_summary_path = DIST / "smoke-test-report.json"
    require(not source_archive.exists() and not zip_path.exists() and not sums_path.exists() and not smoke_summary_path.exists(),
            "Distribution artifacts already exist; move them aside before rebuilding")
    dist_temp = Path(tempfile.mkdtemp(prefix=".portable-build-", dir=DIST))
    source_temp = dist_temp / source_archive.name
    source_digest = source_snapshot(source_temp)
    if WORK.exists():
        resolved_work = WORK.resolve()
        require(resolved_work != ROOT.resolve() and ROOT.resolve() in resolved_work.parents, "Refusing to clean a work directory outside the repository build folder")
        safe_remove_tree(WORK, ROOT / "build")
    stage = dist_temp / PRODUCT
    from PyInstaller.__main__ import run as pyinstaller_run
    pyinstaller_run([
        "--noconfirm", "--clean", "--distpath", str(dist_temp), "--workpath", str(WORK),
        str(ROOT / "packaging" / "portable.spec"),
    ])

    exe = stage / EXE_NAME
    require(exe.is_file(), "PyInstaller did not place the executable at the distribution root")
    require(pe_machine(exe) == 0x8664, "Executable PE machine is not AMD64 x64")
    (stage / "Data" / "settings").mkdir(parents=True, exist_ok=True)
    (stage / "Data" / "logs").mkdir(parents=True, exist_ok=True)
    (stage / "Data" / "cache").mkdir(parents=True, exist_ok=True)
    (stage / "Data" / "credentials").mkdir(parents=True, exist_ok=True)
    for sub in ("settings", "logs", "cache", "credentials"):
        (stage / "Data" / sub / ".keep").write_text("", encoding="ascii")
    copy_licenses(stage / "Other")
    shutil.copy2(ROOT / "README.md", stage / "Other" / "README.md")
    (stage / "Other" / "README.txt").write_text(
        "Antigravity 账号切换器便携版\n\n"
        "双击 AntigravityAccountSwitcherPortable.exe 启动。用户设置、凭据、日志和缓存以明文保存在 Data/。\n"
        "目录布局借鉴 PortableApps 风格，不表示通过 PortableApps.com Format 认证。\n"
        "使用说明请阅读本目录中的 README.md。\n",
        encoding="utf-8",
    )
    smoke_parent = Path(tempfile.mkdtemp(prefix="Antigravity Smoke "))
    smoke_copy = smoke_parent / "应用 中文 空格" / PRODUCT
    require(not smoke_copy.exists(), f"Temporary smoke path already exists: {smoke_copy}")
    smoke_copy.parent.mkdir(parents=True)
    shutil.copytree(stage, smoke_copy)
    smoke_report = smoke_copy.parent / "smoke-test-report.json"
    import subprocess
    smoke_env = os.environ.copy()
    for key in tuple(smoke_env):
        if key.startswith("PYTHON") or key in {"TCL_LIBRARY", "TK_LIBRARY"}:
            smoke_env.pop(key)
    system_root = smoke_env.get("SystemRoot", r"C:\Windows")
    smoke_env["PATH"] = str(Path(system_root) / "System32")
    completed = subprocess.run(
        [str(smoke_copy / EXE_NAME), "--smoke-test", "--smoke-test-report", str(smoke_report)],
        cwd=smoke_parent, env=smoke_env, check=False, shell=False,
    )
    require(completed.returncode == 0, "Packaged executable smoke test returned a failure code")
    report = json.loads(smoke_report.read_text(encoding="utf-8"))
    require(report.get("ok") is True, "Packaged executable smoke test failed")
    reported_root = Path(report.get("checks", {}).get("root", ""))
    require(reported_root.resolve() == smoke_copy.resolve(), "Packaged executable resolved the wrong root from a different working directory")
    smoke_checks = {key: value for key, value in report.get("checks", {}).items() if key != "root"}
    require(smoke_checks and all(value is True for value in smoke_checks.values()), "Packaged executable smoke checks were incomplete")
    smoke_summary_path.write_text(json.dumps({
        "ok": True,
        "checks": {**smoke_checks, "unicodePath": True, "cleanEnvironment": True},
        "privateDataRead": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_remove_tree(smoke_parent, Path(tempfile.gettempdir()))
    for path in (stage / "Data").rglob("*"):
        if path.is_file():
            require(path.name == ".keep" and path.stat().st_size == 0, "Data folder unexpectedly contains private or non-empty files")

    zip_temp = dist_temp / zip_path.name
    with zipfile.ZipFile(zip_temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_dir():
                archive.writestr(f"{PRODUCT}/{path.relative_to(stage).as_posix()}/", "")
            if path.is_file():
                archive.write(path, f"{PRODUCT}/{path.relative_to(stage).as_posix()}")
    digest = hashlib.sha256(zip_temp.read_bytes()).hexdigest()
    source_digest = hashlib.sha256(source_temp.read_bytes()).hexdigest()
    shutil.copy2(source_temp, source_archive)
    shutil.copy2(zip_temp, zip_path)
    sums_path.write_text(
        f"{digest}  {zip_path.name}\n{source_digest}  {source_archive.name}\n", encoding="ascii"
    )
    safe_remove_tree(dist_temp, DIST)
    print(f"Built {zip_path}")
    print(f"SHA-256 {digest}")


if __name__ == "__main__":
    main()

"""Command-line entry point; account and GUI logic live in focused modules."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from account_core import Account, AccountCore, CredentialManager, CREDENTIAL_TARGET, antigravity_user_agent
from common import now_iso, parse_time, read_json, safe_text, sha256
from config import CONFIG_PATH, LEGACY_CONFIG_PATH, OAUTH_CLIENT_ID, ROOT, ConfigError, load_config, resolve_path
from quota_client import PROJECT_ENDPOINTS, QUOTA_ENDPOINTS, QuotaError
from runtime_control import SingleInstance

GoogleError = QuotaError


def __getattr__(name: str) -> Any:
    if name == "SwitcherApp":
        from switcher_ui import SwitcherApp
        return SwitcherApp
    raise AttributeError(name)


def load_cache(config: dict[str, Any]) -> dict[str, Any]:
    from switcher_ui import load_cache as implementation
    return implementation(config)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Antigravity 本地账号切换器")
    parser.add_argument("--self-test", action="store_true", help="扫描本机账号并输出诊断信息")
    parser.add_argument("--quota-only", action="store_true", help="只查询额度（会访问 Google 服务）")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("此工具仅支持 Windows。", file=sys.stderr)
        return 1
    single_instance = SingleInstance()
    try:
        if not single_instance.acquire():
            return 0
        try:
            config = load_config()
        except ConfigError as exc:
            message = safe_text(exc)
            print(message, file=sys.stderr)
            if not args.self_test and not args.quota_only:
                try:
                    import tkinter as tk
                    from tkinter import messagebox
                    root = tk.Tk()
                    root.withdraw()
                    messagebox.showerror("配置错误", message, parent=root)
                    root.destroy()
                except Exception:
                    pass
            return 2
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
        from switcher_ui import SwitcherApp, load_cache
        app = SwitcherApp(core, accounts, load_cache(config))
        core.log_fn = app.write_log
        app.run()
        return 0
    finally:
        single_instance.release()


if __name__ == "__main__":
    raise SystemExit(main())

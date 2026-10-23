"""Tkinter user interface for the account switcher."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from datetime import datetime, timezone
from typing import Any
from tkinter import messagebox

from account_core import Account, AccountCore, CREDENTIAL_TARGET
from cache_store import atomic_save_json, validate_cache
from common import parse_time, read_json, safe_text
from config import resolve_path
from quota_client import run_quota_batch
from runtime_control import SingleInstance, WorkerLifecycle

GWLP_WNDPROC = -4
WM_SETREDRAW = 0x000B
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_UPDATENOW = 0x0100
RDW_ALLCHILDREN = 0x0080

# Canvas card geometry is kept together so redraws, scrolling, and resize
# handling all use the same bounds.
CARD_HEIGHT = 124
CARD_GAP = 10
CARD_STEP = CARD_HEIGHT + CARD_GAP
CARD_INSET_X = 2
CARD_LEFT_WIDTH = 270
CARD_BUTTON_WIDTH = 124


class SwitcherApp:
    def __init__(self, core: AccountCore, accounts: list[Account], cache: dict[str, Any]) -> None:
        self.core = core
        self.accounts = accounts
        self.cache = cache
        self.current_id = core.current_account_id(accounts)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.lifecycle = WorkerLifecycle()
        self.closing = False
        self.switching = False
        self.adding_current = False
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
        self.card_fonts = {
            "small": tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=8),
            "value": tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=8, weight="bold"),
            "email": tkfont.Font(root=self.root, family="Microsoft YaHei UI", size=10, weight="bold"),
        }
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
        self.header = tk.Frame(self.root, bg=colors["form"], height=60)
        self.header.pack(fill="x", padx=24, pady=(16, 6))
        self.header.grid_columnconfigure(1, weight=1)
        self.logo = tk.Label(self.header, text="A", font=("Segoe UI", 13, "bold"), width=2, height=1, padx=4, pady=4)
        self.logo.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        self.title_label = tk.Label(self.header, text="Antigravity 账号", font=("Microsoft YaHei UI", 17, "bold"), anchor="w")
        self.title_label.grid(row=0, column=1, sticky="w")
        self.refresh_button = tk.Button(self.header, text="刷新额度", command=self.start_quota_refresh, width=11, relief="flat", cursor="hand2", bd=0)
        self.refresh_button.grid(row=0, column=3, padx=(12, 0))
        self.quota_status = tk.Label(self.header, text="额度查询未开始", font=("Microsoft YaHei UI", 8), anchor="e")
        self.quota_status.grid(row=1, column=3, sticky="e", pady=(2, 0))
        self.add_account_button = tk.Button(self.header, text="添加当前账号", command=self.add_current_account, width=13, relief="flat", cursor="hand2", bd=0)
        self.add_account_button.grid(row=0, column=2, padx=(12, 0))

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
        total_height = len(self.accounts) * CARD_STEP + 2
        self.canvas.configure(scrollregion=(0, 0, width, total_height))
        self.update_cards()

    def create_card_items(self, account: Account, index: int, width: int) -> None:
        colors = self.colors
        entry = self.cards[account.account_id]
        x0 = CARD_INSET_X
        x1 = max(width - CARD_INSET_X, 700)
        y0 = index * CARD_STEP + CARD_INSET_X
        y1 = y0 + CARD_HEIGHT
        button_x = x1 - CARD_BUTTON_WIDTH - 12
        quota_start = x0 + CARD_LEFT_WIDTH
        quota_gap = 12
        quota_width = max(120, (button_x - quota_start - quota_gap) // 2)
        weekly_x = quota_start + quota_width + quota_gap
        items: dict[str, Any] = {}

        items["card"] = self.canvas.create_rectangle(x0, y0, x1, y1, fill=colors["card"], outline=colors["border"])
        items["strip"] = self.canvas.create_rectangle(x0 + 1, y0 + 1, x0 + 5, y1 - 1, fill=colors["accent"], outline="")
        left_text_width = max(quota_start - (x0 + 35) - 12, 120)
        items["account_label"] = self.canvas.create_text(x0 + 35, y0 + 14, anchor="nw", text=self.fit_card_text(f"ACCOUNT {account.account_id}", ("Consolas", 8), left_text_width), font=("Consolas", 8), fill=colors["muted"])
        items["email"] = self.canvas.create_text(x0 + 35, y0 + 36, anchor="nw", text=self.fit_card_text(account.email, self.card_fonts["email"], left_text_width), font=("Microsoft YaHei UI", 10, "bold"), fill=colors["text"])
        items["status"] = self.canvas.create_text(x0 + 35, y0 + 62, anchor="nw", text="", width=left_text_width, font=("Microsoft YaHei UI", 8), fill=colors["secondary"])

        for prefix, panel_x, title in (("five", quota_start, "5 小时窗口"), ("weekly", weekly_x, "一周窗口")):
            panel_y = y0 + 8
            panel_w = quota_width
            items[f"{prefix}_panel"] = self.canvas.create_rectangle(panel_x, panel_y, panel_x + panel_w, panel_y + 66, fill=colors["panel"], outline="")
            items[f"{prefix}_title"] = self.canvas.create_text(panel_x + 10, panel_y + 6, anchor="nw", text=title, font=("Microsoft YaHei UI", 8), fill=colors["muted"])
            items[prefix] = self.canvas.create_text(panel_x + 10, panel_y + 22, anchor="nw", text="暂无数据", width=max(panel_w - 20, 80), font=("Microsoft YaHei UI", 8, "bold"), fill=colors["text"])
            bar_x = panel_x + 10
            bar_y = panel_y + 57
            bar_w = max(panel_w - 20, 20)
            items[f"{prefix}_bar_bg"] = self.canvas.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + 4, fill=colors["bar_bg"], outline="")
            items[f"{prefix}_bar"] = self.canvas.create_rectangle(bar_x, bar_y, bar_x, bar_y + 4, fill=colors["accent"], outline="")
            items[f"{prefix}_bar_bounds"] = (bar_x, bar_y, bar_w, 4)

        updated_width = max(button_x - quota_start - 8, 140)
        error_width = max(button_x - (x0 + 35) - 12, 160)
        items["updated_width"] = updated_width
        items["error_width"] = error_width
        items["updated"] = self.canvas.create_text(quota_start + 4, y0 + 82, anchor="nw", text="", width=updated_width, font=("Microsoft YaHei UI", 8), fill=colors["muted"])
        items["error"] = self.canvas.create_text(x0 + 35, y0 + 103, anchor="nw", text="", font=("Microsoft YaHei UI", 8), fill=colors["warning"])
        button_tag = f"account-button:{account.account_id}"
        items["button"] = self.canvas.create_rectangle(button_x, y0 + 43, button_x + CARD_BUTTON_WIDTH, y0 + 79, fill=colors["accent"], outline="", tags=(button_tag,))
        items["button_text"] = self.canvas.create_text(button_x + CARD_BUTTON_WIDTH // 2, y0 + 61, anchor="center", text="切换到此账号", font=("Microsoft YaHei UI", 9), fill="white", tags=(button_tag,))
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
        self.quota_status.configure(bg=colors["form"], fg=colors["secondary"])
        self.canvas.configure(bg=colors["form"])
        self.log_text.configure(bg=colors["input"], fg=colors["text"], insertbackground=colors["text"])
        self.log_title.configure(bg=colors["form"], fg=colors["secondary"])
        self.scrollbar.configure(bg=colors["panel"], troughcolor=colors["form"], activebackground=colors["border"])
        self.refresh_button.configure(bg=colors["accent"], fg="white", activebackground=colors["accent_hover"], activeforeground="white")
        self.add_account_button.configure(bg=colors["panel"], fg=colors["text"], activebackground=colors["border"], activeforeground=colors["text"])
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

    def fit_card_text(self, text: str, font: Any, width: int) -> str:
        """Keep a single-line card label inside its allocated column."""
        value = safe_text(text)
        measure_font = font if isinstance(font, tkfont.Font) else tkfont.Font(root=self.root, font=font)
        if measure_font.measure(value) <= width:
            return value
        ellipsis = "…"
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if measure_font.measure(value[:middle] + ellipsis) <= width:
                low = middle
            else:
                high = middle - 1
        return value[:low] + ellipsis

    def update_countdowns(self) -> None:
        """Update only the countdown labels; quota bars remain untouched."""
        for account in self.accounts:
            entry = self.cards.get(account.account_id)
            if not entry:
                continue
            item = self.cache_item(account.account_id)
            items = entry["items"]
            for prefix, bucket in (("five", item.get("fiveHour")), ("weekly", item.get("weekly"))):
                self.set_canvas_text_if_changed(items[prefix], self.format_bucket(bucket))

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
        status_text = "当前账号" if current else "可切换"
        self.set_canvas_text_if_changed(items["status"], self.fit_card_text(status_text, self.card_fonts["small"], 220))
        self.canvas.itemconfigure(items["status"], fill=colors["success"] if current else colors["secondary"])
        five_bucket = item.get("fiveHour")
        weekly_bucket = item.get("weekly")
        for prefix, bucket in (("five", five_bucket), ("weekly", weekly_bucket)):
            self.set_canvas_text_if_changed(items[prefix], self.format_bucket(bucket))
        self.draw_quota_bar(account_id, "fiveHour")
        self.draw_quota_bar(account_id, "weekly")
        updated_text = f"数据更新时间：{self.observed_text(item.get('observedAt'))}"
        self.set_canvas_text_if_changed(items["updated"], self.fit_card_text(updated_text, self.card_fonts["small"], items["updated_width"]))
        error = str(item.get("error") or "")
        if error and item.get("observedAt"):
            error = f"查询失败（已保留上次数据）：{error}"
        elif error:
            error = f"查询失败：{error}"
        self.set_canvas_text_if_changed(items["error"], self.fit_card_text(error, self.card_fonts["small"], items["error_width"]))
        button_color = colors["success"] if current else colors["accent"]
        self.canvas.itemconfigure(items["strip"], fill=button_color if current else colors["accent"])
        self.canvas.itemconfigure(items["button"], fill=button_color)
        self.canvas.itemconfigure(items["button_text"], text="当前账号" if current else "切换到此账号")

    def update_cards(self) -> None:
        for account in self.accounts:
            self.update_card(account.account_id)
        self.update_summary()

    def switch_account(self, account: Account) -> None:
        if self.closing or self.adding_current:
            return
        if self.switching or account.account_id == self.current_id:
            return
        self.switching = True
        self.add_account_button.configure(state="disabled")
        self.update_cards()
        self.write_log(f"开始切换到账号 {account.account_id}（{account.email}）。")

        def worker(_cancel: threading.Event) -> None:
            try:
                self.core.switch(account)
                self.events.put(("switch_ok", account))
            except Exception as exc:
                self.events.put(("switch_error", safe_text(exc)))

        self.lifecycle.start("agy-switch", worker)

    def add_current_account(self) -> None:
        if self.closing or self.switching or self.adding_current:
            return
        self.adding_current = True
        self.add_account_button.configure(state="disabled")
        self.write_log("正在读取当前登录账号凭据并添加到本地列表。")

        def worker(_cancel: threading.Event) -> None:
            try:
                account, added = self.core.add_current_account()
                self.events.put(("add_current_ok", (account, added)))
            except Exception as exc:
                self.events.put(("add_current_error", safe_text(exc)))

        try:
            self.lifecycle.start("agy-add-current-account", worker)
        except RuntimeError:
            self.adding_current = False
            self.add_account_button.configure(state="normal")

    def handle_add_current_success(self, account: Account, added: bool) -> None:
        """Apply the import result on Tk's main thread, preserving live objects."""
        existing = next((item for item in self.accounts if item.account_id == account.account_id), None)
        if existing is None:
            self.accounts.append(account)
            self.build_card(account)
            self.cache_item(account.account_id)
            self.redraw_cards()
        # Do not replace an existing object: a quota worker may still hold it.
        self.current_id = account.account_id
        self.adding_current = False
        self.add_account_button.configure(state="normal" if not self.switching else "disabled")
        self.update_cards()
        if added:
            self.write_log(f"已添加当前登录账号（账号 {account.account_id}）。")
            messagebox.showinfo("添加成功", f"已添加当前登录账号：{safe_text(account.email)}", parent=self.root)
        else:
            self.write_log(f"当前登录账号已在列表中（账号 {account.account_id}），保留本地已有凭据。")
            messagebox.showinfo("账号已存在", f"当前登录账号已存在：{safe_text(existing.email if existing else account.email)}\n\n已保留本地账号数据。", parent=self.root)

    def start_quota_refresh(self) -> None:
        if self.closing:
            return
        if self.quota_refreshing or not bool(self.core.config.get("queryQuota", True)):
            return
        self.quota_refreshing = True
        self.quota_successes = 0
        self.quota_failures = 0
        self.refresh_button.configure(state="disabled")
        self.set_quota_status("正在查询额度……")
        self.write_log(f"开始查询 {len(self.accounts)} 个账号的额度。")

        def worker(cancel: threading.Event) -> None:
            try:
                run_quota_batch(
                    list(self.accounts),
                    self.core.quota,
                    cancel,
                    lambda result: self.events.put(("quota_result", result)),
                    self.core.config.get("quotaMaxWorkers", 2),
                )
            finally:
                self.events.put(("quota_done", None))

        self.lifecycle.start("agy-quota", worker)

    def poll_events(self) -> None:
        if self.closing:
            return
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
                    self.add_account_button.configure(state="normal")
                    self.update_cards()
                    self.write_log(f"账号 {account.account_id} 已切换成功。Antigravity 未自动重新启动。")
                    messagebox.showinfo("切换成功", f"已切换到：{account.email}\n\n请按你原来的方式重新启动 Antigravity。", parent=self.root)
                elif kind == "switch_error":
                    self.switching = False
                    self.add_account_button.configure(state="normal")
                    self.update_cards()
                    self.write_log(f"切换失败：{value}", "ERROR")
                    messagebox.showerror("切换失败", f"{value}\n\n凭据写入只会在确认 Antigravity 退出后执行。", parent=self.root)
                elif kind == "add_current_ok":
                    account, added = value
                    self.handle_add_current_success(account, added)
                elif kind == "add_current_error":
                    self.adding_current = False
                    self.add_account_button.configure(state="normal")
                    self.write_log(f"添加当前账号失败：{value}", "ERROR")
                    messagebox.showerror("添加失败", str(value), parent=self.root)
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
            atomic_save_json(path, self.cache)
        except Exception as exc:
            self.write_log(f"额度缓存保存失败：{safe_text(exc)}", "WARN")

    def tick(self) -> None:
        if self.closing:
            return
        self.update_countdowns()
        new_dark = self.system_dark()
        if new_dark != self.dark:
            self.dark = new_dark
            self.apply_theme()
            self.update_cards()
        self.root.after(1000, self.tick)

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.refresh_button.configure(state="disabled")
        self.add_account_button.configure(state="disabled")
        self.set_quota_status("正在安全完成后台操作……")
        self.lifecycle.request_close(self._finish_close)
        self.root.after(50, self._poll_close)

    def _poll_close(self) -> None:
        if not self.lifecycle.ready_to_close():
            self.root.after(50, self._poll_close)

    def _finish_close(self) -> None:
        self.uninstall_native_drag_guard()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def load_cache(config: dict[str, Any]) -> dict[str, Any]:
    path = resolve_path(str(config["quotaCacheFile"]))
    if not path.exists():
        return {}
    try:
        return validate_cache(read_json(path))
    except Exception:
        return {}

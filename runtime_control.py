"""Small concurrency and lifetime primitives for the account switcher."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from contextlib import contextmanager
from typing import Callable, Iterator


class AccountOperationLocks:
    """Serialize refresh, save, and switch work for one account at a time."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _for_account(self, account_id: str) -> threading.RLock:
        key = str(account_id)
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    @contextmanager
    def hold(self, account_id: str) -> Iterator[None]:
        lock = self._for_account(account_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


class WorkerLifecycle:
    """Run non-daemon workers and signal cancellation without blocking close."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._close_requested = False
        self._close_callback: Callable[[], None] | None = None
        self._close_called = False

    def start(self, name: str, target: Callable[[threading.Event], None]) -> threading.Thread:
        def run() -> None:
            try:
                target(self.cancel_event)
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        with self._lock:
            if self._close_requested:
                raise RuntimeError("worker lifecycle is closing")
            worker = threading.Thread(target=run, name=name, daemon=False)
            self._workers.add(worker)
            worker.start()
            return worker

    def request_close(self, release_callback: Callable[[], None]) -> None:
        """Cancel batch work; caller polls ready_to_close on the GUI thread."""
        with self._lock:
            if self._close_requested:
                return
            self._close_requested = True
            self._close_callback = release_callback
            self.cancel_event.set()

    def ready_to_close(self) -> bool:
        """Run the native-window/single-instance release callback once workers end."""
        callback = None
        with self._lock:
            if not self._close_requested or self._workers:
                return False
            if not self._close_called:
                self._close_called = True
                callback = self._close_callback
        if callback is not None:
            callback()
        return True


class SingleInstance:
    """Own a Windows named mutex handle for this process lifetime."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = r"Local\AntigravityAccountSwitcher", *, win32=None) -> None:
        self.name = name
        self._win32 = win32
        self.handle = None
        self.is_primary = False

    def acquire(self) -> bool:
        if self.handle is not None:
            return self.is_primary
        if os.name != "nt" and self._win32 is None:
            raise OSError("Windows named mutexes are only available on Windows")
        api = self._get_api()
        api.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        api.CreateMutexW.restype = wintypes.HANDLE
        handle = api.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError()
        self.handle = handle
        last_error = api.GetLastError() if self._win32 is not None else ctypes.get_last_error()
        self.is_primary = last_error != self.ERROR_ALREADY_EXISTS
        if not self.is_primary:
            self.release()
        return self.is_primary

    def release(self) -> None:
        handle, self.handle = self.handle, None
        self.is_primary = False
        if handle is not None:
            api = self._get_api()
            api.CloseHandle.argtypes = (wintypes.HANDLE,)
            api.CloseHandle.restype = wintypes.BOOL
            api.CloseHandle(handle)

    def _get_api(self):
        if self._win32 is not None:
            return self._win32
        if not hasattr(self, "_kernel32"):
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return self._kernel32

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

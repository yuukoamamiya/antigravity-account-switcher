from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from runtime_control import AccountOperationLocks, SingleInstance, WorkerLifecycle


class AccountOperationLocksTests(unittest.TestCase):
    def test_same_account_serializes_but_different_accounts_can_overlap(self) -> None:
        locks = AccountOperationLocks()
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with locks.hold("a"):
                entered.set()
                release.wait(2)

        def same_account() -> None:
            with locks.hold("a"):
                second_entered.set()

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=same_account)
        t1.start()
        self.assertTrue(entered.wait(1))
        t2.start()
        self.assertFalse(second_entered.wait(0.05))
        with locks.hold("b"):
            self.assertFalse(second_entered.is_set())
        release.set()
        t1.join(1)
        t2.join(1)
        self.assertTrue(second_entered.is_set())


class WorkerLifecycleTests(unittest.TestCase):
    def test_close_cancels_batches_and_waits_for_in_flight_worker(self) -> None:
        lifecycle = WorkerLifecycle()
        started = threading.Event()
        finish_switch = threading.Event()
        released = Mock()

        def in_flight_switch(cancel: threading.Event) -> None:
            started.set()
            finish_switch.wait(2)  # A switch completes safely even after cancellation.

        worker = lifecycle.start("simulated-switch", in_flight_switch)
        self.assertTrue(started.wait(1))
        lifecycle.request_close(released)
        self.assertTrue(lifecycle.cancel_event.is_set())
        self.assertFalse(lifecycle.ready_to_close())
        released.assert_not_called()

        finish_switch.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(lifecycle.ready_to_close())
        self.assertTrue(lifecycle.ready_to_close())
        released.assert_called_once_with()

    def test_close_without_workers_releases_immediately_and_rejects_new_workers(self) -> None:
        lifecycle = WorkerLifecycle()
        released = Mock()
        lifecycle.request_close(released)
        self.assertTrue(lifecycle.ready_to_close())
        released.assert_called_once_with()
        with self.assertRaises(RuntimeError):
            lifecycle.start("late", lambda _cancel: None)


class SingleInstanceTests(unittest.TestCase):
    def fake_api(self, last_error: int = 0):
        return SimpleNamespace(
            CreateMutexW=Mock(return_value=123),
            GetLastError=Mock(return_value=last_error),
            CloseHandle=Mock(return_value=True),
        )

    def test_primary_instance_retains_then_closes_handle(self) -> None:
        large_handle = 0x123456789ABC
        api = self.fake_api()
        api.CreateMutexW.return_value = large_handle
        instance = SingleInstance(win32=api)
        self.assertTrue(instance.acquire())
        self.assertEqual(instance.handle, large_handle)
        self.assertEqual(api.CreateMutexW.argtypes[2].__name__, "c_wchar_p")
        api.CloseHandle.assert_not_called()
        instance.release()
        self.assertEqual(api.CloseHandle.argtypes[0].__name__, "c_void_p")
        self.assertIs(api.CloseHandle.restype, __import__("ctypes").wintypes.BOOL)
        api.CloseHandle.assert_called_once_with(large_handle)
        self.assertIsNone(instance.handle)
        instance.release()
        api.CloseHandle.assert_called_once_with(large_handle)

    def test_duplicate_instance_closes_its_handle_immediately(self) -> None:
        api = self.fake_api(SingleInstance.ERROR_ALREADY_EXISTS)
        instance = SingleInstance(win32=api)
        self.assertFalse(instance.acquire())
        self.assertIsNone(instance.handle)
        api.CloseHandle.assert_called_once_with(123)

    def test_acquire_is_idempotent_while_primary_handle_is_held(self) -> None:
        api = self.fake_api()
        instance = SingleInstance(win32=api)
        self.assertTrue(instance.acquire())
        self.assertTrue(instance.acquire())
        api.CreateMutexW.assert_called_once()


if __name__ == "__main__":
    unittest.main()

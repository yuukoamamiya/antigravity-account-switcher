"""UI-level tests for account rename, reorder, and delete actions."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from switcher_ui import SwitcherApp


def make_account(account_id: str, email: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(account_id=account_id, email=email or f"{account_id}@example.invalid")


def make_app(accounts=(), backup_dir="synthetic-backup", **overrides):
    app = SwitcherApp.__new__(SwitcherApp)
    app.core = SimpleNamespace(
        config={},
        backup_dir=Path(backup_dir),
        rename_account=Mock(),
        set_account_order=Mock(return_value=len(accounts)),
        delete_account=Mock(),
    )
    app.root = Mock()
    app.closing = False
    app.switching = False
    app.adding_current = False
    app.configuring_proxy = False
    app.quota_refreshing = False
    app.account_refreshing = set()
    app.accounts = list(accounts)
    app.cards = {account.account_id: {"account": account, "items": {}} for account in app.accounts}
    app.cache = {account.account_id: {"fiveHour": None} for account in app.accounts}
    app.redraw_cards = Mock()
    app.update_summary = Mock()
    app.save_cache = Mock()
    app.write_log = Mock()
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


class AccountActionBusyTests(unittest.TestCase):
    def test_busy_states_block_all_account_actions(self):
        busy_states = (
            {"closing": True},
            {"switching": True},
            {"adding_current": True},
            {"configuring_proxy": True},
            {"quota_refreshing": True},
            {"account_refreshing": {"a"}},
        )
        for state in busy_states:
            with self.subTest(state=state):
                account = make_account("a")
                app = make_app([account, make_account("b")], **state)
                with patch("switcher_ui.simpledialog.askstring") as ask, \
                     patch("switcher_ui.messagebox.askyesno") as confirm:
                    app.rename_account(account)
                    app.move_account(account, 1)
                    app.delete_account(account)
                ask.assert_not_called()
                confirm.assert_not_called()
                app.core.rename_account.assert_not_called()
                app.core.set_account_order.assert_not_called()
                app.core.delete_account.assert_not_called()

    def test_account_busy_only_blocks_the_refreshing_account(self):
        refreshing = make_account("a")
        other = make_account("b")
        app = make_app([refreshing, other], account_refreshing={"a"})
        self.assertTrue(app._account_busy(refreshing))
        self.assertFalse(app._account_busy(other))


class RenameAccountActionTests(unittest.TestCase):
    def test_successful_rename_updates_display_name_and_redraws(self):
        account = make_account("a", "old@example.invalid")
        app = make_app([account])
        with patch("switcher_ui.simpledialog.askstring", return_value="  New Name  ") as ask:
            app.rename_account(account)
        self.assertEqual(ask.call_args.args[:2], ("重命名账号", "输入新的显示名称（仅保存在本地元数据）"))
        self.assertEqual(ask.call_args.kwargs["initialvalue"], "old@example.invalid")
        app.core.rename_account.assert_called_once_with(account, "  New Name  ")
        app.redraw_cards.assert_called_once()
        app.write_log.assert_called_once()

    def test_cancel_and_blank_input_leave_account_untouched(self):
        account = make_account("a")
        for value in (None, "", "   "):
            with self.subTest(value=value):
                app = make_app([account])
                with patch("switcher_ui.simpledialog.askstring", return_value=value):
                    app.rename_account(account)
                app.core.rename_account.assert_not_called()
                app.redraw_cards.assert_not_called()

    def test_validation_error_is_reported_without_redraw(self):
        account = make_account("a")
        app = make_app([account])
        app.core.rename_account.side_effect = ValueError("账号名称不能包含控制字符。")
        with patch("switcher_ui.simpledialog.askstring", return_value="bad\nname"), \
             patch("switcher_ui.messagebox.showerror") as error:
            app.rename_account(account)
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0], "重命名失败")
        self.assertEqual(error.call_args.args[1], "账号名称不能包含控制字符。")
        app.redraw_cards.assert_not_called()


class MoveAccountActionTests(unittest.TestCase):
    def test_move_up_swaps_and_persists_new_order(self):
        first = make_account("a")
        second = make_account("b")
        app = make_app([first, second])

        app.move_account(second, -1)

        app.core.set_account_order.assert_called_once_with(["b", "a"])
        self.assertEqual([account.account_id for account in app.accounts], ["b", "a"])
        app.redraw_cards.assert_called_once()

    def test_move_down_swaps_and_persists_new_order(self):
        first = make_account("a")
        second = make_account("b")
        app = make_app([first, second])

        app.move_account(first, 1)

        app.core.set_account_order.assert_called_once_with(["b", "a"])
        self.assertEqual([account.account_id for account in app.accounts], ["b", "a"])

    def test_boundary_moves_do_nothing(self):
        first = make_account("a")
        last = make_account("b")
        for account, offset in ((first, -1), (last, 1)):
            with self.subTest(account=account.account_id, offset=offset):
                app = make_app([first, last])
                app.move_account(account, offset)
                app.core.set_account_order.assert_not_called()
                self.assertEqual([item.account_id for item in app.accounts], ["a", "b"])

    def test_unknown_order_failure_is_reported_and_order_is_unchanged(self):
        first = make_account("a")
        second = make_account("b")
        app = make_app([first, second])
        app.core.set_account_order.side_effect = ValueError("未知账号：b")
        with patch("switcher_ui.messagebox.showerror") as error:
            app.move_account(first, 1)
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0], "排序失败")
        self.assertEqual([account.account_id for account in app.accounts], ["a", "b"])
        app.redraw_cards.assert_not_called()


class DeleteAccountActionTests(unittest.TestCase):
    def test_cancel_keeps_account_and_skips_core(self):
        account = make_account("a")
        app = make_app([account])
        with patch("switcher_ui.messagebox.askyesno", return_value=False):
            app.delete_account(account)
        app.core.delete_account.assert_not_called()
        self.assertEqual(len(app.accounts), 1)
        app.save_cache.assert_not_called()

    def test_failure_is_reported_and_keeps_account(self):
        account = make_account("a")
        app = make_app([account])
        app.core.delete_account.side_effect = RuntimeError("不能删除当前正在使用的账号，请先切换到其他账号。")
        with patch("switcher_ui.messagebox.askyesno", return_value=True), \
             patch("switcher_ui.messagebox.showerror") as error:
            app.delete_account(account)
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0], "删除失败")
        self.assertEqual([item.account_id for item in app.accounts], ["a"])
        self.assertIn("a", app.cache)
        app.save_cache.assert_not_called()

    def test_success_removes_account_from_all_local_state_and_logs_backup_path(self):
        account = make_account("a")
        other = make_account("b")
        app = make_app([account, other])
        backup_dir = app.core.backup_dir / "credentials" / "20260101T000000Z-account-a"
        app.core.delete_account.return_value = backup_dir

        with patch("switcher_ui.messagebox.askyesno", return_value=True) as confirm:
            app.delete_account(account)

        self.assertIn("a@example.invalid", confirm.call_args.args[1])
        app.core.delete_account.assert_called_once_with(account)
        self.assertEqual([item.account_id for item in app.accounts], ["b"])
        self.assertNotIn("a", app.cards)
        self.assertNotIn("a", app.cache)
        self.assertNotIn("a", app.account_refreshing)
        app.save_cache.assert_called_once()
        app.redraw_cards.assert_called_once()
        app.update_summary.assert_called_once()
        relative_label = str(Path("credentials") / "20260101T000000Z-account-a")
        self.assertIn(relative_label, app.write_log.call_args.args[0])
        self.assertNotIn(str(app.core.backup_dir), app.write_log.call_args.args[0])

    def test_log_falls_back_to_absolute_path_when_not_relative(self):
        account = make_account("a")
        app = make_app([account])
        outside = Path("elsewhere") / "account-a"
        app.core.delete_account.return_value = outside

        with patch("switcher_ui.messagebox.askyesno", return_value=True):
            app.delete_account(account)

        self.assertIn(str(outside), app.write_log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

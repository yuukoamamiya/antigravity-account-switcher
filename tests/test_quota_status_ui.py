import queue
import unittest
from unittest.mock import Mock

from switcher_ui import SwitcherApp


class FormatQuotaErrorTests(unittest.TestCase):
    def test_every_status_has_a_label_without_data(self):
        cases = {
            "unauthorized": "凭据失效",
            "forbidden": "被拒绝",
            "rate_limited": "限流中",
            "deadline": "查询超时",
            "network": "网络失败",
            "http": "接口异常",
            "unknown": "查询失败",
            None: "查询失败",
        }
        for status, label in cases.items():
            with self.subTest(status=status):
                self.assertEqual(SwitcherApp.format_quota_error(status, "", False), label)

    def test_keeps_detail_and_marks_stale_data(self):
        self.assertEqual(
            SwitcherApp.format_quota_error("network", "连接失败", True),
            "网络失败（已保留上次数据）：连接失败",
        )
        self.assertEqual(
            SwitcherApp.format_quota_error("rate_limited", "请求过于频繁", False),
            "限流中：请求过于频繁",
        )


class QuotaCacheStatusTests(unittest.TestCase):
    def test_cache_item_defaults_status_to_none(self):
        app = SwitcherApp.__new__(SwitcherApp)
        app.cache = {}
        self.assertIsNone(app.cache_item("acct")["status"])

    def test_failure_result_stores_status_on_cache_item(self):
        app = SwitcherApp.__new__(SwitcherApp)
        app.cache = {}
        app.events = queue.Queue()
        app.closing = False
        app.quota_successes = 0
        app.quota_failures = 0
        app.write_log = Mock()
        app.update_card = Mock()
        app.root = Mock()
        app.events.put(("quota_result", {
            "id": "acct",
            "success": False,
            "fiveHour": None,
            "weekly": None,
            "status": "rate_limited",
            "error": "稍后再试",
            "attemptedAt": "2026-01-01T00:00:00+00:00",
        }))

        app.poll_events()

        self.assertEqual(app.cache["acct"]["status"], "rate_limited")
        self.assertEqual(app.cache["acct"]["error"], "稍后再试")
        self.assertEqual(app.cache["acct"]["lastAttemptAt"], "2026-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cache_store


class ValidateCacheTests(unittest.TestCase):
    def test_keeps_valid_data_and_legacy_error_state(self):
        value = {
            "account-a": {
                "fiveHour": {
                    "remainingFraction": 0.4,
                    "resetTime": "2026-10-23T12:00:00Z",
                    "displayName": "Five hour",
                    "description": "",
                    "window": "5h",
                    "groupName": "Models",
                },
                "weekly": None,
                "observedAt": "2026-10-23T11:00:00+00:00",
                "lastAttemptAt": "2026-10-23T11:30:00Z",
                "error": "temporary failure",
                "ignored": "old extension field",
            }
        }

        result = cache_store.validate_cache(value)

        self.assertEqual(result["account-a"]["fiveHour"]["remainingFraction"], 0.4)
        self.assertIsNone(result["account-a"]["weekly"])
        self.assertEqual(result["account-a"]["observedAt"], value["account-a"]["observedAt"])
        self.assertEqual(result["account-a"]["error"], "temporary failure")
        self.assertNotIn("ignored", result["account-a"])

    def test_drops_bad_accounts_and_individual_buckets(self):
        result = cache_store.validate_cache({
            "bad-record": None,
            12: {"fiveHour": None},
            "bad-fraction": {"fiveHour": {"remainingFraction": "0.5"}},
            "nan": {"weekly": {"remainingFraction": math.nan}},
            "infinity": {"weekly": {"remainingFraction": math.inf}},
            "out-of-range": {"weekly": {"remainingFraction": 1.01}},
            "huge-integer": {"weekly": {"remainingFraction": 10**400}},
            "bad-reset": {"weekly": {"remainingFraction": 0.2, "resetTime": "tomorrow"}},
            "bad-time": {"observedAt": "not a timestamp", "lastAttemptAt": 3},
        })

        self.assertNotIn("bad-record", result)
        self.assertNotIn(12, result)
        for account_id in ("bad-fraction", "nan", "infinity", "out-of-range", "huge-integer", "bad-reset"):
            self.assertIsNone(result[account_id]["fiveHour"] if account_id == "bad-fraction" else result[account_id]["weekly"])
        self.assertIsNone(result["bad-time"]["observedAt"])
        self.assertIsNone(result["bad-time"]["lastAttemptAt"])

    def test_boolean_fraction_is_not_a_number(self):
        result = cache_store.validate_cache({"acct": {"fiveHour": {"remainingFraction": True}}})
        self.assertIsNone(result["acct"]["fiveHour"])


class AtomicSaveJsonTests(unittest.TestCase):
    def test_writes_json_and_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("old", encoding="utf-8")

            cache_store.atomic_save_json(path, {"ok": "额度"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": "额度"})
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("original contents", encoding="utf-8")
            with patch("cache_store.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    cache_store.atomic_save_json(path, {"new": True})

            self.assertEqual(path.read_text(encoding="utf-8"), "original contents")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_rejects_non_finite_json_before_replacing_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("original", encoding="utf-8")

            with self.assertRaises(ValueError):
                cache_store.atomic_save_json(path, {"bad": math.nan})

            self.assertEqual(path.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()

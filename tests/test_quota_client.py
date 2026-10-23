import io
import json
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

from unittest.mock import patch

from quota_client import PROJECT_ENDPOINTS, QUOTA_ENDPOINTS, QuotaClient, _ExplicitProxyHandler, run_quota_batch


class Response:
    def __init__(self, value):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class FakeOpener:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout, request))
        return self.callback(request, timeout)


def account():
    return SimpleNamespace(account_id="acct", payload={"token": {
        "access_token": "simulated-access-token",
        "refresh_token": "simulated-refresh-token",
        "expiry": "2099-01-01T00:00:00+00:00",
    }})


def quota_response():
    return {"groups": [{"displayName": "limits", "buckets": [
        {"remainingFraction": 0.5, "window": "5h"},
        {"remainingFraction": 0.8, "window": "weekly"},
    ]}]}


class QuotaClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.wall = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def make_client(self, callback, *, cache=None, budget=10, config=None):
        opener = FakeOpener(callback)
        client = QuotaClient(
            {"quotaDeadlineSeconds": budget, "requestTimeoutSeconds": 20, **(config or {})},
            "test-agent", lambda _account: None, cache,
            opener_factory=lambda: opener,
            monotonic=lambda: self.now,
            wall_clock=lambda: self.wall,
        )
        return client, opener

    def test_deadline_stops_fallback_and_bounds_each_timeout(self):
        def callback(request, timeout):
            self.assertLessEqual(timeout, 10)
            self.now += 4
            raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())

        client, opener = self.make_client(callback, budget=5)
        result = client.quota(account())
        self.assertFalse(result["success"])
        self.assertEqual(len(opener.calls), 2)  # second discovery gets only remaining budget
        self.assertLessEqual(opener.calls[0][1], 5)
        self.assertLessEqual(opener.calls[1][1], 1)
        self.assertIn("总耗时限制", result["error"])

    def test_cached_project_and_endpoint_are_used_first(self):
        cached_endpoint = QUOTA_ENDPOINTS[2]
        cached_project_endpoint = PROJECT_ENDPOINTS[2]
        cache = {"acct": {
            "projectId": "cached-project", "projectEndpoint": cached_project_endpoint,
            "quotaEndpoint": cached_endpoint, "expiresAt": self.wall.timestamp() + 100,
        }}

        def callback(request, _timeout):
            if request.full_url == cached_endpoint:
                body = json.loads(request.data)
                self.assertEqual(body["project"], "cached-project")
                return Response(quota_response())
            self.fail("unexpected endpoint")

        client, opener = self.make_client(callback, cache=cache)
        self.assertTrue(client.quota(account())["success"])
        self.assertEqual([call[0] for call in opener.calls], [cached_endpoint])

    def test_stale_cache_is_invalidated_and_failure_then_recovery(self):
        cache = {"acct": {"projectId": "old", "quotaEndpoint": QUOTA_ENDPOINTS[0], "expiresAt": self.wall.timestamp() - 1}}
        count = 0

        def callback(request, _timeout):
            nonlocal count
            count += 1
            if request.full_url in PROJECT_ENDPOINTS:
                return Response({"cloudaicompanionProject": "new"})
            if request.full_url == QUOTA_ENDPOINTS[0] and count == 2:
                raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())
            if request.full_url == QUOTA_ENDPOINTS[1]:
                return Response(quota_response())
            return Response(quota_response())

        client, _ = self.make_client(callback, cache=cache)
        first = client.quota(account())
        self.assertTrue(first["success"])
        self.assertEqual(cache["acct"]["projectId"], "new")
        self.assertEqual(cache["acct"]["quotaEndpoint"], QUOTA_ENDPOINTS[1])

    def test_auth_and_rate_limit_fail_without_endpoint_walk(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                def callback(request, _timeout):
                    raise HTTPError(request.full_url, status, "failure", {}, io.BytesIO())
                client, opener = self.make_client(callback)
                result = client.quota(account())
                self.assertFalse(result["success"])
                self.assertEqual(len(opener.calls), 1)
                self.assertIn(str(status), result["error"])

    def test_proxy_bypass_is_explicit_and_suffix_safe(self):
        handler = _ExplicitProxyHandler({"https": "http://proxy.invalid:8080"}, ("localhost", "example.com"))
        bypass = Request("https://api.example.com/path")
        with patch.dict(os.environ, {"NO_PROXY": "*", "HTTPS_PROXY": "http://environment.invalid:9000"}), patch("urllib.request.proxy_bypass", side_effect=AssertionError("environment bypass consulted")):
            self.assertIsNone(handler.proxy_open(bypass, "http://proxy.invalid:8080", "https"))
            self.assertEqual(bypass.host, "api.example.com")
        unrelated = Request("https://notexample.com/path")
        with patch.dict(os.environ, {"NO_PROXY": "*"}), patch("urllib.request.proxy_bypass", side_effect=AssertionError("environment bypass consulted")):
            self.assertIsNone(handler.proxy_open(unrelated, "http://proxy.invalid:8080", "https"))
            self.assertEqual(unrelated.host, "proxy.invalid:8080")
        client, _ = self.make_client(lambda *_: Response({}), config={"proxy": "http://proxy.invalid:8080", "noProxy": "localhost,example.com"})
        self.assertEqual(client._no_proxy_hosts(), ("localhost", "example.com"))
        star = _ExplicitProxyHandler({"https": "http://proxy.invalid:8080"}, ("*",))
        all_hosts = Request("https://anywhere.invalid/path")
        star.proxy_open(all_hosts, "http://proxy.invalid:8080", "https")
        self.assertEqual(all_hosts.host, "anywhere.invalid")

    def test_bad_bucket_numbers_are_ignored(self):
        for value in (True, float("nan"), float("inf"), 10**100):
            self.assertIsNone(QuotaClient._bucket({"remainingFraction": value}, {}))

    def test_failures_do_not_expose_tokens(self):
        def callback(request, _timeout):
            raise HTTPError(request.full_url, 500, "Bearer simulated-access-token", {}, io.BytesIO())
        client, _ = self.make_client(callback)
        result = client.quota(account())
        self.assertNotIn("simulated-access-token", result["error"])

    def test_batch_respects_worker_cap_and_emits_future_failures(self):
        accounts = [SimpleNamespace(account_id=str(index)) for index in range(12)]
        active = 0
        peak = 0
        guard = threading.Lock()
        emitted = []

        def query(item):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.01)
                if item.account_id == "3":
                    raise RuntimeError("simulated future failure")
                return {"id": item.account_id, "success": True}
            finally:
                with guard:
                    active -= 1

        run_quota_batch(accounts, query, threading.Event(), emitted.append, max_workers=99)
        self.assertLessEqual(peak, 4)
        self.assertEqual(len(emitted), len(accounts))
        failure = next(item for item in emitted if item["id"] == "3")
        self.assertFalse(failure["success"])
        self.assertIn("simulated future failure", failure["error"])

    def test_batch_cancellation_stops_submission_and_finishes(self):
        accounts = [SimpleNamespace(account_id=str(index)) for index in range(20)]
        cancel = threading.Event()
        started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        emitted = []
        calls = []

        def query(item):
            calls.append(item.account_id)
            started.set()
            release.wait(1)
            return {"id": item.account_id, "success": True}

        def worker():
            try:
                run_quota_batch(accounts, query, cancel, emitted.append, max_workers=2)
            finally:
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(1))
        cancel.set()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(done.is_set())
        self.assertLessEqual(len(calls), 2)
        self.assertLessEqual(len(emitted), 2)


if __name__ == "__main__":
    unittest.main()

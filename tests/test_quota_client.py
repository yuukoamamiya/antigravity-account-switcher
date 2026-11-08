import io
import json
import os
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

from unittest.mock import patch

from quota_client import (
    MODELS_ENDPOINTS,
    PROJECT_ENDPOINTS,
    QUOTA_ENDPOINTS,
    QuotaClient,
    _ExplicitProxyHandler,
    run_quota_batch,
)


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


def account(expiry="2099-01-01T00:00:00+00:00"):
    return SimpleNamespace(account_id="acct", payload={"token": {
        "access_token": "simulated-access-token",
        "refresh_token": "simulated-refresh-token",
        "expiry": expiry,
    }})


def quota_response():
    return {"groups": [{"displayName": "Gemini Models", "buckets": [
        {"remainingFraction": 0.5, "window": "5h", "bucketId": "gemini-5h"},
        {"remainingFraction": 0.8, "window": "weekly", "bucketId": "gemini-weekly"},
    ]}]}


def models_response():
    return {"models": {
        "gemini-3-flash": {"quotaInfo": {"remainingFraction": 0.7, "resetTime": "2026-01-01T05:00:00Z"}},
        "claude-sonnet-4-5": {"quotaInfo": {"remainingFraction": 0.2}},
    }}


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

    def test_endpoints_prefer_daily_then_production_then_sandbox(self):
        for endpoints in (PROJECT_ENDPOINTS, MODELS_ENDPOINTS, QUOTA_ENDPOINTS):
            self.assertEqual(len(endpoints), 3)
            self.assertTrue(endpoints[0].startswith("https://daily-cloudcode-pa.googleapis.com/"))
            self.assertTrue(endpoints[1].startswith("https://cloudcode-pa.googleapis.com/"))
            self.assertTrue(endpoints[2].startswith("https://daily-cloudcode-pa.sandbox.googleapis.com/"))

    def test_models_then_summary_are_queried_after_project(self):
        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                self.assertEqual(json.loads(request.data), {"project": "project-1"})
                return Response(models_response())
            if request.full_url == QUOTA_ENDPOINTS[0]:
                self.assertEqual(json.loads(request.data), {"project": "project-1"})
                return Response(quota_response())
            self.fail(f"unexpected endpoint: {request.full_url}")

        client, opener = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.5)
        self.assertEqual(result["weekly"]["remainingFraction"], 0.8)
        self.assertEqual(
            [call[0] for call in opener.calls],
            [PROJECT_ENDPOINTS[0], MODELS_ENDPOINTS[0], QUOTA_ENDPOINTS[0]],
        )

    def test_summary_retries_without_project_after_403_on_same_host(self):
        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response(models_response())
            if request.full_url == QUOTA_ENDPOINTS[0]:
                if json.loads(request.data).get("project"):
                    raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())
                return Response(quota_response())
            self.fail(f"unexpected endpoint: {request.full_url}")

        client, opener = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        summary_calls = [call for call in opener.calls if call[0] == QUOTA_ENDPOINTS[0]]
        self.assertEqual(len(summary_calls), 2)
        self.assertEqual(json.loads(summary_calls[0][2].data), {"project": "project-1"})
        self.assertEqual(json.loads(summary_calls[1][2].data), {})

    def test_models_fall_back_to_next_host(self):
        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())
            if request.full_url == MODELS_ENDPOINTS[1]:
                return Response(models_response())
            raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())

        client, opener = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.7)
        self.assertIsNone(result["weekly"])
        self.assertIn(MODELS_ENDPOINTS[1], [call[0] for call in opener.calls])

    def test_models_are_used_when_summary_is_empty(self):
        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response(models_response())
            if request.full_url == QUOTA_ENDPOINTS[0]:
                return Response({"groups": []})
            raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())

        client, _ = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.7)
        self.assertIsNone(result["weekly"])

    def test_summary_prefers_general_group_over_argon_and_third_party(self):
        groups = {"groups": [
            {"displayName": "Gemini 4 Argon", "description": "Premium", "buckets": [
                {"remainingFraction": 0.1, "window": "weekly", "bucketId": "gemini-argon-weekly"},
            ]},
            {"displayName": "Claude and GPT models", "description": "Shared", "buckets": [
                {"remainingFraction": 0.2, "window": "5h", "bucketId": "3p-5h"},
                {"remainingFraction": 0.3, "window": "weekly", "bucketId": "3p-weekly"},
            ]},
            {"displayName": "Gemini Models", "description": "Standard", "buckets": [
                {"remainingFraction": 0.6, "window": "5h", "bucketId": "gemini-5h"},
                {"remainingFraction": 0.9, "window": "weekly", "bucketId": "gemini-weekly"},
            ]},
        ]}

        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response({"models": {}})
            if request.full_url == QUOTA_ENDPOINTS[0]:
                return Response(groups)
            raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())

        client, _ = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["groupName"], "Gemini Models")
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.6)
        self.assertEqual(result["weekly"]["groupName"], "Gemini Models")
        self.assertEqual(result["weekly"]["remainingFraction"], 0.9)

    def test_bucket_id_identifies_windows_when_window_is_empty(self):
        groups = {"groups": [{"displayName": "Gemini Models", "buckets": [
            {"remainingFraction": 0.4, "bucketId": "gemini-5h"},
            {"remainingFraction": 0.9, "bucketId": "gemini-weekly"},
        ]}]}

        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url in MODELS_ENDPOINTS:
                return Response({"models": {}})
            return Response(groups)

        client, _ = self.make_client(callback)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.4)
        self.assertEqual(result["weekly"]["remainingFraction"], 0.9)

    def test_cached_project_is_used_first(self):
        cache = {"acct": {
            "projectId": "cached-project", "projectEndpoint": PROJECT_ENDPOINTS[0],
            "expiresAt": self.wall.timestamp() + 100,
        }}

        def callback(request, _timeout):
            if request.full_url == MODELS_ENDPOINTS[0]:
                self.assertEqual(json.loads(request.data), {"project": "cached-project"})
                return Response(models_response())
            if request.full_url == QUOTA_ENDPOINTS[0]:
                return Response(quota_response())
            self.fail(f"unexpected endpoint: {request.full_url}")

        client, opener = self.make_client(callback, cache=cache)
        self.assertTrue(client.quota(account())["success"])
        self.assertEqual(
            [call[0] for call in opener.calls],
            [MODELS_ENDPOINTS[0], QUOTA_ENDPOINTS[0]],
        )

    def test_expired_project_cache_triggers_discovery(self):
        cache = {"acct": {"projectId": "old", "projectEndpoint": PROJECT_ENDPOINTS[0],
                          "expiresAt": self.wall.timestamp() - 1}}

        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "new"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response(models_response())
            return Response(quota_response())

        client, opener = self.make_client(callback, cache=cache)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(cache["acct"]["projectId"], "new")
        self.assertEqual(opener.calls[0][0], PROJECT_ENDPOINTS[0])

    def test_oauth_refresh_uses_configured_client_secret(self):
        def callback(request, _timeout):
            if request.full_url == "https://oauth2.googleapis.com/token":
                form = urllib.parse.parse_qs(request.data.decode())
                self.assertEqual(form["client_secret"], ["simulated-secret"])
                self.assertEqual(form["grant_type"], ["refresh_token"])
                return Response({"access_token": "fresh-access", "expires_in": 3600})
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response(models_response())
            return Response({"groups": []})

        client, _ = self.make_client(callback, config={"oauthClientSecret": "simulated-secret"})
        result = client.quota(account(expiry="2000-01-01T00:00:00+00:00"))
        self.assertTrue(result["success"])

    def test_oauth_refresh_reads_secret_from_installed_language_server(self):
        import tempfile
        from pathlib import Path

        first_id = "111111-personalclientidabcdefghij.apps.googleusercontent.com"
        second_id = "222222-enterpriseclientidabcdefgh.apps.googleusercontent.com"
        first_client_value = "GOCSPX-" + "A" * 28
        second_client_value = "GOCSPX-" + "B" * 28
        attempts = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "resources" / "bin" / "language_server.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(
                f" {first_id} {second_id} ".encode()
                + f" {first_client_value} {second_client_value} ".encode()
            )
            executable = root / "Antigravity.exe"

            def callback(request, _timeout):
                if request.full_url == "https://oauth2.googleapis.com/token":
                    secret = urllib.parse.parse_qs(request.data.decode())["client_secret"][0]
                    attempts.append(secret)
                    if secret == first_client_value:
                        raise HTTPError(request.full_url, 400, "invalid_client", {}, io.BytesIO())
                    return Response({"access_token": "fresh-access", "expires_in": 3600})
                if request.full_url == PROJECT_ENDPOINTS[0]:
                    return Response({"cloudaicompanionProject": "project-1"})
                if request.full_url == MODELS_ENDPOINTS[0]:
                    return Response(models_response())
                return Response({"groups": []})

            with patch.dict(os.environ, {"LOCALAPPDATA": directory, "PROGRAMFILES": directory}):
                client, _ = self.make_client(callback, config={
                    "oauthClientId": first_id,
                    "antigravityExecutable": str(executable),
                })
                result = client.quota(account(expiry="2000-01-01T00:00:00+00:00"))

        self.assertTrue(result["success"])
        self.assertEqual(attempts, [first_client_value, second_client_value])

    def test_oauth_refresh_reports_missing_client_secret(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Antigravity.exe"
            with patch.dict(os.environ, {"LOCALAPPDATA": directory, "PROGRAMFILES": directory}):
                client, _ = self.make_client(
                    lambda *_: self.fail("no request should be sent"),
                    config={"antigravityExecutable": str(executable)},
                )
                result = client.quota(account(expiry="2000-01-01T00:00:00+00:00"))

        self.assertFalse(result["success"])
        self.assertIn("OAuth", result["error"])

    def test_401_stops_all_further_requests(self):
        def callback(request, _timeout):
            raise HTTPError(request.full_url, 401, "failure", {}, io.BytesIO())

        client, opener = self.make_client(callback)
        result = client.quota(account())
        self.assertFalse(result["success"])
        self.assertEqual(len(opener.calls), 1)
        self.assertIn("401", result["error"])

    def test_429_and_5xx_walk_every_host(self):
        def callback(request, _timeout):
            raise HTTPError(request.full_url, 429 if request.full_url == MODELS_ENDPOINTS[0] else 500,
                            "failure", {}, io.BytesIO())

        client, opener = self.make_client(callback)
        result = client.quota(account())
        self.assertFalse(result["success"])
        self.assertEqual(
            [call[0] for call in opener.calls],
            list(PROJECT_ENDPOINTS) + list(MODELS_ENDPOINTS) + list(QUOTA_ENDPOINTS),
        )

    def test_deadline_stops_after_project_discovery_and_bounds_timeout(self):
        def callback(request, timeout):
            self.assertLessEqual(timeout, 5)
            self.now += 5
            raise HTTPError(request.full_url, 500, "failure", {}, io.BytesIO())

        client, opener = self.make_client(callback, budget=5)
        result = client.quota(account())
        self.assertFalse(result["success"])
        self.assertEqual(len(opener.calls), 1)
        self.assertLessEqual(opener.calls[0][1], 5)
        self.assertIn("总耗时限制", result["error"])

    def test_partial_success_survives_summary_deadline(self):
        def callback(request, _timeout):
            if request.full_url == PROJECT_ENDPOINTS[0]:
                return Response({"cloudaicompanionProject": "project-1"})
            if request.full_url == MODELS_ENDPOINTS[0]:
                return Response(models_response())
            self.now += 10
            return Response(quota_response())

        client, _ = self.make_client(callback, budget=5)
        result = client.quota(account())
        self.assertTrue(result["success"])
        self.assertEqual(result["fiveHour"]["remainingFraction"], 0.7)
        self.assertIsNone(result["weekly"])

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

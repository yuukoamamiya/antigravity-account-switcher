"""Bounded, cache-aware Google quota requests.

This module deliberately depends only on the standard library.  The account
switcher can use :class:`QuotaClient` as the implementation behind its public
``quota(account)`` method while keeping UI and credential management separate.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from common import parse_time as _parse_time, safe_text as _safe_text


PROJECT_ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:loadCodeAssist",
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
)
QUOTA_ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)
OAUTH_URL = "https://oauth2.googleapis.com/token"
_CACHE_TTL_SECONDS = 6 * 60 * 60


class QuotaError(RuntimeError):
    """Safe-to-display quota request failure."""


class QuotaClient:
    """Fetch quota with one monotonic deadline shared by OAuth and endpoints.

    ``cache`` is a mutable per-account mapping supplied by the caller.  Entries
    contain only endpoint/project routing hints and expire after six hours.
    The caller owns persistence of that mapping alongside its existing cache.
    """

    def __init__(
        self,
        config: dict[str, Any],
        user_agent: str,
        save_account: Callable[[Any], None],
        cache: dict[str, Any] | None = None,
        *,
        opener_factory: Callable[[], urllib.request.OpenerDirector] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.user_agent = user_agent
        self.save_account = save_account
        self.cache = cache if cache is not None else {}
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.opener_factory = opener_factory or self._opener

    def _opener(self) -> urllib.request.OpenerDirector:
        proxy = str(self.config.get("proxy") or "").strip()
        no_proxy = self._no_proxy_hosts()
        if proxy:
            # Explicit proxy entries omit bypassed hosts, so environment
            # HTTP_PROXY/NO_PROXY values cannot silently alter the policy.
            proxies = {scheme: proxy for scheme in ("http", "https")}
            handler = _ExplicitProxyHandler(proxies, no_proxy)
        else:
            # Empty ProxyHandler disables urllib's environment proxy discovery.
            handler = urllib.request.ProxyHandler({})
        return urllib.request.build_opener(handler)

    def _no_proxy_hosts(self) -> tuple[str, ...]:
        raw = str(self.config.get("noProxy") or "")
        return tuple(part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip())

    def _request(self, url: str, token: str | None, body: dict[str, Any], deadline: float, *, form: bytes | None = None) -> dict[str, Any]:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise QuotaError("额度查询已超过总耗时限制。")
        request_timeout = max(0.001, min(float(self.config.get("requestTimeoutSeconds", 20)), remaining))
        headers = {"User-Agent": self.user_agent}
        if form is None:
            headers["Content-Type"] = "application/json"
            if token:
                headers["Authorization"] = f"Bearer {token}"
            data = json.dumps(body).encode("utf-8")
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = form
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with self.opener_factory().open(request, timeout=request_timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
            if self.monotonic() >= deadline:
                raise QuotaError("额度查询已超过总耗时限制。")
            if not isinstance(value, dict):
                raise QuotaError("Google 返回的数据格式不正确。")
            return value
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _HttpQuotaError(exc.code) from None
        except urllib.error.URLError as exc:
            raise QuotaError(f"Google 接口请求失败：{_safe_text(exc.reason)}") from None
        except (TimeoutError, OSError):
            raise QuotaError("Google 接口请求超时或失败。") from None
        except json.JSONDecodeError:
            raise QuotaError("Google 接口返回的数据无法解析。") from None

    def _refresh_token(self, account: Any, deadline: float) -> str:
        token = account.payload["token"]
        expiry = _parse_time(token.get("expiry"))
        if token.get("access_token") and expiry and expiry > self.wall_clock() + timedelta(seconds=60):
            return str(token["access_token"])
        form = urllib.parse.urlencode({
            "client_id": self.config.get("oauthClientId", ""),
            "client_secret": self.config.get("oauthClientSecret") or os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", ""),
            "refresh_token": token.get("refresh_token", ""),
            "grant_type": "refresh_token",
        }).encode("utf-8")
        try:
            value = self._request(OAUTH_URL, None, {}, deadline, form=form)
        except _HttpQuotaError as exc:
            raise QuotaError(f"Google OAuth 刷新失败（HTTP {exc.code}）。") from None
        access = value.get("access_token")
        if not access:
            raise QuotaError("Google OAuth 响应中没有 access_token。")
        token["access_token"] = access
        token["expiry"] = (self.wall_clock() + timedelta(seconds=int(value.get("expires_in", 3600)))).isoformat()
        if value.get("refresh_token"):
            token["refresh_token"] = value["refresh_token"]
        self.save_account(account)
        return str(access)

    def _entry(self, account_id: str) -> dict[str, Any]:
        value = self.cache.get(account_id)
        if not isinstance(value, dict):
            return {}
        try:
            if float(value.get("expiresAt", 0)) <= self.wall_clock().timestamp():
                self.cache.pop(account_id, None)
                return {}
        except (TypeError, ValueError, OverflowError):
            self.cache.pop(account_id, None)
            return {}
        return value

    def _store(self, account_id: str, **values: Any) -> None:
        entry = self._entry(account_id)
        entry.update(values)
        entry["expiresAt"] = self.wall_clock().timestamp() + _CACHE_TTL_SECONDS
        self.cache[account_id] = entry

    @staticmethod
    def _bucket(raw: dict[str, Any], group: dict[str, Any]) -> dict[str, Any] | None:
        if raw.get("remainingFraction") is None:
            return None
        if isinstance(raw["remainingFraction"], bool):
            return None
        try:
            fraction = float(raw["remainingFraction"])
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(fraction) or abs(fraction) > 1_000_000:
            return None
        if fraction > 1:
            fraction /= 100
        return {
            "remainingFraction": max(0.0, min(1.0, fraction)),
            "resetTime": str(raw.get("resetTime") or ""),
            "displayName": str(raw.get("displayName") or ""),
            "description": str(raw.get("description") or ""),
            "window": str(raw.get("window") or ""),
            "groupName": str(group.get("displayName") or ""),
        }

    def _project(self, account_id: str, token: str, deadline: float) -> tuple[str | None, str | None]:
        entry = self._entry(account_id)
        cached = entry.get("projectId")
        cached_endpoint = entry.get("projectEndpoint")
        if isinstance(cached, str) and cached:
            return cached, cached_endpoint if cached_endpoint in PROJECT_ENDPOINTS else None
        endpoints = ([cached_endpoint] if cached_endpoint in PROJECT_ENDPOINTS else []) + [e for e in PROJECT_ENDPOINTS if e != cached_endpoint]
        body = {"metadata": {"ideType": "ANTIGRAVITY"}}
        for endpoint in endpoints:
            try:
                response = self._request(endpoint, token, body, deadline)
                project = response.get("cloudaicompanionProject")
                project_id = str(project) if project else (str(cached) if cached else None)
                if project_id:
                    self._store(account_id, projectId=project_id, projectEndpoint=endpoint)
                return project_id, endpoint
            except _HttpQuotaError as exc:
                if exc.code in (401, 403, 429):
                    if exc.code in (401, 403):
                        self._invalidate(account_id, "projectId", "projectEndpoint", "quotaEndpoint")
                    raise QuotaError(_http_message(exc.code)) from None
                if exc.code < 500 and exc.code != 404:
                    break
            except QuotaError as exc:
                if "总耗时限制" in str(exc):
                    raise
        return (str(cached) if cached else None), None

    def _invalidate(self, account_id: str, *keys: str) -> None:
        entry = self._entry(account_id)
        for key in keys:
            entry.pop(key, None)
        if entry:
            self.cache[account_id] = entry
        else:
            self.cache.pop(account_id, None)

    def quota(self, account: Any) -> dict[str, Any]:
        result = {"id": account.account_id, "success": False, "fiveHour": None, "weekly": None, "error": None,
                  "attemptedAt": self.wall_clock().isoformat()}
        account_id = account.account_id
        try:
            budget = max(1.0, float(self.config.get("quotaDeadlineSeconds", 45)))
            deadline = self.monotonic() + budget
            token = self._refresh_token(account, deadline)
            project, _project_endpoint = self._project(account_id, token, deadline)
            entry = self._entry(account_id)
            cached_endpoint = entry.get("quotaEndpoint")
            endpoints = ([cached_endpoint] if cached_endpoint in QUOTA_ENDPOINTS else []) + [e for e in QUOTA_ENDPOINTS if e != cached_endpoint]
            body = {"project": project} if project else {}
            last_error = "额度接口未返回可识别数据。"
            for endpoint in endpoints:
                if deadline <= self.monotonic():
                    raise QuotaError("额度查询已超过总耗时限制。")
                try:
                    response = self._request(endpoint, token, body, deadline)
                    groups = response.get("groups")
                    if not isinstance(groups, list) or not groups:
                        raise QuotaError("Google 返回的额度分组为空。")
                    five = weekly = None
                    for group in groups:
                        if not isinstance(group, dict):
                            continue
                        for raw in group.get("buckets") or []:
                            if not isinstance(raw, dict):
                                continue
                            bucket = self._bucket(raw, group)
                            if bucket is None:
                                continue
                            primary = " ".join(str(bucket.get(k) or "") for k in ("window", "displayName")).lower()
                            fallback = " ".join(str(bucket.get(k) or "") for k in ("description", "groupName")).lower()
                            label = primary or fallback
                            if re.search(r"5\s*-?\s*h|5\s*hour|five.?hour|hourly", label) and five is None:
                                five = bucket
                            elif re.search(r"week|weekly|7\s*-?\s*d|1\s*-?\s*w", label) and weekly is None:
                                weekly = bucket
                    if five is None and weekly is None:
                        raise QuotaError("Google 返回了额度数据，但未识别出 5 小时或一周窗口。")
                    self._store(account_id, quotaEndpoint=endpoint)
                    result.update(success=True, fiveHour=five, weekly=weekly)
                    return result
                except _HttpQuotaError as exc:
                    if exc.code in (401, 403):
                        self._invalidate(account_id, "quotaEndpoint", "projectId", "projectEndpoint")
                        raise QuotaError(_http_message(exc.code)) from None
                    if exc.code == 429:
                        raise QuotaError(_http_message(exc.code)) from None
                    if exc.code < 500 and exc.code != 404:
                        raise QuotaError(_http_message(exc.code)) from None
                    last_error = _http_message(exc.code)
                except QuotaError as exc:
                    last_error = str(exc)
                    if "总耗时限制" in last_error:
                        raise
            raise QuotaError(last_error)
        except Exception as exc:
            result["error"] = _safe_text(exc) or "额度查询失败。"
            return result


class _HttpQuotaError(QuotaError):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(_http_message(code))


def _http_message(code: int) -> str:
    if code == 401:
        return "Google 凭据已失效（HTTP 401）。"
    if code == 403:
        return "Google 拒绝了额度查询（HTTP 403）。"
    if code == 429:
        return "Google 额度接口请求过于频繁（HTTP 429）。"
    return f"Google 接口返回 HTTP {code}。"


def run_quota_batch(
    accounts: list[Any],
    query: Callable[[Any], dict[str, Any]],
    cancel: Any,
    emit: Callable[[dict[str, Any]], None],
    max_workers: Any = 2,
) -> None:
    """Run account queries with a small bounded pool and cooperative cancel."""
    try:
        worker_count = min(4, max(1, int(max_workers)))
    except (TypeError, ValueError, OverflowError):
        worker_count = 2
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="agy-quota-account"
    )
    pending: dict[concurrent.futures.Future[dict[str, Any]], Any] = {}
    accounts_iter = iter(accounts)

    def emit_future(future: concurrent.futures.Future[dict[str, Any]], account: Any) -> None:
        try:
            result = future.result()
            if not isinstance(result, dict):
                raise TypeError("额度查询返回格式不正确。")
        except Exception as exc:
            result = {
                "id": account.account_id,
                "success": False,
                "fiveHour": None,
                "weekly": None,
                "error": _safe_text(exc) or "额度查询失败。",
                "attemptedAt": datetime.now(timezone.utc).isoformat(),
            }
        emit(result)

    try:
        while len(pending) < worker_count and not cancel.is_set():
            try:
                account = next(accounts_iter)
            except StopIteration:
                break
            pending[executor.submit(query, account)] = account
        while pending:
            if cancel.is_set():
                for future in pending:
                    future.cancel()
                break
            completed, _ = concurrent.futures.wait(
                pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in completed:
                account = pending.pop(future)
                emit_future(future, account)
                if not cancel.is_set():
                    try:
                        next_account = next(accounts_iter)
                    except StopIteration:
                        continue
                    pending[executor.submit(query, next_account)] = next_account
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for future, account in list(pending.items()):
            if not future.cancelled():
                emit_future(future, account)


class _ExplicitProxyHandler(urllib.request.ProxyHandler):
    """Use configured proxy except hosts explicitly listed in noProxy."""

    def __init__(self, proxies: dict[str, str], no_proxy: tuple[str, ...]) -> None:
        super().__init__(proxies)
        self.no_proxy = no_proxy

    def proxy_open(self, req: urllib.request.Request, proxy: str, type: str) -> Any:
        host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower().rstrip(".")
        if "*" in self.no_proxy or any(host == item or host.endswith("." + item) for item in self.no_proxy):
            return None
        original_type = req.type
        proxy_type, user, password, hostport = urllib.request._parse_proxy(proxy)
        if proxy_type is None:
            proxy_type = original_type
        if user and password:
            credentials = f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}"
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            req.add_header("Proxy-authorization", "Basic " + encoded)
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if original_type == proxy_type or original_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)

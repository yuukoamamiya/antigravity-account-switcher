"""Bounded, cache-aware Google quota requests.

This module deliberately depends only on the standard library.  The account
switcher can use :class:`QuotaClient` as the implementation behind its public
``quota(account)`` method while keeping UI and credential management separate.

The quota flow mirrors the official Antigravity client:

* ``fetchAvailableModels`` is the primary source for the rolling five-hour
  window (model ``quotaInfo``); ``retrieveUserQuotaSummary`` enriches it with
  the grouped five-hour and weekly windows.
* Requests walk the daily, production, and sandbox hosts in that order, because
  the daily host is the one the official language server uses and the only host
  observed to return every quota group.
* A ``403`` sent with a project id is retried once without it on the same host;
  only ``401`` stops the walk, so one host rejecting the request cannot hide
  quota that another host still serves.
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
import mmap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from common import parse_time as _parse_time, safe_text as _safe_text


DAILY_HOST = "https://daily-cloudcode-pa.googleapis.com"
PRODUCTION_HOST = "https://cloudcode-pa.googleapis.com"
SANDBOX_HOST = "https://daily-cloudcode-pa.sandbox.googleapis.com"
_CLIENT_ID_PATTERN = re.compile(rb"\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
_CLIENT_SECRET_PATTERN = re.compile(rb"GOCSPX-[A-Za-z0-9_-]{28}")

# Daily is the host the official language server queries and the only one
# observed to return every quota group. Production and sandbox remain bounded
# fallbacks so a single unavailable deployment does not hide account quota.
PROJECT_ENDPOINTS = (
    f"{DAILY_HOST}/v1internal:loadCodeAssist",
    f"{PRODUCTION_HOST}/v1internal:loadCodeAssist",
    f"{SANDBOX_HOST}/v1internal:loadCodeAssist",
)
MODELS_ENDPOINTS = (
    f"{DAILY_HOST}/v1internal:fetchAvailableModels",
    f"{PRODUCTION_HOST}/v1internal:fetchAvailableModels",
    f"{SANDBOX_HOST}/v1internal:fetchAvailableModels",
)
QUOTA_ENDPOINTS = (
    f"{DAILY_HOST}/v1internal:retrieveUserQuotaSummary",
    f"{PRODUCTION_HOST}/v1internal:retrieveUserQuotaSummary",
    f"{SANDBOX_HOST}/v1internal:retrieveUserQuotaSummary",
)
OAUTH_URL = "https://oauth2.googleapis.com/token"
_CACHE_TTL_SECONDS = 6 * 60 * 60
_FIVE_HOUR_PATTERN = re.compile(r"5\s*-?\s*h|5\s*hour|five.?hour|hourly")
_WEEKLY_PATTERN = re.compile(r"week|weekly|7\s*-?\s*d|1\s*-?\s*w")
_THIRD_PARTY_GROUP_TOKENS = ("argon", "claude", "gpt", "3p")


class QuotaError(RuntimeError):
    """Safe-to-display quota request failure."""


class _DeadlineExceeded(QuotaError):
    """Raised when the shared quota budget is exhausted."""


class QuotaClient:
    """Fetch quota with one monotonic deadline shared by OAuth and endpoints.

    ``cache`` is a mutable per-account mapping supplied by the caller.  Entries
    contain only project routing hints and expire after six hours.  The caller
    owns persistence of that mapping alongside its existing cache.
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
        save_client_secret: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.user_agent = user_agent
        self.save_account = save_account
        self.cache = cache if cache is not None else {}
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.opener_factory = opener_factory or self._opener
        self.save_client_secret = save_client_secret
        self._client_credentials: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self._saved_oauth_secret: str | None = None
        self._working_oauth_secret: str | None = None

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
            raise _DeadlineExceeded("额度查询已超过总耗时限制。")
        request_timeout = max(0.001, min(float(self.config.get("requestTimeoutSeconds", 20)), remaining))
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
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
                raise _DeadlineExceeded("额度查询已超过总耗时限制。")
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

    def _installed_client_credentials(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Read the official client id/secret pair from the installed app.

        The Quota endpoints require the same public OAuth client the official
        Antigravity app uses. Rather than committing that value, extract it from
        the installed ``language_server.exe`` at runtime. Nothing is persisted.
        """
        if self._client_credentials is not None:
            return self._client_credentials
        ids: list[str] = []
        secrets: list[str] = []
        for path in _language_server_candidates(str(self.config.get("antigravityExecutable") or "")):
            try:
                with open(path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    ids.extend(match.group().decode("ascii") for match in _CLIENT_ID_PATTERN.finditer(data))
                    secrets.extend(match.group().decode("ascii") for match in _CLIENT_SECRET_PATTERN.finditer(data))
            except (OSError, ValueError):
                continue
        self._client_credentials = (
            tuple(dict.fromkeys(ids)),
            tuple(dict.fromkeys(secrets)),
        )
        return self._client_credentials

    def _oauth_client_secrets(self) -> tuple[str, ...]:
        explicit = str(self.config.get("oauthClientSecret") or "").strip()
        if explicit:
            return (explicit,)
        ids, secrets = self._installed_client_credentials()
        if not secrets:
            return ()
        client_id = str(self.config.get("oauthClientId") or "").strip()
        ordered: list[str] = []
        if client_id and client_id in ids and len(ids) == len(secrets):
            # The binary lists the matching id/secret pairs in the same order.
            index = ids.index(client_id)
            ordered.append(secrets[index])
            ordered.extend(secret for position, secret in enumerate(secrets) if position != index)
        else:
            ordered.extend(secrets)
        return tuple(dict.fromkeys(ordered))

    def _refresh_token(self, account: Any, deadline: float) -> str:
        token = account.payload["token"]
        expiry = _parse_time(token.get("expiry"))
        if token.get("access_token") and expiry and expiry > self.wall_clock() + timedelta(seconds=60):
            return str(token["access_token"])
        explicit_secret = bool(str(self.config.get("oauthClientSecret") or "").strip())
        secrets = self._oauth_client_secrets()
        if self._working_oauth_secret:
            secrets = (self._working_oauth_secret,) + tuple(
                secret for secret in secrets if secret != self._working_oauth_secret
            )
        if not secrets:
            raise QuotaError(
                "未找到官方 OAuth 客户端凭据；请安装官方 Antigravity，"
                "或设置 ANTIGRAVITY_OAUTH_CLIENT_SECRET。"
            )
        last_code: int | None = None
        for secret in secrets:
            form = urllib.parse.urlencode({
                "client_id": self.config.get("oauthClientId", ""),
                "client_secret": secret,
                "refresh_token": token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            }).encode("utf-8")
            try:
                value = self._request(OAUTH_URL, None, {}, deadline, form=form)
            except _DeadlineExceeded:
                raise
            except _HttpQuotaError as exc:
                last_code = exc.code
                if exc.code in (400, 401):
                    continue
                raise QuotaError(f"Google OAuth 刷新失败（HTTP {exc.code}）。") from None
            access = value.get("access_token")
            if not access:
                raise QuotaError("Google OAuth 响应中没有 access_token。")
            self._working_oauth_secret = secret
            if not explicit_secret and self.save_client_secret is not None and secret != self._saved_oauth_secret:
                self._saved_oauth_secret = secret
                try:
                    self.save_client_secret(secret)
                except Exception:
                    pass
            token["access_token"] = access
            token["expiry"] = (self.wall_clock() + timedelta(seconds=int(value.get("expires_in", 3600)))).isoformat()
            if value.get("refresh_token"):
                token["refresh_token"] = value["refresh_token"]
            self.save_account(account)
            return str(access)
        raise QuotaError(f"Google OAuth 刷新失败（HTTP {last_code or 400}）。")

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
        # Older releases cached routing hints learned from daily/sandbox
        # environments. Only reuse project ids that match the current hosts.
        if value.get("projectEndpoint") not in PROJECT_ENDPOINTS:
            value.pop("projectEndpoint", None)
            value.pop("projectId", None)
        if value:
            self.cache[account_id] = value
        else:
            self.cache.pop(account_id, None)
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
            "bucketId": str(raw.get("bucketId") or ""),
            "groupName": str(group.get("displayName") or ""),
        }

    def _project(self, account_id: str, token: str, deadline: float) -> str | None:
        entry = self._entry(account_id)
        cached = entry.get("projectId")
        cached_endpoint = entry.get("projectEndpoint")
        if isinstance(cached, str) and cached:
            return cached
        endpoints = ([cached_endpoint] if cached_endpoint in PROJECT_ENDPOINTS else []) + [e for e in PROJECT_ENDPOINTS if e != cached_endpoint]
        body = {"metadata": {"ideType": "ANTIGRAVITY"}}
        last_error = "项目发现失败。"
        for endpoint in endpoints:
            try:
                response = self._request(endpoint, token, body, deadline)
            except _DeadlineExceeded:
                raise
            except _HttpQuotaError as exc:
                if exc.code == 401:
                    raise QuotaError(_http_message(exc.code)) from None
                last_error = _http_message(exc.code)
                continue
            except QuotaError as exc:
                last_error = str(exc)
                continue
            project = response.get("cloudaicompanionProject")
            if project:
                project_id = str(project)
                self._store(account_id, projectId=project_id, projectEndpoint=endpoint)
                return project_id
            return None
        # Project discovery is optional: the quota endpoints accept an empty body.
        self.log_route(last_error)
        return None

    def log_route(self, _message: str) -> None:
        """Hook for callers that want to surface non-fatal routing failures."""

    def _parse_models(self, models: Any) -> dict[str, Any] | None:
        if not isinstance(models, dict):
            return None
        candidates: list[tuple[str, dict[str, Any]]] = []
        for name, info in models.items():
            if not isinstance(info, dict):
                continue
            quota = info.get("quotaInfo")
            if not isinstance(quota, dict):
                continue
            bucket = self._bucket(
                {
                    "remainingFraction": quota.get("remainingFraction"),
                    "resetTime": quota.get("resetTime"),
                    "displayName": str(name),
                    "window": "5h",
                },
                {"displayName": "models"},
            )
            if bucket is not None:
                candidates.append((str(name).lower(), bucket))
        if not candidates:
            return None
        gemini = [pair for pair in candidates if "gemini" in pair[0]]
        pool = gemini or candidates
        return min(pool, key=lambda pair: pair[1]["remainingFraction"])[1]

    def _models(self, token: str, project: str | None, deadline: float) -> dict[str, Any] | None:
        body = {"project": project} if project else {}
        last_error = "模型额度接口未返回可识别数据。"
        for endpoint in MODELS_ENDPOINTS:
            payload = dict(body)
            retried_without_project = False
            while True:
                try:
                    response = self._request(endpoint, token, payload, deadline)
                except _DeadlineExceeded:
                    raise
                except _HttpQuotaError as exc:
                    if exc.code == 401:
                        raise QuotaError(_http_message(exc.code)) from None
                    if exc.code == 403 and "project" in payload and not retried_without_project:
                        payload = {}
                        retried_without_project = True
                        continue
                    last_error = _http_message(exc.code)
                    break
                except QuotaError as exc:
                    last_error = str(exc)
                    break
                bucket = self._parse_models(response.get("models"))
                if bucket is not None:
                    return bucket
                last_error = "模型额度接口返回了空数据。"
                break
        self.log_route(last_error)
        return None

    @staticmethod
    def _bucket_matches(bucket: dict[str, Any], kind: str) -> bool:
        text = " ".join(
            str(bucket.get(key) or "")
            for key in ("window", "bucketId", "displayName")
        ).lower()
        return bool((_FIVE_HOUR_PATTERN if kind == "five" else _WEEKLY_PATTERN).search(text))

    @staticmethod
    def _is_general_group(bucket: dict[str, Any]) -> bool:
        text = " ".join(str(bucket.get(key) or "") for key in ("groupName", "description")).lower()
        return not any(token in text for token in _THIRD_PARTY_GROUP_TOKENS)

    def _select_buckets(self, groups: list[Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        buckets: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for raw in group.get("buckets") or []:
                if not isinstance(raw, dict):
                    continue
                bucket = self._bucket(raw, group)
                if bucket is not None:
                    buckets.append(bucket)
        general = [bucket for bucket in buckets if self._is_general_group(bucket)]

        def pick(kind: str) -> dict[str, Any] | None:
            for pool in (general, buckets):
                for bucket in pool:
                    if self._bucket_matches(bucket, kind):
                        return bucket
            return None

        return pick("five"), pick("week")

    def _summary(self, token: str, project: str | None, deadline: float) -> tuple[dict[str, Any] | None, dict[str, Any] | None] | None:
        body = {"project": project} if project else {}
        last_error = "额度摘要接口未返回可识别数据。"
        for endpoint in QUOTA_ENDPOINTS:
            payload = dict(body)
            retried_without_project = False
            while True:
                try:
                    response = self._request(endpoint, token, payload, deadline)
                except _DeadlineExceeded:
                    raise
                except _HttpQuotaError as exc:
                    if exc.code == 401:
                        raise QuotaError(_http_message(exc.code)) from None
                    if exc.code == 403 and "project" in payload and not retried_without_project:
                        payload = {}
                        retried_without_project = True
                        continue
                    last_error = _http_message(exc.code)
                    break
                except QuotaError as exc:
                    last_error = str(exc)
                    break
                groups = response.get("groups")
                if not isinstance(groups, list) or not groups:
                    last_error = "Google 返回的额度分组为空。"
                    break
                five, weekly = self._select_buckets(groups)
                if five is None and weekly is None:
                    last_error = "Google 返回了额度数据，但未识别出 5 小时或一周窗口。"
                    break
                return five, weekly
        self.log_route(last_error)
        return None

    def quota(self, account: Any) -> dict[str, Any]:
        result = {"id": account.account_id, "success": False, "fiveHour": None, "weekly": None, "error": None,
                  "attemptedAt": self.wall_clock().isoformat()}
        account_id = account.account_id
        try:
            budget = max(1.0, float(self.config.get("quotaDeadlineSeconds", 45)))
            deadline = self.monotonic() + budget
            token = self._refresh_token(account, deadline)
            project = self._project(account_id, token, deadline)

            five: dict[str, Any] | None = None
            weekly: dict[str, Any] | None = None
            errors: list[str] = []
            deadline_error: str | None = None

            try:
                five = self._models(token, project, deadline)
            except _DeadlineExceeded:
                deadline_error = "额度查询已超过总耗时限制。"
            except QuotaError as exc:
                errors.append(str(exc))

            if deadline_error is None:
                try:
                    summary = self._summary(token, project, deadline)
                except _DeadlineExceeded:
                    deadline_error = "额度查询已超过总耗时限制。"
                except QuotaError as exc:
                    errors.append(str(exc))
                else:
                    if summary is not None:
                        summary_five, summary_weekly = summary
                        five = summary_five or five
                        weekly = summary_weekly

            if five is None and weekly is None:
                if deadline_error is not None:
                    raise QuotaError(deadline_error)
                if errors:
                    raise QuotaError(errors[0])
                raise QuotaError("额度接口未返回可识别数据。")
            result.update(success=True, fiveHour=five, weekly=weekly)
            return result
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


def _language_server_candidates(executable: str) -> list[Path]:
    """Locate the official ``language_server.exe`` holding the OAuth client."""
    candidates: list[Path] = []
    exe = Path(executable).expanduser() if executable.strip() else None
    if exe is not None:
        if exe.name.lower() == "language_server.exe":
            candidates.append(exe)
        else:
            candidates.append(exe.parent / "resources" / "bin" / "language_server.exe")
            candidates.append(exe.parent / "bin" / "language_server.exe")
    for root_name in ("LOCALAPPDATA", "PROGRAMFILES"):
        root = os.environ.get(root_name)
        if not root:
            continue
        for name in ("antigravity", "Antigravity", "Antigravity IDE"):
            candidates.append(Path(root) / "Programs" / name / "resources" / "bin" / "language_server.exe")
            candidates.append(Path(root) / name / "resources" / "bin" / "language_server.exe")
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if candidate.is_file():
                result.append(candidate)
        except OSError:
            continue
    return result


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

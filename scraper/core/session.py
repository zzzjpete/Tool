from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from scraper.core.exceptions import RateLimited, ScraperError
from scraper.core.rate_limit import TokenBucket


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# Per-session Accept-Language variants — real users have slightly different
# language preference orderings; varying this widens the fingerprint surface.
_ACCEPT_LANGUAGE_OPTIONS = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "zh,en-US;q=0.9,en;q=0.8",
    "zh-CN,zh-Hans;q=0.9,en;q=0.8",
]


# curl_cffi impersonation targets. Each value spoofs a specific browser's TLS
# handshake (JA3/JA4), HTTP/2 frame ordering, and header order to match. We rotate
# per session — varying the TLS fingerprint by run, not request (a fingerprint
# that changes mid-session is itself anomalous).
_IMPERSONATE_POOL = [
    "chrome120",
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome133a",
    "chrome136",
]


def _pick_ua() -> str:
    """Pick a fresh UA via fake-useragent. Pool spans Chrome/Edge/Safari/Firefox
    on Windows/macOS/iOS/Android, desktop + mobile.

    Called once per `HttpSession` (not per request — switching UA mid-session is
    itself suspicious). Widening the pool here is defense-in-depth, not a silver
    bullet against sophisticated detection.
    """
    try:
        from fake_useragent import UserAgent
        return UserAgent(
            browsers=["Chrome", "Edge", "Safari", "Firefox"],
            os=["Windows", "Mac OS X", "Linux", "iOS", "Android"],
            platforms=["desktop", "mobile"],
        ).random
    except Exception:
        return DEFAULT_UA


_CHROME_VER_RE = re.compile(r"Chrome/(\d+)")
_EDGE_VER_RE = re.compile(r"Edg(?:e|A|iOS)?/(\d+)")
_FIREFOX_RE = re.compile(r"Firefox/")
_SAFARI_RE = re.compile(r"Version/[\d.]+ +Safari/")


def _pick_impersonate(ua: str) -> str:
    """Pick a curl_cffi impersonate target that's *coherent* with the picked UA.

    A TLS handshake claiming to be Chrome paired with a UA string claiming to be
    Safari is a clear bot signal. Strategy:
      * Edge UA → impersonate edge101 (only modern Edge target available)
      * Safari UA → impersonate safari180 (most-recent stable)
      * Chrome UA (or anything else) → pick from the Chrome pool, biased toward
        the version family in the UA string when we can match it.
    """
    if _EDGE_VER_RE.search(ua):
        return "edge101"
    if _SAFARI_RE.search(ua) and not _CHROME_VER_RE.search(ua):
        return "safari180"
    chrome_m = _CHROME_VER_RE.search(ua)
    if chrome_m:
        v = int(chrome_m.group(1))
        # Available chrome targets sorted ascending — bisect down to the
        # nearest-but-≤ option so the impersonation isn't claiming a Chrome
        # version newer than the UA advertises.
        targets = [120, 123, 124, 131, 133, 136]
        candidates = [t for t in targets if t <= v]
        if candidates:
            chosen = max(candidates)
            return f"chrome{chosen}a" if chosen == 133 else f"chrome{chosen}"
    return random.choice(_IMPERSONATE_POOL)


def _client_hints_for(ua: str) -> dict[str, str]:
    """Build matching `Sec-CH-UA*` headers for the picked UA.

    Returns an empty dict for Safari/Firefox — sending Chromium client hints with
    a non-Chromium UA is itself a fingerprint anomaly. For Chromium-family UAs
    (Chrome, Edge) we synthesize the brand list, mobile flag, and platform.
    """
    chrome_m = _CHROME_VER_RE.search(ua)
    edge_m = _EDGE_VER_RE.search(ua)
    if not chrome_m and not edge_m:
        # Pure Safari, Firefox — these browsers don't send Sec-CH-UA hints at all.
        return {}

    v = (edge_m or chrome_m).group(1)
    if edge_m:
        brand_list = (
            f'"Microsoft Edge";v="{v}", '
            f'"Chromium";v="{v}", "Not-A.Brand";v="99"'
        )
    else:
        brand_list = (
            f'"Google Chrome";v="{v}", '
            f'"Chromium";v="{v}", "Not-A.Brand";v="99"'
        )

    mobile = "?1" if ("Mobi" in ua or "Android" in ua) else "?0"

    if "Windows" in ua:
        plat = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        plat = '"macOS"'
    elif "Android" in ua:
        plat = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        plat = '"iOS"'
    else:
        plat = '"Linux"'

    return {
        "Sec-CH-UA": brand_list,
        "Sec-CH-UA-Mobile": mobile,
        "Sec-CH-UA-Platform": plat,
    }


def _pick_accept_language() -> str:
    return random.choice(_ACCEPT_LANGUAGE_OPTIONS)


def _load_cookies_into_client(client: AsyncSession, path: Path) -> None:
    """Load previously-saved cookies into the client's jar. Silent if path missing."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("cookie persistence file {} unreadable, ignoring: {}", path, e)
        return
    existing = {c.name for c in client.cookies.jar}
    loaded = 0
    for c in data:
        name = c.get("name")
        if not name or name in existing:
            # Don't overwrite an explicit configured cookie with a stale persisted one.
            continue
        client.cookies.set(
            name,
            c.get("value", ""),
            domain=c.get("domain", ""),
            path=c.get("path", "/"),
        )
        loaded += 1
    if loaded:
        logger.debug("loaded {} persisted cookies from {}", loaded, path)


def _save_cookies_from_client(client: AsyncSession, path: Path) -> None:
    """Dump the client's cookie jar to disk as JSON."""
    cookies: list[dict[str, Any]] = []
    for c in client.cookies.jar:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
        })
    if not cookies:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    logger.debug("persisted {} cookies to {}", len(cookies), path)


class HttpSession:
    """Async HTTP wrapper with retry, rate limiting, and shared cookie jar.

    Backed by `curl_cffi` rather than `httpx` — same async API, but `curl_cffi`
    spoofs a real browser's TLS/JA3+JA4 handshake and HTTP/2 frame order via its
    `impersonate=` parameter. The Python `httpx`/`requests` TLS fingerprint is
    trivial to identify; this neutralizes one of the biggest single bot signals
    a server can see before any header parsing happens.
    """

    def __init__(
        self,
        *,
        rate: float,
        timeout: float = 20.0,
        max_retries: int = 4,
        http2: bool = True,
        proxy: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        cookie: str = "",
        persist_cookies_to: Optional[Path] = None,
    ) -> None:
        # `http2` is accepted for backwards-compat with callers but has no effect:
        # curl_cffi picks HTTP version based on the impersonate profile, which is
        # always HTTP/2 for the modern Chrome targets we use.
        del http2
        self._bucket = TokenBucket(rate=rate)
        self._max_retries = max_retries
        self._persist_cookies_to = persist_cookies_to
        ua = _pick_ua()
        impersonate = _pick_impersonate(ua)
        base_headers = {
            "User-Agent": ua,
            "Accept-Language": _pick_accept_language(),
            "Accept": "application/json, text/plain, */*",
            # Sec-CH-UA* hints matched to the picked UA. Empty dict for non-Chromium
            # — sending these with a Safari/Firefox UA is itself a bot tell.
            **_client_hints_for(ua),
            # Sec-Fetch-* headers. All modern browsers (Chrome ≥76, FF ≥90, Safari ≥16.4)
            # send these on every request — their absence is a strong bot signal.
            # Defaults reflect AJAX XHR semantics, which matches how we use the session
            # (calling /x/web-interface/*, /api/v4/* from JS). Sec-Fetch-User is only
            # set for user-initiated nav requests, so we deliberately omit it.
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if headers:
            base_headers.update(headers)
        if cookie:
            base_headers["Cookie"] = cookie
        self._client = AsyncSession(
            impersonate=impersonate,
            timeout=timeout,
            headers=base_headers,
            proxy=proxy,
            allow_redirects=True,
            # Don't let curl_cffi inject its own browser-default headers — we want
            # full control over Accept/Accept-Language/etc to keep them consistent
            # with the manual headers above.
            default_headers=False,
        )
        logger.debug("HttpSession opened (impersonate={} ua_family={})", impersonate, _ua_family(ua))
        # Load previously-persisted anonymous cookies (buvid3, _xsrf, etc.) so the
        # session looks like a returning user, not a brand-new one. Configured-cookie
        # values in `cookie=` win — we've already set them above and the loader
        # below uses `setdefault` semantics by skipping names already in the jar.
        if self._persist_cookies_to is not None:
            _load_cookies_into_client(self._client, self._persist_cookies_to)

    async def __aenter__(self) -> "HttpSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        # Persist the cookie jar before the client tears down. Best-effort —
        # IO failure here shouldn't propagate and mask the actual scrape result.
        if self._persist_cookies_to is not None:
            try:
                _save_cookies_from_client(self._client, self._persist_cookies_to)
            except Exception as e:
                logger.warning("failed to persist cookie jar to {}: {}", self._persist_cookies_to, e)
        await self._client.close()

    @property
    def client(self) -> AsyncSession:
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        json: Any = None,
        data: Any = None,
        expect_json: bool = True,
    ) -> Any:
        await self._bucket.acquire()
        # 200–900 ms jitter on top of the token bucket. The bucket gives constant
        # rps, which is itself a fingerprint; widening the jitter window makes the
        # request cadence harder to model. Costs ~0.5s/request on average — fine
        # given the conservative rate limits.
        await asyncio.sleep(random.uniform(0.2, 0.9))

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=0.6, max=8.0),
            retry=retry_if_exception_type((RequestsError, RateLimited)),
            reraise=True,
        ):
            with attempt:
                logger.debug(
                    "{} {} params={} attempt={}",
                    method,
                    url,
                    dict(params) if params else {},
                    attempt.retry_state.attempt_number,
                )
                resp = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json,
                    data=data,
                )
                if resp.status_code in (429, 412):
                    # Polite back-off before retry.
                    await asyncio.sleep(2 + random.random() * 3)
                    raise RateLimited(f"{resp.status_code} from {url}")
                if resp.status_code >= 500:
                    # curl_cffi only raises RequestsError for transport-level
                    # failures, so we synthesize one here to engage the retry
                    # path on transient 5xx server errors.
                    raise RequestsError(f"server error {resp.status_code}")
                if resp.status_code >= 400:
                    raise ScraperError(
                        f"HTTP {resp.status_code} {url}: {resp.text[:300]}"
                    )
                if expect_json:
                    return resp.json()
                return resp.text
        raise ScraperError("retry loop exhausted without return")

    async def get(self, url: str, **kw: Any) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Any:
        return await self.request("POST", url, **kw)


def _ua_family(ua: str) -> str:
    """One-word UA family for log messages."""
    if _EDGE_VER_RE.search(ua):
        return "edge"
    if _CHROME_VER_RE.search(ua):
        return "chrome"
    if _SAFARI_RE.search(ua):
        return "safari"
    if _FIREFOX_RE.search(ua):
        return "firefox"
    return "unknown"

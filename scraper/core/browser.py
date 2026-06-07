"""Playwright-backed fallback fetcher for pages that the JSON API cannot reach.

Use this when:
  - the page renders content client-side and there is no public XHR you can hit
  - the endpoint requires a signature (e.g. Zhihu's x-zse-96) we don't reproduce
  - anti-bot rejects raw httpx requests but accepts a real browser fingerprint

Playwright is an *optional* dependency — `import scraper.core.browser` succeeds without
it; the import only fails when you actually open a `BrowserSession`. Install with:

    pip install playwright
    python -m playwright install chromium

The session mirrors `HttpSession`: token-bucket rate-limited, async context manager,
shared cookie jar. Scrapers can route specific calls through it without rewriting
their architecture.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from scraper.core.exceptions import ScraperError
from scraper.core.rate_limit import TokenBucket


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# Minimal stealth: hide the obvious headless tells in JS. Not a full bypass —
# sophisticated detection (canvas fingerprint, audio fingerprint, etc.) still works.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


class BrowserSession:
    """Async Playwright wrapper for JS-rendered fetches.

    Lazily imports playwright so the rest of the framework runs without it installed.
    """

    def __init__(
        self,
        *,
        rate: float = 0.5,
        headless: bool = True,
        cookie: str = "",
        user_agent: str = DEFAULT_UA,
        proxy: Optional[str] = None,
        cookie_domain: Optional[str] = None,
        stealth: bool = True,
        user_data_dir: Optional[str] = None,
    ) -> None:
        self._bucket = TokenBucket(rate=rate)
        self._headless = headless
        self._cookie = cookie
        self._user_agent = user_agent
        self._proxy = proxy
        self._cookie_domain = cookie_domain
        self._stealth = stealth
        # When set, the browser uses a persistent profile dir (localStorage,
        # IndexedDB, service workers, cookies all survive across runs). Lets the
        # user log in once and stay logged in. Costs ~50 MB on disk per profile.
        self._user_data_dir = user_data_dir
        self._pw = None
        self._browser = None
        self._context = None
        self._stealth_applied = False

    async def __aenter__(self) -> "BrowserSession":
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ScraperError(
                "playwright is not installed. Run: pip install playwright "
                "&& python -m playwright install chromium"
            ) from e

        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self._headless}
        if self._stealth:
            # --disable-blink-features=AutomationControlled removes the most obvious
            # `navigator.webdriver === true` tell at the Chromium level.
            launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        if self._user_data_dir:
            # Persistent profile path — login state, localStorage, IndexedDB, and the
            # cookie jar all survive across runs. `launch_persistent_context` returns
            # the BrowserContext directly; there is no separate Browser handle to
            # close on teardown.
            from pathlib import Path as _Path
            _Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = await self._pw.chromium.launch_persistent_context(
                self._user_data_dir,
                user_agent=self._user_agent,
                locale="zh-CN",
                viewport={"width": 1366, "height": 768},
                **launch_kwargs,
            )
            self._browser = None  # owned by the persistent context
        else:
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
            self._context = await self._browser.new_context(
                user_agent=self._user_agent,
                locale="zh-CN",
                viewport={"width": 1366, "height": 768},
            )
        if self._stealth:
            # Layer 1: the manual init script handles the easy tells (navigator.webdriver,
            # navigator.languages, navigator.plugins, window.chrome). Kept as a fallback
            # for when playwright-stealth isn't installed.
            await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
            # Layer 2: playwright-stealth covers ~20 additional fingerprint vectors
            # (canvas, WebGL, AudioContext, mediaDevices, permissions, screen, etc.).
            # Optional dep — fall back to layer 1 alone if not installed.
            self._stealth_applied = await _try_apply_playwright_stealth(self._context)
        if self._cookie and self._cookie_domain:
            await self._context.add_cookies(_parse_cookie_string(self._cookie, self._cookie_domain))
        logger.debug(
            "browser session opened (headless={} stealth={} plus={})",
            self._headless, self._stealth,
            "playwright-stealth" if self._stealth_applied else "manual-only",
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def get_html(
        self,
        url: str,
        *,
        wait_for: Optional[str] = None,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
    ) -> str:
        """Navigate to `url` and return the fully-rendered HTML.

        `wait_for` is an optional CSS selector to wait for before reading content —
        use it when the page renders asynchronously after initial load.

        `wait_until` controls how long goto() blocks. `"load"` waits for every
        subresource (images, ads, iframes) — correct for pages whose content is
        painted late, but on ad-heavy sites the load event can take >30s and time
        out. For pages whose payload is server-rendered into the initial HTML
        (e.g. Zhihu's `js-initialData` blob), pass `"domcontentloaded"`: it fires
        as soon as the HTML is parsed (1–3s), which is all we need to read the SSR
        JSON — and it sidesteps the slow-load timeout entirely.
        """
        await self._bucket.acquire()
        page = await self._require_context().new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            await _settle(page)
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=10_000)
            return await page.content()
        finally:
            await page.close()

    async def evaluate(self, url: str, expression: str, *, wait_for: Optional[str] = None) -> Any:
        """Navigate to `url` and evaluate a JS expression in page context. Returns its value."""
        await self._bucket.acquire()
        page = await self._require_context().new_page()
        try:
            await page.goto(url, wait_until="load")
            await _settle(page)
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=10_000)
            return await page.evaluate(expression)
        finally:
            await page.close()

    async def capture_xhr(
        self,
        url: str,
        predicate: Callable[[str], bool],
        *,
        timeout_ms: int = 15_000,
        after_goto: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> dict:
        """Navigate to `url` and return the JSON body of the first XHR matching `predicate`.

        Useful when the page makes a request to an endpoint we'd otherwise have to
        reverse-engineer (e.g. signed routes). Predicate receives the request URL.

        If `after_goto` is given, it's called with the Page after navigation but
        before we wait for the XHR — use it to scroll or click something that
        triggers a lazy-loaded request. The signature is `async (page) -> None`.
        """
        await self._bucket.acquire()
        page = await self._require_context().new_page()
        try:
            async with page.expect_response(
                lambda r: predicate(r.url), timeout=timeout_ms
            ) as info:
                await page.goto(url, wait_until="domcontentloaded")
                if after_goto is not None:
                    await after_goto(page)
            response = await info.value
            return await response.json()
        finally:
            await page.close()

    def _require_context(self):
        if self._context is None:
            raise RuntimeError("use BrowserSession as an async context manager")
        return self._context


async def _try_apply_playwright_stealth(context) -> bool:
    """Best-effort apply playwright-stealth to the context. Returns True on success.

    `playwright-stealth` is an optional dep — if not installed, fall back to the
    manual init script alone. If installed but its API call fails (version skew),
    log and keep going rather than crashing the scrape.
    """
    try:
        from playwright_stealth import Stealth
    except ImportError:
        return False
    try:
        await Stealth().apply_stealth_async(context)
        return True
    except Exception as e:
        logger.warning("playwright-stealth apply failed; falling back to manual stealth: {}", e)
        return False


async def _settle(page) -> None:
    """Best-effort wait for the page to stop navigating after `load`.

    Some sites do JS-driven redirects after `load` fires, which causes `page.content()`
    to raise "page is navigating and changing the content". A short networkidle wait
    with a low timeout handles this without blocking on sites whose network never
    fully settles (analytics beacons, long-poll connections, etc.).
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass


def _parse_cookie_string(cookie: str, domain: str) -> list[dict[str, Any]]:
    """Convert a `name=value; name2=value2` browser cookie string into Playwright cookie objects."""
    out: list[dict[str, Any]] = []
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append({"name": name.strip(), "value": value.strip(), "domain": domain, "path": "/"})
    return out

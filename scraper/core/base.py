from __future__ import annotations

from abc import ABC
from pathlib import Path
from typing import Optional

from loguru import logger

from scraper.core.config import Config
from scraper.core.session import HttpSession
from scraper.core.storage import SqliteStorage


class BaseScraper(ABC):
    """Common scaffolding shared by per-platform scrapers."""

    platform: str = "base"
    base_url: str = ""
    default_headers: dict[str, str] = {}

    def __init__(
        self,
        config: Config,
        storage: Optional[SqliteStorage] = None,
        *,
        rate: Optional[float] = None,
        cookie: Optional[str] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self._rate = rate or self._default_rate()
        self._cookie = cookie if cookie is not None else self._default_cookie()
        self.session: Optional[HttpSession] = None

    def _default_rate(self) -> float:
        return getattr(self.config.rate_limit, self.platform, 1.0)

    def _default_cookie(self) -> str:
        plat = getattr(self.config, self.platform, None)
        return plat.cookie if plat else ""

    async def __aenter__(self):
        self.session = HttpSession(
            rate=self._rate,
            timeout=self.config.http.timeout,
            max_retries=self.config.http.max_retries,
            http2=self.config.http.http2,
            proxy=self.config.http.proxy,
            headers=self.default_headers,
            cookie=self._cookie,
            # Persist anonymous cookies (buvid3, _xsrf, etc.) across runs so the
            # session looks like a returning user, not a brand-new fingerprint.
            persist_cookies_to=Path("data/cookies") / f"{self.platform}.json",
        )
        logger.debug("opened session for {} (rate={}/s)", self.platform, self._rate)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.aclose()
        self.session = None

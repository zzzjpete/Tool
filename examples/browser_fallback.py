"""Use BrowserSession to render a JS-heavy / anti-bot page when the JSON API is not enough.

Prereq:
    pip install playwright
    python -m playwright install chromium

Zhihu blocks bare HTTP requests and aggressively detects headless Chromium —
BrowserSession's stealth flag (on by default) masks the obvious tells
(navigator.webdriver, AutomationControlled, etc.) which together with a
logged-in cookie gets the page to render normally.
"""

import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper import BrowserSession, load_config


async def main() -> None:
    cfg = load_config("config.yaml")
    cookie = cfg.zhihu.cookie  # required for Zhihu; empty string is fine for public sites

    async with BrowserSession(
        rate=0.5,
        cookie=cookie,
        cookie_domain=".zhihu.com",
    ) as bs:
        html = await bs.get_html("https://www.zhihu.com/question/6285588743")
        print(f"got {len(html)} bytes of rendered HTML")

        m = re.search(r"<title[^>]*>([^<]+)</title>", html)
        if m:
            print(f"title: {m.group(1)}")
        m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if m:
            print(f"question: {m.group(1)}")
        print(f"answer cards on page: {len(re.findall(r'AnswerItem', html))}")


if __name__ == "__main__":
    asyncio.run(main())

# scraper

Async scraping + ML-analysis toolkit for **Bilibili**, **Zhihu**, **Weibo**, and
**Baidu Tieba**. Built for personal/research use on Windows.

Given a keyword, it fetches relevant posts and comments, persists them to SQLite
+ exports CSVs, and gives you a Jupyter notebook + pandas helpers for
discussion-volume analysis. Think MediaCrawler-style search mode, but with the
analysis layer included.

---

## Get started in 3 commands

```powershell
pip install -e .
python -m scraper init           # guided setup: config + cookie + Playwright
python -m scraper scrape "电动车" --count 10
```

That's it. Open the result:

```powershell
python -m scraper status         # what's in your DB
python -m scraper view question 536080693
jupyter notebook examples\notebooks\discussion_volume.ipynb
```

CSVs are auto-written to `data/exports/` (UTF-8 BOM, Excel-friendly).

For a one-page walkthrough: see **快速开始.txt**.
For the full manual: see **使用说明.txt**.

---

## What you get

**Collection**
- Search → top-N → fan-out fetch with full content + comments. Cross-platform
  in one command: `scrape "<kw>"` runs all four platforms in parallel.
- Bilibili: videos, comments, dynamics, user uploads, search (WBI-signed).
- Zhihu: questions (browser-rendered to bypass x-zse-96), top-5 answers per
  question, per-answer comments, columns, articles, search.
- Weibo: keyword search + hot-flow comments + single posts via the `m.weibo.cn`
  JSON API (no signing). Keyword search needs a logged-in cookie (the `SUB` token).
- Tieba: keyword → threads + reply floors, sign-free via the mobile JSON 吧 feed
  (`getFrsData` / `getPbData`), with an HTML-search fallback. Works anonymously.
- Browser fallback via Playwright with stealth flags (works around Zhihu's
  headless detection).

**Anti-detection**
- Per-session UA rotation across Chrome/Edge/Safari/Firefox × desktop/mobile,
  with matching `Sec-CH-UA` client hints.
- Randomized `Accept-Language`, 200–900 ms request jitter.
- Conservative rate limits (0.7 bilibili / 0.5 zhihu / 0.8 weibo / 0.8 tieba, rps).
- `CookieExpired` and `SoftBanned` exceptions catch state issues early.

**Storage**
- SQLite with `raw` JSON column on every row → re-derive features without
  re-scraping.
- Cross-platform unified views (`posts_unified`, `comments_unified`) with
  **Chinese column names** matching the MediaCrawler export schema:
  `平台 / 帖子ID / 笔记链接 / 作者ID / 作者昵称 / 发布时间 / 标题 / 内容 / 标签 / 评论数量 / 点赞数量 / 收藏数量 / 转发数量 / 播放数量 / 抓取时间`
- Append-only `engagement_snapshots` — re-scrape daily, get a time series.
- `scrape_runs` + `keyword_posts` so every fetched item is linked to its run.
- Auto-export of every scrape run to CSV.

**Analysis**
- `scraper.analyze` — pandas helpers: `posts_for_keyword`, `volume_by_day`,
  `engagement_history`, `comments_for_post`, `scrape_run_summary`, `as_dataframe`.
- `scraper.viz` — matplotlib helpers (with CJK font config) for one-line plots.
- Starter notebook at `examples/notebooks/discussion_volume.ipynb` — change
  `KEYWORD`, Run All, get a finished report with charts.

**Ergonomics**
- `scraper init` — guided setup
- `scraper doctor` — diagnostic with fix commands
- `scraper status` — DB inventory + recent runs
- `scraper view <kind> <id>` — pretty-print one record
- `scraper interactive` — guided keyword scrape

---

## Requirements

- Python 3.12+ (tested on 3.14)
- Windows 10/11 (PowerShell)
- ~500 MB disk including Playwright Chromium
- A logged-in Zhihu cookie (anonymous Zhihu API access is mostly blocked)
- A logged-in Weibo cookie — the `SUB` token — for Weibo keyword search
  (Bilibili and Tieba work anonymously)

---

## CLI reference

```powershell
# Setup & health
python -m scraper init                       # first-time setup wizard
python -m scraper doctor                     # diagnostic check
python -m scraper status                     # DB inventory

# Scraping
python -m scraper scrape "<kw>" --count 20            # all four platforms in parallel
python -m scraper scrape "<kw>" --platforms weibo,tieba --count 20   # restrict to a subset
python -m scraper bili scrape "<kw>" --count 20
python -m scraper zhihu scrape "<kw>" --count 20 --comments-per-answer 5
python -m scraper weibo scrape "<kw>" --count 20 --comments 10
python -m scraper tieba scrape "<kw>" --count 20 --replies 10
python -m scraper interactive                          # guided prompts

# Per-record
python -m scraper bili video BV1xx411c7XW
python -m scraper bili dynamics <mid>
python -m scraper zhihu question 12345
python -m scraper zhihu column c_1234567890
python -m scraper weibo comments <post_id>
python -m scraper tieba thread <tid>
python -m scraper grab "https://m.weibo.cn/detail/<id>"   # paste any supported URL

# View & export
python -m scraper view video BV1xx411c7XW
python -m scraper view answer 2199786648 --comments 20
python -m scraper view weibo <post_id>
python -m scraper view thread <tid>
python -m scraper export --format csv --table posts_unified --out posts.csv
```

Every `scrape` command auto-exports CSVs; `--no-csv` to disable.

---

## Python API

```python
import asyncio
from scraper import (
    BilibiliScraper, ZhihuScraper, WeiboScraper, TiebaScraper,
    SqliteStorage, load_config,
)
from scraper.analyze import posts_for_keyword, volume_by_day

async def collect():
    cfg = load_config("config.yaml")
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()
    async with BilibiliScraper(cfg, storage) as bili:
        await bili.scrape_keyword("电动车", count=20, comments_pages=1)
    async with TiebaScraper(cfg, storage) as tieba:        # sign-free, works anonymously
        await tieba.scrape_keyword("电动车", count=20, replies_per_thread=10)
    await storage.close()

asyncio.run(collect())

# Then analyze
df = posts_for_keyword("电动车")
daily = volume_by_day("电动车")
```

---

## Layout

```
scraper/
  core/           session, browser, storage, config, exceptions, rate_limit
  platforms/
    bilibili/     WBI signing + endpoints
    zhihu/        initialData parser + browser-based question fetcher
    weibo/        m.weibo.cn container API + hotflow comments
    tieba/        f/search/res + mobile JSON (getFrsData / getPbData), sign-free
  analyze.py      pandas helpers
  viz.py          matplotlib helpers
  cli.py          all CLI commands
examples/
  notebooks/
    discussion_volume.ipynb   ← start here for analysis
  scrape_*.py                 ← single-file usage examples
config.example.yaml
快速开始.txt          ← 30-line quickstart
使用说明.txt          ← full Chinese manual
CLAUDE.md             ← developer notes / architectural reasoning
```

---

## Notes

- Respect each site's ToS. This is for personal / research use; rate limits are
  intentionally conservative.
- All four sites change their anti-bot logic regularly. If `scrape` starts
  returning empty results or 403s, run `python -m scraper doctor`, then refresh
  your cookie.
- Zhihu's question/answer endpoints require `x-zse-96` signing which we don't
  reproduce — those are fetched via Playwright by parsing the page's embedded
  `js-initialData` JSON.
- Weibo keyword search requires the `SUB` cookie (anonymous search returns
  `ok=-100`); without it Weibo is skipped during a scrape, not fatal.
- Tieba uses the sign-free mobile JSON path; on flagged / datacenter IPs the HTML
  search page may 403, so it prefers the 吧 feed. See CLAUDE.md for details.

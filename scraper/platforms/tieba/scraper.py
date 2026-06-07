from __future__ import annotations

import asyncio
import json as _json
import re
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup
from loguru import logger

from scraper.core.base import BaseScraper
from scraper.core.exceptions import ParseError, ScraperError, SoftBanned
from scraper.core.progress import StepEvent, TotalEvent


class TiebaScraper(BaseScraper):
    """Baidu Tieba scraper on the sign-free path.

    Two seams, both reusing the shared `HttpSession` (no request signing, no browser):
      - keyword search → the `/f/search/res` HTML results page, parsed with BeautifulSoup
        (robust to class churn: we anchor on `/p/<tid>` links).
      - thread content + replies → the mobile JSON endpoint `/mg/p/getPbData`.

    MediaCrawler's current Tieba client uses MD5-signed `/c/f/pb/page_pc` + a browser;
    we deliberately take the lighter path. If Baidu ever blocks it, the search step
    still yields title/excerpt/forum metadata, and the signed endpoint is the documented
    fallback (see CLAUDE.md). Thread detail is best-effort: a thread always persists with
    at least its search metadata even if the reply JSON shape drifts.
    """

    platform = "tieba"
    base_url = "https://tieba.baidu.com"
    default_headers = {
        "Referer": "https://tieba.baidu.com/",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._warmed_up = False

    # ---- internal helpers ---------------------------------------------------

    async def _warmup(self) -> None:
        """GET the homepage once so the jar picks up the `BAIDUID` anti-bot cookie."""
        if self._warmed_up or self.session is None:
            return
        jar_names = {c.name for c in self.session.client.cookies.jar}
        if "BAIDUID" not in jar_names:
            try:
                await self.session.client.get("https://tieba.baidu.com/")
            except Exception as e:  # noqa: BLE001 — warmup is best-effort
                logger.debug("tieba warmup GET failed (non-fatal): {}", e)
        self._warmed_up = True

    async def _get_html(self, path: str, params: Optional[dict] = None) -> str:
        assert self.session is not None, "use TiebaScraper as an async context manager"
        url = path if path.startswith("http") else self.base_url + path
        try:
            text = await self.session.get(url, params=params, expect_json=False)
        except ScraperError as e:
            msg = str(e)
            if any(code in msg for code in ("403", "418", "412")):
                raise SoftBanned(f"tieba blocked the request: {msg[:120]}") from e
            raise
        if not isinstance(text, str):
            raise ParseError(f"unexpected non-text response from {path}")
        return text

    async def _get_json(self, path: str, params: Optional[dict] = None) -> dict:
        """Fetch as text then json.loads — avoids an uncaught JSONDecodeError escaping
        the retry layer when Tieba returns an HTML error page instead of JSON."""
        text = await self._get_html(path, params)
        try:
            data = _json.loads(text)
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"tieba {path} did not return JSON") from e
        if not isinstance(data, dict):
            raise ParseError(f"tieba {path} returned non-object JSON")
        return data

    # ---- public endpoints ---------------------------------------------------

    async def search(
        self,
        keyword: str,
        *,
        pages: int = 1,
        persist: bool = False,
    ) -> list[dict]:
        """Keyword search. Returns hit dicts {tid, title, excerpt, forum_name,
        author_name, author_id, reply_num, created_ts, created_at}.

        Strategy: prefer the mobile JSON bar feed (`/mg/f/getFrsData`, treating the
        keyword as a 吧 name) — it's reliable anonymously and returns rich metadata.
        Fall back to the cross-bar HTML search page (`/f/search/res`) when no such 吧
        exists. `persist` is a no-op for symmetry; threads become `tieba_threads`
        rows only after `get_thread` enriches them.
        """
        await self._warmup()
        hits = await self._search_via_frs(keyword, pages=pages)
        if hits:
            return hits
        logger.info("贴吧：“{}吧” 无数据，改用网页搜索 /f/search/res。", keyword)
        return await self._search_via_html(keyword, pages=pages)

    async def _search_via_frs(self, keyword: str, *, pages: int) -> list[dict]:
        """Mobile JSON feed of the bar literally named `keyword`. Reliable anon path."""
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            try:
                body = await self._get_json(
                    "/mg/f/getFrsData", {"kw": keyword, "pn": page, "rn": 20})
            except ScraperError as e:
                logger.debug("贴吧 getFrsData 失败（{}），回退网页搜索。", type(e).__name__)
                return out
            if body.get("errno") not in (0, None):
                break
            data = body.get("data") or {}
            forum = data.get("forum") or {}
            if forum.get("is_exists") == 0:
                break  # no such 吧 → caller falls back to HTML keyword search
            forum_name = forum.get("name") or keyword
            threads = data.get("thread_list") or []
            if not threads:
                break
            fresh = 0
            for t in threads:
                if t.get("is_top"):
                    continue  # skip pinned 吧规/announcements — not topic content
                tid = str(t.get("tid") or t.get("id") or "")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                fresh += 1
                a = t.get("author") or {}
                out.append({
                    "tid": tid,
                    "title": t.get("title") or "",
                    "excerpt": t.get("abstract") or "",
                    "forum_name": forum_name,
                    "author_name": a.get("name") or a.get("name_show") or "",
                    "author_id": str(a.get("id") or ""),
                    "reply_num": _to_int(t.get("reply_num")),
                    "created_ts": _to_int(t.get("create_time")) or None,
                    "created_at": "",
                })
            if fresh == 0:
                break
            await asyncio.sleep(0)
        return out

    async def _search_via_html(self, keyword: str, *, pages: int) -> list[dict]:
        """Cross-bar keyword search via the HTML results page. Broader coverage, but
        Baidu anti-bots this harder than the mobile feed (often HTTP 403 on flagged IPs)."""
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            html = await self._get_html(
                "/f/search/res",
                {"ie": "utf-8", "qw": keyword, "rn": 20, "pn": page, "sm": 1},
            )
            hits = _parse_search_html(html)
            fresh = [h for h in hits if h["tid"] not in seen]
            for h in fresh:
                seen.add(h["tid"])
            if not fresh:
                if page == 1 and not out:
                    raise SoftBanned(
                        f"tieba search returned 0 threads for {keyword!r}: the 吧 feed "
                        "was empty and the HTML search page returned nothing (often a 403 "
                        "soft-ban on datacenter IPs — try from a residential network)"
                    )
                break
            out.extend(fresh)
            await asyncio.sleep(0)
        return out

    async def get_thread(
        self,
        tid: str,
        *,
        max_replies: int = 0,
        base_meta: Optional[dict] = None,
        persist: bool = True,
    ) -> int:
        """Fetch a thread's first-floor content + reply floors, persist them.

        `base_meta` is the search hit (title/excerpt/forum/author) used both to seed
        fields and as a fallback when the mobile JSON is unavailable. Returns the
        number of reply floors persisted (0 if `max_replies == 0` or replies couldn't
        be reached). The thread row itself is always persisted.
        """
        meta = base_meta or {}
        title = meta.get("title") or ""
        forum = meta.get("forum_name") or ""
        author = meta.get("author_name") or ""
        author_id = str(meta.get("author_id") or "")
        content = meta.get("excerpt") or ""
        created_at = meta.get("created_at") or ""
        created_ts: Optional[int] = meta.get("created_ts")
        reply_num = _to_int(meta.get("reply_num"))
        replies_kept = 0
        raw_thread: dict = {}

        try:
            body = await self._get_json(
                "/mg/p/getPbData",
                {"kz": tid, "pn": 1, "rn": max(5, min(max_replies or 10, 30)), "format": "json"},
            )
            if body.get("errno") not in (0, None):
                raise ParseError(f"getPbData errno={body.get('errno')}: {body.get('errmsg')}")
            data = body.get("data") or {}
            raw_thread = data
            thread = data.get("thread") or {}
            forum_obj = data.get("forum") or {}
            # getPbData's `thread` omits the title — take it from the OP post or meta.
            reply_num = _to_int(thread.get("reply_num") or thread.get("replyNum")) or reply_num
            forum = forum_obj.get("name") or forum
            post_list = data.get("post_list") or data.get("postList") or []
            if post_list:
                first = post_list[0]
                title = thread.get("title") or first.get("title") or title
                content = _content_text(first.get("content")) or content
                a0 = first.get("author") or {}
                author = a0.get("name") or a0.get("name_show") or author
                author_id = str(a0.get("id") or a0.get("portrait") or "") or author_id
                created_ts = _to_int(first.get("time")) or _to_int(thread.get("create_time")) or created_ts
                if persist and self.storage and max_replies > 0:
                    for fl in post_list[1:]:
                        if replies_kept >= max_replies:
                            break
                        pid = str(fl.get("id") or fl.get("post_id") or "")
                        if not pid:
                            continue
                        a = fl.get("author") or {}
                        await self.storage.upsert(
                            "tieba_comments",
                            {
                                "pid": pid,
                                "tid": str(tid),
                                "floor": _to_int(fl.get("floor")),
                                "author_name": a.get("name") or a.get("name_show") or "",
                                "author_id": str(a.get("id") or a.get("portrait") or ""),
                                "content": _content_text(fl.get("content")),
                                "created_at": "",
                                "created_ts": _to_int(fl.get("time")) or None,
                                "raw": fl,
                            },
                        )
                        replies_kept += 1
        except ScraperError as e:
            logger.warning(
                "贴吧：帖子 {} 的正文/楼层拉取失败（{}），仅保留搜索元数据（标题/摘要/吧名）。",
                tid, type(e).__name__,
            )

        if persist and self.storage:
            await self.storage.upsert(
                "tieba_threads",
                {
                    "tid": str(tid),
                    "title": title,
                    "author_name": author,
                    "author_id": author_id,
                    "forum_name": forum,
                    "content": content,
                    "reply_num": reply_num or replies_kept,
                    "created_at": created_at,
                    "created_ts": created_ts,
                    "url": f"https://tieba.baidu.com/p/{tid}",
                    "raw": raw_thread or meta,
                },
            )
            await self.storage.record_engagement(
                platform="tieba",
                post_id=str(tid),
                comment_count=reply_num or replies_kept,
            )
        return replies_kept

    async def scrape_keyword(
        self,
        keyword: str,
        *,
        count: int = 10,
        replies_per_thread: int = 0,
        concurrency: int = 2,
        only_new: bool = False,
        progress: Optional[Callable[[Any], None]] = None,
    ) -> dict:
        """Search-mode scrape: keyword → top-N threads (+ optional reply floors).

        Same contract as the other platforms. Returns:
          {keyword, threads, comments, requested, skipped, failed, errors, run_id}
        ('comments' counts persisted reply floors.)
        """
        def _emit(item: Any) -> None:
            if progress is None:
                return
            try:
                progress(item)
            except Exception:  # noqa: BLE001
                pass

        run_id: Optional[int] = None
        if self.storage:
            run_id = await self.storage.start_run(
                keyword=keyword,
                platform="tieba",
                requested_count=count,
                config={
                    "replies_per_thread": replies_per_thread,
                    "concurrency": concurrency,
                    "only_new": only_new,
                },
            )

        comments_total = 0
        threads_done = 0
        failed = 0
        errors: dict[str, int] = {}
        skipped = 0
        error_msg: Optional[str] = None
        try:
            _emit(f"[贴吧] 搜索关键词 “{keyword}” …")
            pages_needed = max(1, (count + 19) // 20)
            hits = await self.search(keyword, pages=pages_needed)

            # Dedupe by tid, preserve order, cap to count.
            seen_tids: set[str] = set()
            kept_hits: list[dict] = []
            for h in hits:
                if h["tid"] not in seen_tids:
                    seen_tids.add(h["tid"])
                    kept_hits.append(h)
            kept_hits = kept_hits[:count]

            if only_new and kept_hits and self.storage:
                seen = await self.storage.keyword_post_ids(keyword=keyword, platform="tieba")
                before = len(kept_hits)
                kept_hits = [h for h in kept_hits if h["tid"] not in seen]
                skipped = before - len(kept_hits)
                if skipped:
                    _emit(f"[贴吧] --only-new 跳过 {skipped} 个已抓过的帖子")

            if not kept_hits:
                if only_new and skipped > 0:
                    _emit(f"[贴吧] 没有新帖子（{skipped} 个都已抓过）")
                else:
                    logger.warning(
                        "贴吧采集：关键词 {!r} 没搜到帖子。可能是被风控或关键词太冷门 — "
                        "跑一下 `python -m scraper doctor` 看看。",
                        keyword,
                    )
                    _emit("[贴吧] 没搜到帖子（可能被风控/关键词冷门）")
                return {
                    "keyword": keyword, "threads": 0, "comments": 0,
                    "requested": count, "skipped": skipped,
                    "failed": 0, "errors": {}, "run_id": run_id,
                }

            _emit(f"[贴吧] 搜到 {len(kept_hits)} 个帖子，开始抓取（并发 {concurrency}）")
            _emit(TotalEvent(total=len(kept_hits), label=f"贴吧 · {keyword}"))

            if self.storage and run_id is not None:
                for rank, h in enumerate(kept_hits, start=1):
                    await self.storage.record_keyword_post(
                        keyword=keyword, platform="tieba", post_id=h["tid"],
                        run_id=run_id, rank=rank,
                    )

            sem = asyncio.Semaphore(concurrency)
            total = len(kept_hits)
            finished = 0

            async def _one(h: dict) -> None:
                nonlocal comments_total, threads_done, finished, failed
                async with sem:
                    try:
                        kept = await self.get_thread(
                            h["tid"], max_replies=replies_per_thread, base_meta=h,
                        )
                        threads_done += 1
                        comments_total += kept
                        finished += 1
                        _emit(f"[贴吧] {finished}/{total} ✓ {h['tid']}  楼层 {kept}")
                        _emit(StepEvent(ok=True, extra={"comments": kept}))
                    except ScraperError as e:
                        finished += 1
                        failed += 1
                        err_name = type(e).__name__
                        errors[err_name] = errors.get(err_name, 0) + 1
                        logger.warning(
                            "贴吧采集：帖子 {} 抓取失败（关键词 {!r}）：{}",
                            h["tid"], keyword, e,
                        )
                        _emit(f"[贴吧] {finished}/{total} ✗ {h['tid']}  ({err_name})")
                        _emit(StepEvent(ok=False, error=err_name))

            await asyncio.gather(*[_one(h) for h in kept_hits])
            logger.info(
                "tieba scrape: keyword={!r} threads={} replies={} failed={}",
                keyword, threads_done, comments_total, failed,
            )
            return {
                "keyword": keyword,
                "threads": threads_done,
                "comments": comments_total,
                "requested": count,
                "skipped": skipped,
                "failed": failed,
                "errors": errors,
                "run_id": run_id,
            }
        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            raise
        finally:
            if self.storage and run_id is not None:
                await self.storage.finish_run(
                    run_id,
                    posts_fetched=threads_done,
                    comments_fetched=comments_total,
                    error=error_msg,
                )


# ---- module-level parsing helpers -------------------------------------------


def _parse_search_html(html: str) -> list[dict]:
    """Parse the `/f/search/res` results page into hit dicts.

    Anchors on `/p/<tid>` links so it survives Baidu's periodic CSS-class churn:
    first tries the documented `.s_post` blocks (richer metadata), then falls back to
    scraping every thread link on the page if the block structure changed.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen: set[str] = set()

    for post in soup.select(".s_post"):
        link = post.select_one(".p_title a") or post.find("a", href=re.compile(r"/p/\d+"))
        if not link:
            continue
        m = re.search(r"/p/(\d+)", link.get("href") or "")
        if not m:
            continue
        tid = m.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        content_el = post.select_one(".p_content")
        forum_el = post.select_one("a.p_forum") or post.find("a", href=re.compile(r"/f\?kw="))
        author_el = post.select_one("a.p_author") or post.find("a", href=re.compile(r"/home/main"))
        date_el = post.select_one(".p_date")
        out.append({
            "tid": tid,
            "title": link.get_text(strip=True),
            "excerpt": content_el.get_text(" ", strip=True) if content_el else "",
            "forum_name": forum_el.get_text(strip=True) if forum_el else "",
            "author_name": author_el.get_text(strip=True) if author_el else "",
            "created_at": date_el.get_text(strip=True) if date_el else "",
        })

    # Fallback: markup changed and `.s_post` matched nothing — scrape thread links.
    if not out:
        for a in soup.find_all("a", href=re.compile(r"^/p/\d+")):
            m = re.search(r"/p/(\d+)", a.get("href") or "")
            if not m:
                continue
            tid = m.group(1)
            title = a.get_text(strip=True)
            if tid in seen or not title:
                continue
            seen.add(tid)
            out.append({
                "tid": tid, "title": title, "excerpt": "",
                "forum_name": "", "author_name": "", "created_at": "",
            })
    return out


def _content_text(content: Any) -> str:
    """Flatten Tieba's post content into plain text.

    The mobile API returns content as a list of segments ({type, text, ...}); plain
    text is the union of every `text` field. Defensive about str / unexpected shapes.
    """
    if not content:
        return ""
    if isinstance(content, str):
        return _TAG_RE.sub("", content).strip()
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict):
                t = seg.get("text")
                if t:
                    parts.append(str(t))
            elif isinstance(seg, str):
                parts.append(seg)
        # Segment text can embed inline HTML (e.g. "<br/>"); strip it for clean text.
        return _TAG_RE.sub("", " ".join(parts)).strip()
    return str(content)


def _to_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
    return 0


_TAG_RE = re.compile(r"<[^>]+>")

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Optional

from bs4 import BeautifulSoup
from loguru import logger

from scraper.core.base import BaseScraper
from scraper.core.exceptions import AuthRequired, NotFound, ParseError, ScraperError, SoftBanned
from scraper.core.progress import StepEvent, TotalEvent


@dataclass
class WeiboPost:
    id: str            # numeric status id, stringified
    mblogid: str       # base-62 bid used in weibo.com/<uid>/<bid> links
    user_id: str
    user_name: str
    text: str
    created_at: str    # raw Weibo time string
    created_ts: Optional[int]  # best-effort epoch seconds
    source: str
    reposts_count: int
    comments_count: int
    attitudes_count: int
    raw: dict


class WeiboScraper(BaseScraper):
    """Sina Weibo scraper built on the mobile site's container JSON API.

    m.weibo.cn exposes a clean JSON API that needs no request signing — unlike the
    desktop weibo.com endpoints. Keyword search works anonymously for a page or two;
    a logged-in cookie (the `SUB` token) makes search + comment-fetching far more
    reliable. We reuse the shared `HttpSession` (curl_cffi TLS impersonation, rate
    limiting, cookie persistence) unchanged.
    """

    platform = "weibo"
    base_url = "https://m.weibo.cn"
    default_headers = {
        "Referer": "https://m.weibo.cn/",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._warmed_up = False

    # ---- internal helpers ---------------------------------------------------

    async def _warmup(self) -> None:
        """GET the mobile homepage once so the cookie jar picks up anonymous tokens
        (`_T_WM` etc.). Search beyond the first page is much more reliable with these
        present. Skipped if a real login cookie (`SUB`) is already in the jar."""
        if self._warmed_up or self.session is None:
            return
        jar_names = {c.name for c in self.session.client.cookies.jar}
        if "_T_WM" not in jar_names and "SUB" not in jar_names:
            try:
                await self.session.client.get("https://m.weibo.cn/")
            except Exception as e:  # noqa: BLE001 — warmup is best-effort
                logger.debug("weibo warmup GET failed (non-fatal): {}", e)
        self._warmed_up = True

    async def _call(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        headers: Optional[dict] = None,
    ) -> dict:
        assert self.session is not None, "use WeiboScraper as an async context manager"
        url = path if path.startswith("http") else self.base_url + path
        try:
            body = await self.session.get(url, params=params, headers=headers)
        except ScraperError as e:
            msg = str(e)
            # m.weibo.cn returns 403/418/432 when it decides the request is a bot.
            if any(code in msg for code in ("403", "418", "432")):
                raise SoftBanned(f"weibo blocked the request: {msg[:120]}") from e
            raise
        if not isinstance(body, dict):
            raise ParseError(f"unexpected non-dict response from {path}")
        return body

    # ---- public endpoints ---------------------------------------------------

    async def search(
        self,
        keyword: str,
        *,
        pages: int = 1,
        persist: bool = False,
    ) -> list[WeiboPost]:
        """Keyword search via the `searchall` container. Returns parsed posts.

        `persist=True` writes each post to `weibo_posts` (used by the standalone
        `weibo search` command). `scrape_keyword` calls with `persist=False` and
        persists only the kept top-N itself (after `--only-new` filtering).
        """
        await self._warmup()
        out: list[WeiboPost] = []
        for page in range(1, pages + 1):
            # containerid encodes the search: type=1 is the "综合/all" tab.
            body = await self._call(
                "/api/container/getIndex",
                {
                    "containerid": f"100103type=1&q={keyword}",
                    "page_type": "searchall",
                    "page": page,
                },
            )
            ok = body.get("ok")
            cards = (body.get("data") or {}).get("cards") or []
            mblogs = list(_iter_mblogs(cards))
            if not mblogs:
                if page == 1:
                    # ok=-100 ("login required") is the usual anonymous outcome — Weibo's
                    # searchall now gates results behind the SUB cookie. Distinguish that
                    # from a soft-ban on an already-cookied session.
                    if ok != 1 and not self._cookie:
                        raise AuthRequired(
                            f"微博搜索需要登录 cookie（ok={ok}）。在 config.yaml 的 weibo.cookie "
                            "粘贴含 SUB 的 cookie，或运行 `python -m scraper login weibo`。"
                        )
                    raise SoftBanned(
                        f"weibo search returned 0 posts for {keyword!r} (ok={ok}); "
                        "cookie 可能失效或被风控"
                    )
                break
            for mb in mblogs:
                post = _parse_mblog(mb)
                if not post.id:
                    continue
                out.append(post)
                if persist:
                    await self._persist_post(post)
            await asyncio.sleep(0)
        return out

    async def _persist_post(self, post: WeiboPost) -> None:
        """Upsert one post + append an engagement snapshot. No-op without storage."""
        if not (self.storage and post.id):
            return
        await self.storage.upsert("weibo_posts", _post_row(post))
        # Weibo has no view count for normal posts — map likes/comments/reposts.
        await self.storage.record_engagement(
            platform="weibo",
            post_id=post.id,
            like_count=post.attitudes_count,
            comment_count=post.comments_count,
            share_count=post.reposts_count,
        )

    async def _maybe_long_text(self, mb: dict) -> str:
        """Return full text for an `isLongText` post via /statuses/extend, else the
        (possibly truncated) search text. Best-effort — falls back on any error."""
        if not mb.get("isLongText"):
            return _strip_html(mb.get("text") or "")
        try:
            body = await self._call("/statuses/extend", {"id": mb.get("id")})
            long_html = ((body.get("data") or {}).get("longTextContent")) or ""
            if long_html:
                return _strip_html(long_html)
        except ScraperError as e:
            logger.debug("weibo long-text fetch failed for {}: {}", mb.get("id"), e)
        return _strip_html(mb.get("text") or "")

    async def get_comments(
        self,
        post_id: str,
        *,
        max_count: int = 20,
        persist: bool = True,
    ) -> int:
        """Fetch hot-flow comments for a post. Returns the number persisted.

        Best-effort: Weibo often gates comments behind a login cookie and returns
        ok=0 / 403 anonymously. On any block we stop and return what we got rather
        than failing the whole scrape (the post's comment *count* is already stored).
        """
        kept = 0
        max_id = 0
        max_id_type = 0
        headers = {"Referer": f"https://m.weibo.cn/detail/{post_id}"}
        while kept < max_count:
            params: dict[str, Any] = {
                "id": post_id,
                "mid": post_id,
                "max_id_type": max_id_type,
            }
            if max_id:
                params["max_id"] = max_id
            try:
                body = await self._call("/comments/hotflow", params, headers=headers)
            except SoftBanned:
                break
            if body.get("ok") == 0:
                break
            data = body.get("data") or {}
            comments = data.get("data") or []
            if not comments:
                break
            for c in comments:
                if kept >= max_count:
                    break
                user = c.get("user") or {}
                row = {
                    "comment_id": str(c.get("id") or ""),
                    "post_id": str(post_id),
                    "user_id": str(user.get("id") or ""),
                    "user_name": user.get("screen_name") or "",
                    "text": _strip_html(c.get("text") or ""),
                    "like_count": c.get("like_count") or 0,
                    "created_at": c.get("created_at") or "",
                    "created_ts": _parse_weibo_time(c.get("created_at") or ""),
                    "raw": c,
                }
                if persist and self.storage and row["comment_id"]:
                    await self.storage.upsert("weibo_comments", row)
                    kept += 1
            max_id = data.get("max_id") or 0
            max_id_type = data.get("max_id_type") or 0
            if not max_id:
                break
            await asyncio.sleep(0)
        return kept

    async def get_status(
        self,
        id_or_bid: str,
        *,
        persist: bool = True,
        with_comments: int = 0,
    ) -> tuple[WeiboPost, int]:
        """Fetch a single post by numeric id or base-62 bid (used by `grab`).

        Returns (post, comments_persisted). `/statuses/show` accepts either id form
        and returns the full (non-truncated) mblog, so no long-text round-trip is
        usually needed — but we still resolve isLongText defensively.
        """
        await self._warmup()
        body = await self._call("/statuses/show", {"id": id_or_bid})
        if body.get("ok") == 0:
            raise NotFound(f"weibo status {id_or_bid} not found or blocked")
        mb = body.get("data") or {}
        post = _parse_mblog(mb)
        if not post.id:
            raise NotFound(f"weibo status {id_or_bid} returned no usable payload")
        if post.raw.get("isLongText"):
            post.text = await self._maybe_long_text(post.raw)
        if persist:
            await self._persist_post(post)
        n = 0
        if with_comments > 0:
            n = await self.get_comments(post.id, max_count=with_comments)
        return post, n

    async def scrape_keyword(
        self,
        keyword: str,
        *,
        count: int = 10,
        comments_count: int = 0,
        concurrency: int = 4,
        only_new: bool = False,
        progress: Optional[Callable[[Any], None]] = None,
    ) -> dict:
        """Search-mode scrape: keyword → top-N posts (+ optional comments).

        Mirrors the Bilibili scraper's contract: emits `TotalEvent`/`StepEvent`,
        opens a `scrape_runs` row, records `keyword_posts` linkage up front, supports
        `--only-new`, and returns a summary dict:
          {keyword, posts, comments, requested, skipped, failed, errors, run_id}
        """
        def _emit(item: Any) -> None:
            if progress is None:
                return
            try:
                progress(item)
            except Exception:  # noqa: BLE001 — never let a bad UI callback break a scrape
                pass

        run_id: Optional[int] = None
        if self.storage:
            run_id = await self.storage.start_run(
                keyword=keyword,
                platform="weibo",
                requested_count=count,
                config={
                    "comments_count": comments_count,
                    "concurrency": concurrency,
                    "only_new": only_new,
                },
            )

        comments_total = 0
        posts_done = 0
        failed = 0
        errors: dict[str, int] = {}
        skipped = 0
        error_msg: Optional[str] = None
        try:
            _emit(f"[微博] 搜索关键词 “{keyword}” …")
            # ~10 posts per searchall page.
            pages_needed = max(1, (count + 9) // 10)
            found = await self.search(keyword, pages=pages_needed, persist=False)

            # Dedupe by id, preserving search order, then cap to `count`.
            seen_ids: set[str] = set()
            posts: list[WeiboPost] = []
            for p in found:
                if p.id and p.id not in seen_ids:
                    seen_ids.add(p.id)
                    posts.append(p)
            posts = posts[:count]

            if only_new and posts and self.storage:
                seen = await self.storage.keyword_post_ids(keyword=keyword, platform="weibo")
                before = len(posts)
                posts = [p for p in posts if p.id not in seen]
                skipped = before - len(posts)
                if skipped:
                    _emit(f"[微博] --only-new 跳过 {skipped} 条已抓过的微博")

            if not posts:
                if only_new and skipped > 0:
                    _emit(f"[微博] 没有新微博（{skipped} 条都已抓过）")
                else:
                    logger.warning(
                        "微博采集：关键词 {!r} 没搜到可用微博。可能是被风控、需要登录 cookie、"
                        "或关键词太冷门 — 跑一下 `python -m scraper doctor` 看看。",
                        keyword,
                    )
                    _emit("[微博] 没搜到可用微博（可能被风控/需登录/关键词冷门）")
                return {
                    "keyword": keyword, "posts": 0, "comments": 0,
                    "requested": count, "skipped": skipped,
                    "failed": 0, "errors": {}, "run_id": run_id,
                }

            _emit(f"[微博] 搜到 {len(posts)} 条微博，开始抓取（并发 {concurrency}）")
            _emit(TotalEvent(total=len(posts), label=f"微博 · {keyword}"))

            if self.storage and run_id is not None:
                for rank, p in enumerate(posts, start=1):
                    await self.storage.record_keyword_post(
                        keyword=keyword, platform="weibo", post_id=p.id,
                        run_id=run_id, rank=rank,
                    )

            sem = asyncio.Semaphore(concurrency)
            total = len(posts)
            finished = 0  # asyncio single-thread — no lock needed

            async def _one(p: WeiboPost) -> None:
                nonlocal comments_total, posts_done, finished, failed
                async with sem:
                    try:
                        # Replace truncated search text with full content when long.
                        if p.raw.get("isLongText"):
                            p.text = await self._maybe_long_text(p.raw)
                        await self._persist_post(p)
                        posts_done += 1
                        n = 0
                        if comments_count > 0:
                            n = await self.get_comments(p.id, max_count=comments_count)
                            comments_total += n
                        finished += 1
                        _emit(f"[微博] {finished}/{total} ✓ {p.id}  评论 {n}")
                        _emit(StepEvent(ok=True, extra={"comments": n}))
                    except ScraperError as e:
                        finished += 1
                        failed += 1
                        err_name = type(e).__name__
                        errors[err_name] = errors.get(err_name, 0) + 1
                        logger.warning(
                            "微博采集：微博 {} 抓取失败（关键词 {!r}）：{}",
                            p.id, keyword, e,
                        )
                        _emit(f"[微博] {finished}/{total} ✗ {p.id}  ({err_name})")
                        _emit(StepEvent(ok=False, error=err_name))

            await asyncio.gather(*[_one(p) for p in posts])
            logger.info(
                "weibo scrape: keyword={!r} posts={} comments={} failed={}",
                keyword, posts_done, comments_total, failed,
            )
            return {
                "keyword": keyword,
                "posts": posts_done,
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
                    posts_fetched=posts_done,
                    comments_fetched=comments_total,
                    error=error_msg,
                )


# ---- module-level parsing helpers -------------------------------------------


def _iter_mblogs(cards: list[dict]) -> Iterator[dict]:
    """Yield the `mblog` payload from search cards.

    searchall mixes flat post cards (`card_type == 9`) with grouped cards
    (`card_group` holding more type-9 cards). We pull mblogs from both.
    """
    for card in cards:
        if card.get("card_type") == 9 and card.get("mblog"):
            yield card["mblog"]
        for sub in card.get("card_group") or []:
            if sub.get("card_type") == 9 and sub.get("mblog"):
                yield sub["mblog"]


def _parse_mblog(mb: dict) -> WeiboPost:
    user = mb.get("user") or {}
    return WeiboPost(
        id=str(mb.get("id") or mb.get("mid") or ""),
        mblogid=mb.get("bid") or "",
        user_id=str(user.get("id") or ""),
        user_name=user.get("screen_name") or "",
        text=_strip_html(mb.get("text") or ""),
        created_at=mb.get("created_at") or "",
        created_ts=_parse_weibo_time(mb.get("created_at") or ""),
        source=mb.get("source") or "",
        reposts_count=_to_int(mb.get("reposts_count")),
        comments_count=_to_int(mb.get("comments_count")),
        attitudes_count=_to_int(mb.get("attitudes_count")),
        raw=mb,
    )


def _post_row(post: WeiboPost) -> dict:
    return {
        "id": post.id,
        "mblogid": post.mblogid,
        "user_id": post.user_id,
        "user_name": post.user_name,
        "text": post.text,
        "created_at": post.created_at,
        "created_ts": post.created_ts,
        "source": post.source,
        "reposts_count": post.reposts_count,
        "comments_count": post.comments_count,
        "attitudes_count": post.attitudes_count,
        "raw": post.raw,
    }


def _to_int(v: Any) -> int:
    """Coerce Weibo's sometimes-string counts (e.g. '1.2万' is pre-resolved on the
    mobile API, but be defensive) to an int. Returns 0 on anything non-numeric."""
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


_WEIBO_REL_MIN = re.compile(r"(\d+)\s*分钟前")
_WEIBO_REL_HR = re.compile(r"(\d+)\s*小时前")


def _parse_weibo_time(s: str) -> Optional[int]:
    """Best-effort parse of Weibo's many created_at formats into epoch seconds.

    Handles: '刚刚', 'N分钟前', 'N小时前', '昨天 HH:MM', 'MM-DD', 'MM-DD HH:MM',
    'YYYY-MM-DD[ HH:MM[:SS]]', and the full 'Sat Sep 09 16:30:00 +0800 2023' form
    some endpoints return. Returns None when nothing matches — the raw string is
    always kept in `created_at` so no information is lost.
    """
    if not s:
        return None
    s = s.strip()
    now = time.time()
    try:
        if s == "刚刚":
            return int(now)
        m = _WEIBO_REL_MIN.search(s)
        if m:
            return int(now - int(m.group(1)) * 60)
        m = _WEIBO_REL_HR.search(s)
        if m:
            return int(now - int(m.group(1)) * 3600)
        if s.startswith("昨天"):
            dt = datetime.now() - timedelta(days=1)
            hhmm = s.replace("昨天", "").strip()
            if hhmm:
                try:
                    t = datetime.strptime(hhmm, "%H:%M").time()
                    dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                except ValueError:
                    pass
            return int(dt.timestamp())
        # Full form first (unambiguous): "Sat Sep 09 16:30:00 +0800 2023"
        try:
            return int(datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").timestamp())
        except ValueError:
            pass
        # Year-qualified forms.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(s, fmt).timestamp())
            except ValueError:
                continue
        # Bare month-day forms — assume current year.
        for fmt in ("%m-%d %H:%M", "%m-%d"):
            try:
                dt = datetime.strptime(s, fmt).replace(year=datetime.now().year)
                return int(dt.timestamp())
            except ValueError:
                continue
    except Exception:  # noqa: BLE001 — time parsing is best-effort
        return None
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    if "<" not in html:
        return html
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(" ", strip=True)

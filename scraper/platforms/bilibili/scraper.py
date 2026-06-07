from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional

from loguru import logger

from scraper.core.base import BaseScraper
from scraper.core.exceptions import (
    AuthRequired,
    CookieExpired,
    NotFound,
    ParseError,
    ScraperError,
    SoftBanned,
)
from scraper.core.progress import StepEvent, TotalEvent
from scraper.platforms.bilibili.wbi import extract_keys_from_nav, sign_params


@dataclass
class BiliVideo:
    bvid: str
    aid: int
    title: str
    desc: str
    owner: dict
    pubdate: int
    duration: int
    stat: dict
    raw: dict


class BilibiliScraper(BaseScraper):
    platform = "bilibili"
    base_url = "https://api.bilibili.com"
    default_headers = {
        "Referer": "https://www.bilibili.com",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._wbi_keys: Optional[tuple[str, str]] = None
        self._wbi_keys_at: float = 0.0
        self._warmed_up: bool = False

    # ---- internal helpers ---------------------------------------------------

    async def _ensure_wbi_keys(self) -> tuple[str, str]:
        # Cache for ~30 minutes; the keys rotate but not aggressively.
        if self._wbi_keys and time.time() - self._wbi_keys_at < 1800:
            return self._wbi_keys
        data = await self._call("/x/web-interface/nav", expect_code_zero=False)
        keys = extract_keys_from_nav(data.get("data", {}))
        self._wbi_keys = keys
        self._wbi_keys_at = time.time()
        return keys

    async def _warmup(self) -> None:
        """Visit bilibili.com once so the cookie jar picks up `buvid3` / `b_nut`.

        The dynamic-feed and a few other newer endpoints return HTTP 412 (anti-bot)
        when called without these anonymous browser cookies, even if the request is
        WBI-signed. Cheap one-shot, then cached for the session lifetime.

        Skipped if cookie-jar persistence already loaded buvid3 from a prior run —
        the persisted value is just as valid and looks more like a returning user
        than re-fetching a fresh one every time.
        """
        if self._warmed_up or self.session is None:
            return
        jar_names = {c.name for c in self.session.client.cookies.jar}
        if "buvid3" not in jar_names:
            await self.session.client.get("https://www.bilibili.com/")
        self._warmed_up = True

    async def _call(
        self,
        path: str,
        params: Optional[dict] = None,
        *,
        sign: bool = False,
        expect_code_zero: bool = True,
        headers: Optional[dict] = None,
    ) -> dict:
        assert self.session is not None, "use BilibiliScraper as an async context manager"
        params = dict(params or {})
        if sign:
            img_key, sub_key = await self._ensure_wbi_keys()
            params = sign_params(params, img_key, sub_key)
        url = self.base_url + path
        body = await self.session.get(url, params=params, headers=headers)
        if not isinstance(body, dict):
            raise ParseError(f"unexpected non-dict response from {path}")
        if expect_code_zero:
            code = body.get("code")
            if code == -404 or code == -400:
                raise NotFound(body.get("message", "not found"))
            if code == -101:
                # -101: "账号未登录" — set SESSDATA expired or never present. Treat as
                # stale cookie if the user supplied one, plain AuthRequired otherwise.
                msg = body.get("message", "login required")
                if self._cookie:
                    raise CookieExpired(f"bilibili SESSDATA looks expired: {msg}")
                raise AuthRequired(msg)
            if code == -111:
                raise AuthRequired(body.get("message", "csrf required"))
            if code == -352:
                # -352: anti-bot ("风控校验失败"). Already a soft block.
                raise SoftBanned(f"bilibili anti-bot (-352): {body.get('message')}")
            if code != 0:
                raise ScraperError(f"bilibili api error {code}: {body.get('message')}")
        return body

    # ---- public endpoints ---------------------------------------------------

    async def get_video(
        self,
        bvid: str,
        *,
        persist: bool = True,
        with_tags: bool = False,
    ) -> BiliVideo:
        """Fetch core metadata for a video by BV id.

        If `with_tags=True`, makes an extra request to /x/tag/archive/tags to fetch
        the full tag list and stores it as a JSON array in the `tags` column.
        Default is False because it's an additional API hit per video.
        """
        body = await self._call("/x/web-interface/view", {"bvid": bvid})
        d = body["data"]
        video = BiliVideo(
            bvid=d["bvid"],
            aid=d["aid"],
            title=d["title"],
            desc=d.get("desc", ""),
            owner=d.get("owner", {}),
            pubdate=d.get("pubdate", 0),
            duration=d.get("duration", 0),
            stat=d.get("stat", {}),
            raw=d,
        )
        tag_names: Optional[list[str]] = None
        if with_tags:
            try:
                tag_names = await self.get_video_tags(video.aid)
            except ScraperError as e:
                logger.warning("B站标签：视频 {} 拉取失败：{}（不影响视频本身的数据）", bvid, e)
        if persist and self.storage:
            await self.storage.upsert(
                "bili_videos",
                {
                    "bvid": video.bvid,
                    "aid": video.aid,
                    "title": video.title,
                    "desc": video.desc,
                    "owner_mid": video.owner.get("mid"),
                    "owner_name": video.owner.get("name"),
                    "pubdate": video.pubdate,
                    "duration": video.duration,
                    "view": video.stat.get("view"),
                    "like": video.stat.get("like"),
                    "coin": video.stat.get("coin"),
                    "favorite": video.stat.get("favorite"),
                    "reply": video.stat.get("reply"),
                    "share": video.stat.get("share"),
                    "danmaku": video.stat.get("danmaku"),
                    "tags": tag_names if tag_names is not None else None,
                    "raw": video.raw,
                },
            )
            # Time-series snapshot — append-only, lets you compute growth deltas
            # across re-scrapes without losing history.
            await self.storage.record_engagement(
                platform="bili",
                post_id=video.bvid,
                view_count=video.stat.get("view"),
                like_count=video.stat.get("like"),
                comment_count=video.stat.get("reply"),
                favorite_count=video.stat.get("favorite"),
                share_count=video.stat.get("share"),
            )
        return video

    async def get_video_tags(self, aid: int) -> list[str]:
        """Fetch the tag list for a video by aid. Returns just the tag names."""
        body = await self._call("/x/tag/archive/tags", {"aid": aid})
        items = body.get("data") or []
        return [t.get("tag_name") for t in items if t.get("tag_name")]

    async def iter_comments(
        self,
        bvid: str,
        *,
        pages: int = 3,
        persist: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield top-level comments for a video, page by page."""
        video = await self.get_video(bvid, persist=persist)
        oid = video.aid
        for page in range(1, pages + 1):
            body = await self._call(
                "/x/v2/reply",
                # ps capped at 20 — bilibili rejects >20 with "ps out of bounds"
                {"oid": oid, "type": 1, "pn": page, "ps": 20, "sort": 2},
            )
            replies = (body.get("data") or {}).get("replies") or []
            if not replies:
                logger.info("bili comments: no more replies at page {}", page)
                break
            for r in replies:
                row = {
                    "rpid": r["rpid"],
                    "oid": oid,
                    "parent": r.get("parent", 0),
                    "mid": r.get("mid"),
                    "uname": (r.get("member") or {}).get("uname"),
                    "message": (r.get("content") or {}).get("message"),
                    "ctime": r.get("ctime"),
                    "likes": r.get("like"),
                    "raw": r,
                }
                if persist and self.storage:
                    await self.storage.upsert("bili_comments", row)
                yield row
            await asyncio.sleep(0)

    async def get_user_videos(
        self,
        mid: int,
        *,
        pages: int = 1,
        page_size: int = 30,
    ) -> list[dict]:
        """List videos uploaded by a user (mid). Uses the WBI-signed endpoint."""
        out: list[dict] = []
        for page in range(1, pages + 1):
            body = await self._call(
                "/x/space/wbi/arc/search",
                {
                    "mid": mid,
                    "ps": page_size,
                    "pn": page,
                    "order": "pubdate",
                    "platform": "web",
                    "web_location": 1550101,
                },
                sign=True,
            )
            vlist = (((body.get("data") or {}).get("list") or {}).get("vlist")) or []
            if not vlist:
                break
            out.extend(vlist)
        return out

    async def iter_user_dynamics(
        self,
        mid: int,
        *,
        pages: int = 1,
        persist: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield a user's dynamic-feed (动态) items, paginated by offset cursor.

        Covers all dynamic types — video uploads (AV), images (DRAW), text (WORD),
        forwards, articles. Each item is normalized into one row and the full
        payload is preserved in `raw` so type-specific fields can be re-derived later.
        """
        # 412 anti-bot fires without anonymous browser cookies + space-page Referer
        # + WBI signing. Warm up the cookie jar before the first request.
        await self._warmup()
        offset = ""
        space_referer = f"https://space.bilibili.com/{mid}/dynamic"
        for page in range(1, pages + 1):
            params: dict[str, Any] = {
                "host_mid": mid,
                "timezone_offset": -480,
                "platform": "web",
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
            }
            if offset:
                params["offset"] = offset
            body = await self._call(
                "/x/polymer/web-dynamic/v1/feed/space",
                params,
                sign=True,
                headers={"Referer": space_referer},
            )
            data = body.get("data") or {}
            items = data.get("items") or []
            if not items:
                logger.info("bili dynamics: no items at page {}", page)
                break
            for it in items:
                row = _normalize_dynamic(it)
                if persist and self.storage and row["dynamic_id"]:
                    await self.storage.upsert("bili_dynamics", row)
                yield row
            if not data.get("has_more"):
                logger.info("bili dynamics: reached end at page {}", page)
                break
            offset = data.get("offset") or ""
            if not offset:
                break
            await asyncio.sleep(0)

    async def search(
        self,
        keyword: str,
        *,
        pages: int = 1,
        order: str = "totalrank",
        persist: bool = True,
    ) -> list[dict]:
        """General search across all categories."""
        out: list[dict] = []
        rank = 0
        for page in range(1, pages + 1):
            body = await self._call(
                "/x/web-interface/wbi/search/all/v2",
                {
                    "keyword": keyword,
                    "page": page,
                    "page_size": 20,
                    "order": order,
                    "platform": "pc",
                },
                sign=True,
            )
            results = (body.get("data") or {}).get("result") or []
            videos: list[dict] = []
            for group in results:
                if group.get("result_type") == "video":
                    videos = group.get("data") or []
                    break
            if not videos:
                # Empty-but-success on page 1 of a non-trivial keyword is the
                # canonical Bilibili soft-ban signal — they return code 0 with an
                # empty result array instead of -352. Raise so callers can react.
                if page == 1:
                    raise SoftBanned(
                        f"bilibili search returned 0 video results for {keyword!r}; "
                        "this is usually a soft ban, not a genuine miss"
                    )
                break
            for v in videos:
                rank += 1
                row = {
                    "keyword": keyword,
                    "bvid": v.get("bvid"),
                    "title": (v.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                    "author": v.get("author"),
                    "play": v.get("play"),
                    "rank": rank,
                    "raw": v,
                }
                out.append(row)
                if persist and self.storage and row["bvid"]:
                    await self.storage.upsert("bili_search", row)
        return out


    async def scrape_keyword(
        self,
        keyword: str,
        *,
        count: int = 10,
        comments_pages: int = 1,
        concurrency: int = 4,
        with_tags: bool = True,
        only_new: bool = False,
        progress: Optional[Callable[[Any], None]] = None,
    ) -> dict:
        """MediaCrawler-style search mode: keyword → top-N videos with full details.

        `progress`, if given, is called per milestone with either a `str` (free-form
        status line — back-compat), a `TotalEvent(total=N)` once after search resolves,
        or a `StepEvent(ok=bool, error=str)` per item. Pass either; legacy string-only
        consumers still work.

        `only_new=True` filters out post_ids already recorded under this keyword in
        `keyword_posts`. Lets daily re-runs only pay quota for genuinely new posts.

        Returns a summary dict with failure breakdown:
          {keyword, videos, comments, requested, skipped, failed, errors, run_id}
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
                platform="bili",
                requested_count=count,
                config={
                    "comments_pages": comments_pages,
                    "concurrency": concurrency,
                    "with_tags": with_tags,
                    "only_new": only_new,
                },
            )

        comments_total = 0
        videos_done = 0
        failed = 0
        errors: dict[str, int] = {}
        skipped = 0
        error_msg: Optional[str] = None
        try:
            _emit(f"[B站] 搜索关键词 “{keyword}” …")
            pages_needed = max(1, (count + 19) // 20)
            hits = await self.search(keyword, pages=pages_needed)
            bvids = [h["bvid"] for h in hits if h.get("bvid")][:count]

            if only_new and bvids and self.storage:
                seen = await self.storage.keyword_post_ids(keyword=keyword, platform="bili")
                before = len(bvids)
                bvids = [b for b in bvids if b not in seen]
                skipped = before - len(bvids)
                if skipped:
                    _emit(f"[B站] --only-new 跳过 {skipped} 个已抓过的视频")

            if not bvids:
                if only_new and skipped > 0:
                    _emit(f"[B站] 没有新视频（{skipped} 个都已抓过）")
                else:
                    logger.warning(
                        "B站采集：关键词 {!r} 没搜到可用视频。可能是被风控、关键词太冷门 — "
                        "跑一下 `python -m scraper doctor` 看看。",
                        keyword,
                    )
                    _emit(f"[B站] 没搜到可用视频（可能被风控/关键词冷门）")
                return {
                    "keyword": keyword, "videos": 0, "comments": 0,
                    "requested": count, "skipped": skipped,
                    "failed": 0, "errors": {}, "run_id": run_id,
                }
            _emit(f"[B站] 搜到 {len(bvids)} 个视频，开始抓取（并发 {concurrency}）")

            _emit(TotalEvent(total=len(bvids), label=f"B站 · {keyword}"))

            # Record the keyword→post linkage up front so the run row reflects
            # intent even if the per-video fetches fail.
            if self.storage and run_id is not None:
                for rank, bvid in enumerate(bvids, start=1):
                    await self.storage.record_keyword_post(
                        keyword=keyword, platform="bili", post_id=bvid,
                        run_id=run_id, rank=rank,
                    )

            sem = asyncio.Semaphore(concurrency)
            total = len(bvids)
            finished = 0  # increments under asyncio single-thread — no lock needed

            async def _one(bvid: str) -> None:
                nonlocal comments_total, videos_done, finished, failed
                async with sem:
                    try:
                        await self.get_video(bvid, with_tags=with_tags)
                        videos_done += 1
                        n = 0
                        if comments_pages > 0:
                            async for _ in self.iter_comments(bvid, pages=comments_pages):
                                n += 1
                            comments_total += n
                        finished += 1
                        _emit(f"[B站] 视频 {finished}/{total} ✓ {bvid}  评论 {n}")
                        _emit(StepEvent(ok=True, extra={"comments": n}))
                    except ScraperError as e:
                        finished += 1
                        failed += 1
                        err_name = type(e).__name__
                        errors[err_name] = errors.get(err_name, 0) + 1
                        logger.warning(
                            "B站采集：视频 {} 抓取失败（关键词 {!r}）：{}",
                            bvid, keyword, e,
                        )
                        _emit(f"[B站] 视频 {finished}/{total} ✗ {bvid}  ({err_name})")
                        _emit(StepEvent(ok=False, error=err_name))

            await asyncio.gather(*[_one(b) for b in bvids])
            logger.info(
                "bili scrape: keyword={!r} videos={} comments={} failed={}",
                keyword, videos_done, comments_total, failed,
            )
            return {
                "keyword": keyword,
                "videos": videos_done,
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
                    posts_fetched=videos_done,
                    comments_fetched=comments_total,
                    error=error_msg,
                )


def _normalize_dynamic(item: dict) -> dict:
    """Flatten a dynamic-feed item into a stable row shape.

    The web-dynamic API nests data under modules.{module_author,module_dynamic,module_stat}
    and emits a different shape per dynamic type. We extract the common fields and
    pull `bvid` out for AV-type dynamics so it can be joined to bili_videos.
    """
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    stat = modules.get("module_stat") or {}

    desc = (dynamic.get("desc") or {}).get("text") or ""
    major = dynamic.get("major") or {}
    bvid = None
    if major.get("type") == "MAJOR_TYPE_ARCHIVE":
        archive = major.get("archive") or {}
        bvid = archive.get("bvid")
        if not desc:
            desc = archive.get("title") or ""

    return {
        "dynamic_id": item.get("id_str") or str(item.get("id") or ""),
        "mid": author.get("mid"),
        "uname": author.get("name"),
        "type": item.get("type"),
        "text": desc,
        "pub_ts": author.get("pub_ts"),
        "like_count": (stat.get("like") or {}).get("count"),
        "comment_count": (stat.get("comment") or {}).get("count"),
        "forward_count": (stat.get("forward") or {}).get("count"),
        "bvid": bvid,
        "raw": item,
    }

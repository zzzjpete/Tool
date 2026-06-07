from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Callable, Optional

from bs4 import BeautifulSoup
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


_INITIAL_DATA_RE = re.compile(
    r'<script[^>]*id="js-initialData"[^>]*>(.+?)</script>', re.DOTALL
)

# Zhihu error codes worth distinguishing. The boundary between "your cookie is dead"
# and "soft-ban / signing missing" is fuzzy on Zhihu's side; we err toward CookieExpired
# only for codes that are unambiguously about login state.
_ZHIHU_LOGIN_EXPIRED_CODES = {100, 401001, 100200000}  # "未登录" / "登录已失效"
_ZHIHU_SOFT_BAN_CODES = {10003, 40362, 1003}  # "请求来源异常" / signing missing / soft block


_ANSWER_INCLUDE = (
    "data[*].is_normal,suggest_edit,comment_count,collapsed_counts,"
    "reviewing_comments_count,can_comment,content,voteup_count,reshipment_settings,"
    "comment_permission,created_time,updated_time,review_info,"
    "question,excerpt,relationship.is_authorized,is_author,voting,is_thanked,is_nothelp,"
    "is_labeled,is_recognized;"
    "data[*].mark_infos[*].url;data[*].author.follower_count,badge[*].topics"
)


class ZhihuScraper(BaseScraper):
    platform = "zhihu"
    base_url = "https://www.zhihu.com"
    default_headers = {
        "Referer": "https://www.zhihu.com/",
        "Origin": "https://www.zhihu.com",
        "x-requested-with": "fetch",
    }

    # ---- internal helpers ---------------------------------------------------

    def _require_cookie(self) -> None:
        if not self._cookie:
            raise AuthRequired(
                "Zhihu requires a logged-in cookie. Paste yours into config.yaml under zhihu.cookie"
            )

    async def _get_json(self, path: str, params: Optional[dict] = None) -> dict:
        assert self.session is not None, "use ZhihuScraper as an async context manager"
        url = path if path.startswith("http") else self.base_url + path
        try:
            data = await self.session.get(url, params=params)
        except ScraperError as e:
            msg = str(e)
            # Zhihu embeds its real status in a JSON body even on 4xx — pull it out
            # so we can distinguish "your cookie is dead" from "this endpoint needs
            # signing" from "rate limited". See zhihu_xzse96_workaround.md for the
            # x-zse-96 endpoints; those return 403 / code 10003 regardless of cookie.
            zhihu_code: Optional[int] = None
            zhihu_msg = ""
            try:
                body = json.loads(msg.split(":", 2)[-1].strip())
                zhihu_code = (body.get("error") or {}).get("code")
                zhihu_msg = (body.get("error") or {}).get("message") or ""
            except Exception:
                pass
            if zhihu_code in _ZHIHU_LOGIN_EXPIRED_CODES:
                raise CookieExpired(
                    f"zhihu cookie expired (code {zhihu_code}): {zhihu_msg}"
                ) from e
            if zhihu_code in _ZHIHU_SOFT_BAN_CODES:
                raise SoftBanned(
                    f"zhihu soft-ban or signing required (code {zhihu_code}): {zhihu_msg}"
                ) from e
            if "401" in msg or "403" in msg:
                raise AuthRequired("zhihu auth failed; refresh cookie") from e
            if "404" in msg:
                raise NotFound(msg) from e
            raise
        if not isinstance(data, dict):
            raise ParseError(f"unexpected non-dict from {path}")
        return data

    # ---- public endpoints ---------------------------------------------------

    async def get_question(self, question_id: int, *, persist: bool = True) -> dict:
        self._require_cookie()
        params = {
            "include": "answer_count,follower_count,visit_count,excerpt,detail,topics",
        }
        data = await self._get_json(f"/api/v4/questions/{question_id}", params)
        if persist and self.storage:
            topics = _extract_topic_names(data.get("topics"))
            await self.storage.upsert(
                "zhihu_questions",
                {
                    "id": data.get("id") or question_id,
                    "title": data.get("title"),
                    "detail": _strip_html(data.get("detail") or ""),
                    "answer_count": data.get("answer_count"),
                    "follower_count": data.get("follower_count"),
                    "view_count": data.get("visit_count"),
                    "created": data.get("created"),
                    "topics": topics if topics else None,
                    "raw": data,
                },
            )
            await self.storage.record_engagement(
                platform="zhihu_question",
                post_id=str(data.get("id") or question_id),
                view_count=data.get("visit_count"),
                like_count=None,
                comment_count=data.get("answer_count"),
                favorite_count=data.get("follower_count"),
                share_count=None,
            )
        return data

    async def iter_answers(
        self,
        question_id: int,
        *,
        limit: Optional[int] = None,
        page_size: int = 5,
        persist: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield answers for a question. Stops at `limit` if given, else exhausts."""
        self._require_cookie()
        offset = 0
        seen = 0
        url = f"{self.base_url}/api/v4/questions/{question_id}/answers"
        while True:
            params = {
                "include": _ANSWER_INCLUDE,
                "limit": page_size,
                "offset": offset,
                "platform": "desktop",
                "sort_by": "default",
            }
            body = await self._get_json(url, params)
            items = body.get("data") or []
            if not items:
                logger.info("zhihu answers: page exhausted at offset {}", offset)
                break
            for ans in items:
                row = {
                    "id": ans.get("id"),
                    "question_id": question_id,
                    "author_id": ((ans.get("author") or {}).get("id")),
                    "author_name": ((ans.get("author") or {}).get("name")),
                    "voteup_count": ans.get("voteup_count"),
                    "comment_count": ans.get("comment_count"),
                    "created_time": ans.get("created_time"),
                    "updated_time": ans.get("updated_time"),
                    "content": _strip_html(ans.get("content") or ""),
                    "raw": ans,
                }
                if persist and self.storage and row["id"]:
                    await self.storage.upsert("zhihu_answers", row)
                yield row
                seen += 1
                if limit and seen >= limit:
                    return
            paging = body.get("paging") or {}
            if paging.get("is_end"):
                break
            offset += page_size
            await asyncio.sleep(0)

    async def get_user(self, url_token: str) -> dict:
        """Fetch a user's public profile by their url_token (the slug in /people/<token>)."""
        self._require_cookie()
        params = {
            "include": (
                "allow_message,is_followed,is_following,is_org,is_blocking,"
                "employments,answer_count,follower_count,articles_count,gender,badge[?(type=best_answerer)].topics"
            )
        }
        return await self._get_json(f"/api/v4/members/{url_token}", params)

    async def search(
        self,
        keyword: str,
        *,
        pages: int = 1,
        page_size: int = 20,
        persist: bool = True,
    ) -> list[dict]:
        """General search. Returns flattened result list (questions, answers, users, etc.)."""
        self._require_cookie()
        out: list[dict] = []
        for page in range(pages):
            params = {
                "t": "general",
                "q": keyword,
                "correction": 1,
                "offset": page * page_size,
                "limit": page_size,
                "search_hash_id": "",
                "show_all_topics": 0,
            }
            body = await self._get_json("/api/v4/search_v3", params)
            items = body.get("data") or []
            if not items:
                # Same soft-ban heuristic as bili — Zhihu returns an empty data array
                # instead of an error when they decide your cookie is suspicious.
                if page == 0:
                    raise SoftBanned(
                        f"zhihu search returned 0 results for {keyword!r}; "
                        "usually a soft ban, not a genuine miss"
                    )
                break
            for it in items:
                obj = it.get("object") or {}
                kind = obj.get("type") or it.get("type") or "unknown"
                target_id = str(obj.get("id") or it.get("id") or "")
                title = obj.get("title") or obj.get("question", {}).get("title") or obj.get("name") or ""
                excerpt = _strip_html(obj.get("excerpt") or obj.get("excerpt_new") or "")
                row = {
                    "keyword": keyword,
                    "kind": kind,
                    "target_id": target_id,
                    "title": title,
                    "excerpt": excerpt,
                    "raw": it,
                }
                out.append(row)
                if persist and self.storage and target_id:
                    await self.storage.upsert("zhihu_search", row)
            if (body.get("paging") or {}).get("is_end"):
                break
        return out

    async def get_article(self, article_id: int, *, persist: bool = True) -> dict:
        """Fetch a Zhihu article (column post)."""
        self._require_cookie()
        data = await self._get_json(f"/api/v4/articles/{article_id}")
        if persist and self.storage and data.get("id"):
            await self.storage.upsert("zhihu_articles", _article_row(data, column_id=None))
        return data

    async def get_column(self, column_id: str, *, persist: bool = True) -> dict:
        """Fetch a Zhihu column's (专栏) metadata."""
        self._require_cookie()
        # The bare endpoint omits stats — we have to ask for them via include=.
        params = {
            "include": "intro,description,created,articles_count,followers",
        }
        data = await self._get_json(f"/api/v4/columns/{column_id}", params)
        if persist and self.storage:
            author = data.get("author") or {}
            await self.storage.upsert(
                "zhihu_columns",
                {
                    "id": data.get("id") or column_id,
                    "title": data.get("title"),
                    "author_id": author.get("id"),
                    "author_name": author.get("name"),
                    "description": _strip_html(data.get("description") or ""),
                    "articles_count": data.get("articles_count"),
                    "followers": data.get("followers"),
                    "created": data.get("created"),
                    "updated": data.get("updated"),
                    "raw": data,
                },
            )
        return data

    async def iter_column_items(
        self,
        column_id: str,
        *,
        limit: Optional[int] = None,
        page_size: int = 10,
        persist: bool = True,
    ) -> AsyncIterator[dict]:
        """Yield articles in a column, ordered as the API returns them.

        Each yielded row is normalized for storage; the underlying response is in `raw`.
        """
        self._require_cookie()
        offset = 0
        seen = 0
        url = f"{self.base_url}/api/v4/columns/{column_id}/items"
        while True:
            params = {"limit": page_size, "offset": offset}
            body = await self._get_json(url, params)
            items = body.get("data") or []
            if not items:
                logger.info("zhihu column {}: exhausted at offset {}", column_id, offset)
                break
            for art in items:
                row = _article_row(art, column_id=column_id)
                if persist and self.storage and row["id"]:
                    await self.storage.upsert("zhihu_articles", row)
                yield row
                seen += 1
                if limit and seen >= limit:
                    return
            paging = body.get("paging") or {}
            if paging.get("is_end"):
                break
            offset += page_size
            await asyncio.sleep(0)


    async def scrape_keyword(
        self,
        keyword: str,
        *,
        count: int = 10,
        answers_per_question: int = 5,
        comments_per_answer: int = 0,
        concurrency: int = 3,
        only_new: bool = False,
        progress: Optional[Callable[[Any], None]] = None,
    ) -> dict:
        """MediaCrawler-style search mode: keyword → top-N questions with top answers.

        Implementation note: Zhihu's `/api/v4/questions/{id}` and `/api/v4/answers/*`
        endpoints require an `x-zse-96` signature we don't reproduce — bare httpx
        calls get HTTP 403 with code 10003 ("请求来源异常"). The search endpoint
        and `/api/v4/columns/*` are exempt and work fine via httpx.

        So this method uses a hybrid path:
          - `search` (httpx) collects relevant question ids
          - For each question, render the page via `BrowserSession` and parse
            the embedded `<script id="js-initialData">` JSON, which contains
            the full question metadata + ~5 top answers. No signing needed.

        Concurrency is capped at 1 here regardless of the parameter — browser
        pages share the same context and parallel navigations destabilize playwright
        under stealth. The shared token bucket still rate-limits.

        `progress`, if given, is called with one short Chinese status line at each
        milestone (search done, per-question completion). Used by the CLI so users
        see live progress instead of going silent for minutes.
        """
        def _emit(item: Any) -> None:
            if progress is None:
                return
            try:
                progress(item)
            except Exception:  # noqa: BLE001 — never let a bad UI callback break a scrape
                pass

        self._require_cookie()
        _emit(f"[知乎] 搜索关键词 “{keyword}” …")
        # 20 per search page — request enough to fill `count` after kind-filtering.
        pages_needed = max(1, (count * 2 + 19) // 20)
        hits = await self.search(keyword, pages=pages_needed)
        # Pull question ids from `question` hits *and* the parent question of
        # `answer` hits — for many keywords Zhihu returns mostly answers, so
        # filtering to direct `question` matches alone yields nothing useful.
        seen: set[int] = set()
        question_ids: list[int] = []
        for h in hits:
            qid: Optional[int] = None
            kind = h.get("kind")
            if kind == "question" and h.get("target_id"):
                try:
                    qid = int(h["target_id"])
                except (TypeError, ValueError):
                    qid = None
            elif kind == "answer":
                parent = ((h.get("raw") or {}).get("object") or {}).get("question") or {}
                pid = parent.get("id")
                if pid is not None:
                    try:
                        qid = int(pid)
                    except (TypeError, ValueError):
                        qid = None
            if qid is not None and qid not in seen:
                seen.add(qid)
                question_ids.append(qid)
            if len(question_ids) >= count:
                break

        skipped = 0
        if only_new and question_ids and self.storage:
            seen_qids = await self.storage.keyword_post_ids(keyword=keyword, platform="zhihu")
            before = len(question_ids)
            question_ids = [q for q in question_ids if str(q) not in seen_qids]
            skipped = before - len(question_ids)
            if skipped:
                _emit(f"[知乎] --only-new 跳过 {skipped} 个已抓过的问题")

        if not question_ids:
            if only_new and skipped > 0:
                _emit(f"[知乎] 没有新问题（{skipped} 个都已抓过）")
            else:
                logger.warning(
                    "知乎采集：关键词 {!r} 没搜到任何问题。可能是被风控、关键词太冷门、"
                    "或 cookie 失效 — 跑一下 `python -m scraper doctor` 看看。",
                    keyword,
                )
                _emit(f"[知乎] 没搜到任何问题（可能被风控/cookie 失效/关键词冷门）")
            return {
                "keyword": keyword, "questions": 0, "answers": 0,
                "comments": 0, "requested": count, "skipped": skipped,
                "failed": 0, "errors": {}, "run_id": None,
            }
        _emit(f"[知乎] 搜到 {len(question_ids)} 个问题，开始抓取（浏览器渲染，约 1–2 秒/个）")
        _emit(TotalEvent(total=len(question_ids), label=f"知乎 · {keyword}"))

        run_id: Optional[int] = None
        if self.storage:
            run_id = await self.storage.start_run(
                keyword=keyword,
                platform="zhihu",
                requested_count=count,
                config={
                    "answers_per_question": answers_per_question,
                    "concurrency": concurrency,
                    "only_new": only_new,
                },
            )
            for rank, qid in enumerate(question_ids, start=1):
                await self.storage.record_keyword_post(
                    keyword=keyword, platform="zhihu", post_id=str(qid),
                    run_id=run_id, rank=rank,
                )

        # Lazy-import to keep playwright optional for users who don't need scrape mode.
        from scraper.core.browser import BrowserSession

        answers_total = 0
        comments_total = 0
        questions_done = 0
        failed = 0
        errors: dict[str, int] = {}
        error_msg: Optional[str] = None
        try:
            async with BrowserSession(
                rate=self._rate,
                cookie=self._cookie,
                cookie_domain=".zhihu.com",
                # Persistent profile so login state, anti-bot tokens, and any
                # interactive challenges survive between scrape runs. Makes the
                # session look like a returning browser, not a brand-new install.
                user_data_dir="data/browser-profile/zhihu",
            ) as browser:
                total = len(question_ids)
                for idx, qid in enumerate(question_ids, start=1):
                    try:
                        ans_ids = await self._fetch_question_via_browser(
                            browser, qid, max_answers=answers_per_question,
                        )
                        answers_total += len(ans_ids)
                        questions_done += 1
                        kept_here = 0
                        if comments_per_answer > 0 and ans_ids:
                            _emit(f"[知乎] 问题 {idx}/{total} 抓评论中…（{len(ans_ids)} 个回答 × 最多 {comments_per_answer} 条）")
                            for aid in ans_ids:
                                kept = await self.fetch_answer_comments_via_browser(
                                    browser, qid, aid, max_comments=comments_per_answer,
                                )
                                comments_total += kept
                                kept_here += kept
                        _emit(
                            f"[知乎] 问题 {idx}/{total} ✓ {qid}  回答 {len(ans_ids)}"
                            + (f"  评论 {kept_here}" if comments_per_answer > 0 else "")
                        )
                        _emit(StepEvent(
                            ok=True,
                            extra={"answers": len(ans_ids), "comments": kept_here},
                        ))
                    except ScraperError as e:
                        failed += 1
                        err_name = type(e).__name__
                        errors[err_name] = errors.get(err_name, 0) + 1
                        logger.warning(
                            "知乎采集：问题 {} 抓取失败（关键词 {!r}）：{}",
                            qid, keyword, e,
                        )
                        _emit(f"[知乎] 问题 {idx}/{total} ✗ {qid}  ({err_name})")
                        _emit(StepEvent(ok=False, error=err_name))

            logger.info(
                "zhihu scrape: keyword={!r} questions={} answers={} comments={} failed={}",
                keyword, questions_done, answers_total, comments_total, failed,
            )
            return {
                "keyword": keyword,
                "questions": questions_done,
                "answers": answers_total,
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
                    posts_fetched=questions_done,
                    # Reuse comments_fetched slot for answers + comments combined,
                    # since "comments-on-Zhihu" semantically straddles both.
                    comments_fetched=answers_total + comments_total,
                    error=error_msg,
                )

    async def fetch_answer_comments_via_browser(
        self,
        browser: Any,
        question_id: int,
        answer_id: int,
        *,
        max_comments: int = 20,
    ) -> int:
        """Best-effort fetch of comments on a Zhihu answer.

        Zhihu's `/api/v4/comment_v5/answers/{aid}/root_comment` requires x-zse-96
        signing we don't reproduce. The page also doesn't render comments into
        SSR initialData. So we navigate to the answer page, scroll to the bottom
        to trigger Zhihu's lazy-load, and intercept the signed XHR they make.

        Returns the number of comments persisted. May return 0 if the lazy-load
        doesn't fire (Zhihu sometimes hides comments behind an explicit click) —
        the caller should treat this as best-effort, not guaranteed.
        """
        url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"

        async def _scroll_then_settle(page: Any) -> None:
            # Comments on Zhihu are behind a "查看XXX条评论" button. Scrolling alone
            # doesn't fire the XHR; we have to click. Strategy:
            #  1) scroll into the comments region so the button is in viewport
            #  2) try a handful of selectors and aria-labels (Zhihu rotates classes)
            #  3) as a last resort, click any element whose text contains "条评论"
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.0)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(0.5)

            selectors = [
                'button[aria-label*="评论"]',
                'button[id*="Comments"]',
                '.Comments-toggleButton',
                '.ContentItem-actions button:has-text("评论")',
            ]
            clicked = False
            for sel in selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # Last resort: find any visible button whose innerText mentions "评论"
                try:
                    await page.evaluate(
                        "() => { for (const el of document.querySelectorAll('button,span,a')) "
                        "{ if (el.innerText && el.innerText.includes('评论') && el.offsetParent) "
                        "{ el.click(); return; } } }"
                    )
                except Exception:
                    pass
            await asyncio.sleep(1.0)

        try:
            body = await browser.capture_xhr(
                url,
                lambda u: "comment_v5" in u and str(answer_id) in u and "root_comment" in u,
                timeout_ms=12_000,
                after_goto=_scroll_then_settle,
            )
        except Exception as e:
            logger.warning(
                "知乎评论：回答 {} 的懒加载没触发（{}）。"
                "评论数量已经记录在 zhihu_answers 表里，只是跳过了具体评论文本 — "
                "不影响整体统计。",
                answer_id, type(e).__name__,
            )
            return 0

        comments = body.get("data") or []
        kept = 0
        for c in comments[:max_comments]:
            author = c.get("author") or {}
            row = {
                "comment_id": c.get("id"),
                "answer_id": answer_id,
                "author_id": author.get("id"),
                "author_name": author.get("name"),
                "content": _strip_html(c.get("content") or ""),
                "like_count": c.get("like_count") or c.get("vote_count"),
                "created_time": c.get("created_time"),
                "raw": c,
            }
            if self.storage and row["comment_id"]:
                await self.storage.upsert("zhihu_answer_comments", row)
                kept += 1
        return kept

    async def _fetch_question_via_browser(
        self,
        browser: Any,
        question_id: int,
        *,
        max_answers: int,
    ) -> list[int]:
        """Render a Zhihu question page and parse its embedded initialData JSON.

        Returns the list of answer ids persisted. Sidesteps the x-zse-96 signature
        requirement by extracting data from server-rendered HTML rather than the
        signed API.
        """
        url = f"https://www.zhihu.com/question/{question_id}"
        try:
            # domcontentloaded (not the default "load"): the question's answers are
            # server-rendered into the js-initialData blob, so we only need the parsed
            # HTML — waiting for the full load event on Zhihu's ad-heavy pages times
            # out at 30s far too often.
            html = await browser.get_html(url, wait_until="domcontentloaded")
        except ScraperError:
            raise
        except Exception as e:
            # Playwright TimeoutError / network errors / etc. need to be re-raised
            # as ScraperError so scrape_keyword's `except ScraperError` block treats
            # them as a per-question failure (logs + continues) rather than letting
            # them bubble up and kill the whole asyncio.gather batch.
            raise ScraperError(
                f"browser fetch failed for question {question_id}: "
                f"{type(e).__name__}: {e}"
            ) from e
        m = _INITIAL_DATA_RE.search(html)
        if not m:
            raise ParseError(f"no js-initialData script on {url}")
        try:
            initial = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise ParseError(f"initialData not valid JSON for q={question_id}") from e

        entities = (initial.get("initialState") or {}).get("entities") or {}
        questions = entities.get("questions") or {}
        answers = entities.get("answers") or {}
        topics_entity = entities.get("topics") or {}

        q = questions.get(str(question_id))
        if not q:
            raise NotFound(f"question {question_id} not present in rendered page")
        if self.storage:
            answer_count = q.get("answerCount") or q.get("answer_count")
            follower_count = q.get("followerCount") or q.get("follower_count")
            view_count = q.get("visitCount") or q.get("visit_count")
            raw_topics = q.get("topics") or q.get("topicIds") or []
            # Topic ids often appear as a list of ids referencing entities.topics.
            # Resolve them to names here while we still have the entities dict.
            resolved: list[str] = []
            for t in raw_topics:
                if isinstance(t, dict):
                    name = t.get("name") or t.get("title")
                    if name:
                        resolved.append(str(name))
                else:
                    ent = topics_entity.get(str(t))
                    if ent and ent.get("name"):
                        resolved.append(str(ent["name"]))
                    else:
                        resolved.append(str(t))
            topic_names = resolved
            await self.storage.upsert(
                "zhihu_questions",
                {
                    "id": q.get("id") or question_id,
                    "title": q.get("title"),
                    "detail": _strip_html(q.get("detail") or ""),
                    "answer_count": answer_count,
                    "follower_count": follower_count,
                    "view_count": view_count,
                    "created": q.get("created"),
                    "topics": topic_names if topic_names else None,
                    "raw": q,
                },
            )
            await self.storage.record_engagement(
                platform="zhihu_question",
                post_id=str(q.get("id") or question_id),
                view_count=view_count,
                like_count=None,
                comment_count=answer_count,
                favorite_count=follower_count,
                share_count=None,
            )

        # The answers entity store is keyed by answer id and contains every answer
        # serialized into the page (typically the first ~5). Filter to the ones
        # that belong to this question, sort by score, and persist up to max_answers.
        my_answers = [
            a for a in answers.values()
            if str((a.get("question") or {}).get("id")) == str(question_id)
        ]
        my_answers.sort(key=lambda a: a.get("voteupCount") or 0, reverse=True)
        persisted_ids: list[int] = []
        for ans in my_answers[:max_answers]:
            author = ans.get("author") or {}
            row = {
                "id": ans.get("id"),
                "question_id": question_id,
                "author_id": author.get("id"),
                "author_name": author.get("name"),
                "voteup_count": ans.get("voteupCount") or ans.get("voteup_count"),
                "comment_count": ans.get("commentCount") or ans.get("comment_count"),
                "created_time": ans.get("createdTime") or ans.get("created_time"),
                "updated_time": ans.get("updatedTime") or ans.get("updated_time"),
                "content": _strip_html(ans.get("content") or ""),
                "raw": ans,
            }
            if self.storage and row["id"]:
                await self.storage.upsert("zhihu_answers", row)
                persisted_ids.append(int(row["id"]))
        return persisted_ids


def _extract_topic_names(topics: Any) -> list[str]:
    """Normalize Zhihu's many topic representations to a flat list of names.

    The API include=topics version returns a list of {id,name,...} dicts; the
    initialData (rendered page) version often returns a list of topic ids that
    point into entities.topics. We can't resolve the id->name without the
    entities dict, so for that case we just return ids as strings (better than
    nothing — name lookup is cheap in the analyze layer).
    """
    if not topics:
        return []
    out: list[str] = []
    for t in topics:
        if isinstance(t, dict):
            name = t.get("name") or t.get("title")
            if name:
                out.append(str(name))
        elif isinstance(t, (str, int)):
            out.append(str(t))
    return out


def _article_row(article: dict, *, column_id: Optional[str]) -> dict:
    author = article.get("author") or {}
    col = article.get("column") or {}
    return {
        "id": article.get("id"),
        "column_id": column_id or col.get("id"),
        "title": article.get("title"),
        "author_id": author.get("id"),
        "author_name": author.get("name"),
        "voteup_count": article.get("voteup_count"),
        "comment_count": article.get("comment_count"),
        "created": article.get("created"),
        "updated": article.get("updated"),
        "excerpt": _strip_html(article.get("excerpt") or ""),
        "content": _strip_html(article.get("content") or ""),
        "raw": article,
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    if "<" not in html:
        return html
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text("\n", strip=True)

from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import click
from loguru import logger

from scraper.core.config import Config, load_config
from scraper.core.exceptions import ScraperError
from scraper.core.storage import SqliteStorage
from scraper.platforms.bilibili import BilibiliScraper
from scraper.platforms.tieba import TiebaScraper
from scraper.platforms.weibo import WeiboScraper
from scraper.platforms.zhihu import ZhihuScraper


class _PlatformSpec:
    """One row of the platform registry — everything the generic scrape/summary
    plumbing needs to drive a platform without hardcoding its name.

    `key` is the short token used on the CLI and in `keyword_posts.platform`
    ('bili'/'zhihu'/'weibo'/'tieba'). `make_kwargs` maps the common scrape-options
    dict to the platform's `scrape_keyword` keyword args; `comments_opt` is the opts
    key whose 0-value means "comments were skipped" (drives the end-of-run tip).
    """

    def __init__(self, key, label, scraper_cls, posts_key, make_kwargs,
                 comments_opt, comments_tip, has_answers=False):
        self.key = key
        self.label = label
        self.scraper_cls = scraper_cls
        self.posts_key = posts_key
        self._make_kwargs = make_kwargs
        self.comments_opt = comments_opt
        self.comments_tip = comments_tip
        self.has_answers = has_answers

    def make_kwargs(self, opts: dict) -> dict:
        return self._make_kwargs(opts)

    def no_comments(self, opts: dict) -> bool:
        return (opts.get(self.comments_opt) or 0) == 0


# The platform registry. Adding a platform here wires it into `scrape`, the
# interactive loop, and the end-of-run summary — no per-platform `if` branches.
_PLATFORMS: dict[str, _PlatformSpec] = {
    "bili": _PlatformSpec(
        "bili", "B站", BilibiliScraper, "videos",
        lambda o: dict(count=o["count"], comments_pages=o["comments_pages"], only_new=o["only_new"]),
        "comments_pages", "本次未抓评论。加 --comments-pages 2 可一并抓评论。",
    ),
    "zhihu": _PlatformSpec(
        "zhihu", "知乎", ZhihuScraper, "questions",
        lambda o: dict(count=o["count"], answers_per_question=o["answers_per_q"],
                       comments_per_answer=o["comments_per_answer"], only_new=o["only_new"]),
        "comments_per_answer", "本次未抓每条评论。加 --comments-per-answer 10 可一并抓评论。",
        has_answers=True,
    ),
    "weibo": _PlatformSpec(
        "weibo", "微博", WeiboScraper, "posts",
        lambda o: dict(count=o["count"], comments_count=o["weibo_comments"], only_new=o["only_new"]),
        "weibo_comments", "本次未抓评论。加 --weibo-comments 10 可一并抓评论。",
    ),
    "tieba": _PlatformSpec(
        "tieba", "贴吧", TiebaScraper, "threads",
        lambda o: dict(count=o["count"], replies_per_thread=o["tieba_replies"], only_new=o["only_new"]),
        "tieba_replies", "本次未抓楼层回复。加 --tieba-replies 10 可一并抓楼层。",
    ),
}


def _setup_logging(cfg: Config) -> None:
    logger.remove()
    logger.add(sys.stderr, level=cfg.logging.level)
    if cfg.logging.file:
        Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(cfg.logging.file, level=cfg.logging.level, rotation="10 MB")


async def _open_storage(cfg: Config) -> SqliteStorage:
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()
    return storage


def _ensure_utf8_streams() -> None:
    """Force stdout/stderr to UTF-8 on Windows. Idempotent and best-effort.

    Needed because Python defaults streams to the system locale (cp936/GBK on
    a Chinese Windows install), which can't encode ✓/✗ and our other glyphs
    when output is piped to a file (CI logs, background tasks). Real consoles
    usually inherit utf-8 fine; pipes are where this bites.
    """
    if sys.platform != "win32":
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8")
        except Exception:
            pass


def _progress_echo(msg) -> None:
    """Fallback progress callback — print free-form strings, drop structured events.

    Kept for back-compat with any caller still passing a plain function; the
    real bar-driven UX is in `_ScrapeProgress` below.
    """
    if isinstance(msg, str):
        click.echo(f"  {msg}")


class _ScrapeProgress:
    """Progress reporter that drives a tqdm bar from `TotalEvent` / `StepEvent`.

    Gracefully degrades when tqdm isn't installed — strings still print one per
    line, just without the bar. Construct one per scrape_keyword call and close
    it afterwards (use `with` ... or call `.close()`).
    """

    def __init__(self, label: str = "") -> None:
        # tqdm writes to stderr; both ✓/✗ in the postfix and the desc bar will
        # bomb on a GBK-encoded pipe without this.
        _ensure_utf8_streams()
        self.label = label
        self.bar = None
        try:
            from tqdm import tqdm  # type: ignore[import-not-found]
            self._tqdm = tqdm
        except ImportError:
            self._tqdm = None
        self._ok = 0
        self._fail = 0
        self._comments = 0

    def __call__(self, item) -> None:
        # Lazy import to avoid touching the progress module from the CLI startup path.
        from scraper.core.progress import StepEvent, TotalEvent

        if isinstance(item, TotalEvent):
            if self._tqdm is not None and self.bar is None:
                self.bar = self._tqdm(
                    total=item.total,
                    desc=item.label or self.label,
                    unit="个",
                    leave=True,
                    ncols=88,
                )
            elif self._tqdm is None:
                # Fallback: announce total but don't draw anything fancy.
                click.echo(f"  ({item.label or self.label}: {item.total} 项)")
        elif isinstance(item, StepEvent):
            if item.ok:
                self._ok += 1
            else:
                self._fail += 1
            if item.extra:
                self._comments += int(item.extra.get("comments") or 0)
            if self.bar is not None:
                self.bar.set_postfix_str(
                    f"✓{self._ok} ✗{self._fail} 评论={self._comments}"
                )
                self.bar.update(1)
        else:
            # Free-form string. tqdm.write keeps the bar from getting torn by stdout.
            text = f"  {item}"
            if self.bar is not None:
                self.bar.write(text)
            else:
                click.echo(text)

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()
            self.bar = None

    def __enter__(self) -> "_ScrapeProgress":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _format_errors(errors: dict) -> str:
    """Render error tally like ' (HTTP 412 ×2, SoftBanned ×1)' or '' if empty."""
    if not errors:
        return ""
    parts = [f"{name} ×{n}" for name, n in sorted(errors.items(), key=lambda x: -x[1])]
    return " (" + ", ".join(parts) + ")"


def _print_scrape_summary(platform: str, summary: dict, *, no_comments: bool = False) -> None:
    """One-line end-of-scrape summary with success/fail/skip/error breakdown."""
    _ensure_utf8_streams()
    spec = _PLATFORMS.get(platform)
    posts_done = summary.get(spec.posts_key if spec else "posts", 0)
    failed = summary.get("failed", 0)
    requested = summary.get("requested", 0)
    skipped = summary.get("skipped", 0)
    errors = summary.get("errors") or {}
    comments = summary.get("comments", 0)

    label = spec.label if spec else platform
    head = f"  [{label}] 完成: ✓{posts_done} 成功"
    if failed:
        head += f" / ✗{failed} 失败" + _format_errors(errors)

    extras = []
    if spec and spec.has_answers:
        extras.append(f"回答 {summary.get('answers', 0)}")
    extras.append(f"评论 {comments}")
    if skipped:
        extras.append(f"跳过(已抓过) {skipped}")
    if requested:
        extras.append(f"目标 {requested}")
    click.echo(head + " · " + " · ".join(extras))

    if no_comments and spec:
        click.echo(f"  提示: {spec.comments_tip}")


def _maybe_emit_reports(keywords, db_path: str, do_open: bool = False) -> None:
    """Build one report per keyword after a scrape. Failures are reported but
    don't propagate — the underlying scrape already succeeded."""
    try:
        from scraper.report import build_report
    except Exception as e:
        click.echo(f"(报告跳过: {type(e).__name__}: {e})")
        return
    paths: list = []
    for kw in keywords:
        try:
            p = build_report(kw, db_path=db_path)
            paths.append(p)
        except Exception as e:
            click.echo(f"  (报告 {kw!r} 生成失败: {type(e).__name__}: {e})")
    if paths:
        click.echo()
        click.echo("报告已生成:")
        for p in paths:
            click.echo(f"   {p}")
        if do_open and paths:
            import webbrowser
            webbrowser.open(paths[0].as_uri())


def _export_runs_csv(run_ids: list[int], db_path: Optional[str] = None) -> None:
    """Write CSV exports and surface the paths prominently.

    Failures are logged but don't propagate — the scrape itself succeeded
    regardless of whether the convenience CSV got written. `db_path` must be the
    active config's DB path; without it the analyze layer falls back to
    config.yaml, which is the wrong DB under a custom `--config`.
    """
    if not run_ids:
        return
    try:
        from scraper.analyze import export_run_to_csv
    except Exception as e:
        click.echo(f"(csv export skipped: {type(e).__name__}: {e})")
        return
    all_paths: list[str] = []
    for rid in run_ids:
        try:
            paths = export_run_to_csv(rid, db_path=db_path)
            for _role, p in paths.items():
                all_paths.append(p)
        except Exception as e:
            click.echo(f"  (csv export failed for run {rid}: {type(e).__name__}: {e})")
    if all_paths:
        click.echo()
        click.echo("CSV 已生成 (可直接 Excel 打开):")
        for p in all_paths:
            click.echo(f"   {p}")


class _GroupedCLI(click.Group):
    """Click group that prints `--help` commands clustered by purpose.

    Lifted from the user feedback: 10 commands in a flat list make `--help` look
    like everything is equally important. Grouping them by intent (daily / setup /
    troubleshoot / advanced) tells new users where to start without hiding any
    capability.
    """

    SECTIONS: list[tuple[str, list[str]]] = [
        ("日常使用 Daily", ["scrape", "grab", "interactive", "status", "last", "open", "sentiment", "report"]),
        ("首次设置 Setup", ["init", "login"]),
        ("出问题时 Troubleshooting", ["doctor"]),
        ("高级 / 单平台 Advanced", ["bili", "zhihu", "weibo", "tieba", "view", "export"]),
    ]

    def format_commands(self, ctx: click.Context, formatter) -> None:
        commands = {name: self.get_command(ctx, name) for name in self.list_commands(ctx)}
        # Drop hidden / nonexistent entries.
        commands = {n: c for n, c in commands.items() if c is not None and not c.hidden}

        listed: set[str] = set()
        for section, names in self.SECTIONS:
            rows = []
            for name in names:
                cmd = commands.get(name)
                if cmd is None:
                    continue
                rows.append((name, cmd.get_short_help_str(limit=60)))
                listed.add(name)
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

        # Anything not explicitly placed falls into a final "其他" bucket so
        # newly-added commands stay visible until they get categorized.
        leftovers = [(n, commands[n].get_short_help_str(limit=60))
                     for n in commands if n not in listed]
        if leftovers:
            with formatter.section("其他 Other"):
                formatter.write_dl(leftovers)


@click.group(cls=_GroupedCLI, invoke_without_command=True)
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """爬虫工具 — Bilibili & Zhihu scraping CLI.

    Run without a subcommand to drop into the guided interactive scrape loop —
    the friendliest entry point for new users.
    """
    # Set stdout/stderr to UTF-8 once for the entire CLI invocation so every
    # subcommand can safely emit Chinese, ✓/✗ glyphs, and tqdm bars without
    # tripping the Windows GBK default for piped streams.
    _ensure_utf8_streams()
    cfg = load_config(config_path)
    _setup_logging(cfg)
    ctx.obj = cfg

    if ctx.invoked_subcommand is None:
        ctx.invoke(interactive)


# ---------------------------------------------------------------------------
# Bilibili
# ---------------------------------------------------------------------------


@cli.group()
def bili() -> None:
    """Bilibili commands."""


@bili.command("video")
@click.argument("bvid")
@click.pass_obj
def bili_video(cfg: Config, bvid: str) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            v = await s.get_video(bvid)
            click.echo(f"{v.bvid}\t{v.title}")
            click.echo(f"  up: {v.owner.get('name')} ({v.owner.get('mid')})")
            click.echo(f"  views={v.stat.get('view')} likes={v.stat.get('like')} replies={v.stat.get('reply')}")
        await storage.close()

    asyncio.run(run())


@bili.command("comments")
@click.argument("bvid")
@click.option("--pages", default=3, show_default=True)
@click.pass_obj
def bili_comments(cfg: Config, bvid: str, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            n = 0
            async for c in s.iter_comments(bvid, pages=pages):
                n += 1
                msg = (c.get("message") or "").replace("\n", " ")[:120]
                click.echo(f"  {c['uname']}: {msg}")
            click.echo(f"-- {n} comments saved --")
        await storage.close()

    asyncio.run(run())


@bili.command("user")
@click.argument("mid", type=int)
@click.option("--pages", default=1, show_default=True)
@click.pass_obj
def bili_user(cfg: Config, mid: int, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            videos = await s.get_user_videos(mid, pages=pages)
            for v in videos:
                click.echo(f"  {v.get('bvid')}\t{v.get('title')}")
            click.echo(f"-- {len(videos)} videos --")
        await storage.close()

    asyncio.run(run())


@bili.command("dynamics")
@click.argument("mid", type=int)
@click.option("--pages", default=1, show_default=True, help="Number of feed pages to walk.")
@click.pass_obj
def bili_dynamics(cfg: Config, mid: int, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            n = 0
            async for d in s.iter_user_dynamics(mid, pages=pages):
                n += 1
                text = (d.get("text") or "").replace("\n", " ")[:120]
                tag = (d.get("type") or "").replace("DYNAMIC_TYPE_", "")
                click.echo(f"  [{tag:<8}] {d['dynamic_id']}\t{text}")
            click.echo(f"-- {n} dynamics saved --")
        await storage.close()

    asyncio.run(run())


@bili.command("scrape")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--count", default=10, show_default=True, help="Top-N videos per keyword.")
@click.option(
    "--comments-pages",
    default=1,
    show_default=True,
    help="Pages of comments per video (0 to skip).",
)
@click.option("--concurrency", default=4, show_default=True)
@click.option(
    "--only-new/--no-only-new",
    "only_new",
    default=False,
    show_default=True,
    help="Skip post_ids already linked to this keyword in prior runs.",
)
@click.option(
    "--csv/--no-csv",
    "write_csv",
    default=True,
    show_default=True,
    help="Auto-export each run's posts+comments to CSV under data/exports/.",
)
@click.option(
    "--report/--no-report",
    "write_report",
    default=False,
    show_default=True,
    help="Auto-generate an HTML analysis report after scrape (data/reports/).",
)
@click.pass_obj
def bili_scrape(
    cfg: Config,
    keywords: tuple[str, ...],
    count: int,
    comments_pages: int,
    concurrency: int,
    only_new: bool,
    write_csv: bool,
    write_report: bool,
) -> None:
    """Fan out: for each keyword, search + fetch top-N videos with comments."""

    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            for kw in keywords:
                click.echo(f"--- bili scrape: {kw} ---")
                with _ScrapeProgress(label=f"B站 · {kw}") as prog:
                    summary = await s.scrape_keyword(
                        kw,
                        count=count,
                        comments_pages=comments_pages,
                        concurrency=concurrency,
                        only_new=only_new,
                        progress=prog,
                    )
                _print_scrape_summary(
                    "bili", summary, no_comments=(comments_pages == 0),
                )
                if summary.get("run_id") is not None:
                    run_ids.append(summary["run_id"])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports(keywords, cfg.storage.path)


@bili.command("search")
@click.argument("keyword")
@click.option("--pages", default=1, show_default=True)
@click.option("--order", default="totalrank", show_default=True)
@click.pass_obj
def bili_search(cfg: Config, keyword: str, pages: int, order: str) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with BilibiliScraper(cfg, storage) as s:
            results = await s.search(keyword, pages=pages, order=order)
            for r in results:
                click.echo(f"  [{r['rank']:>3}] {r['bvid']}\t{r['author']}\t{r['title']}")
            click.echo(f"-- {len(results)} results --")
        await storage.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Zhihu
# ---------------------------------------------------------------------------


@cli.group()
def zhihu() -> None:
    """Zhihu commands. Requires zhihu.cookie set in config.yaml."""


@zhihu.command("question")
@click.argument("question_id", type=int)
@click.pass_obj
def zhihu_question(cfg: Config, question_id: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            q = await s.get_question(question_id)
            click.echo(f"{q.get('id')}\t{q.get('title')}")
            click.echo(f"  answers={q.get('answer_count')} followers={q.get('follower_count')}")
        await storage.close()

    asyncio.run(run())


@zhihu.command("answers")
@click.argument("question_id", type=int)
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_obj
def zhihu_answers(cfg: Config, question_id: int, limit: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            n = 0
            async for ans in s.iter_answers(question_id, limit=limit):
                n += 1
                excerpt = (ans.get("content") or "").replace("\n", " ")[:140]
                click.echo(f"  [{ans['voteup_count']:>5}] {ans['author_name']}: {excerpt}")
            click.echo(f"-- {n} answers saved --")
        await storage.close()

    asyncio.run(run())


@zhihu.command("user")
@click.argument("url_token")
@click.pass_obj
def zhihu_user(cfg: Config, url_token: str) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            u = await s.get_user(url_token)
            click.echo(f"{u.get('name')} ({u.get('url_token')})")
            click.echo(
                f"  followers={u.get('follower_count')} answers={u.get('answer_count')} articles={u.get('articles_count')}"
            )
            if u.get("headline"):
                click.echo(f"  headline: {u['headline']}")
        await storage.close()

    asyncio.run(run())


@zhihu.command("column")
@click.argument("column_id")
@click.option("--limit", type=int, default=20, show_default=True, help="Max articles to fetch.")
@click.pass_obj
def zhihu_column(cfg: Config, column_id: str, limit: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            col = await s.get_column(column_id)
            click.echo(f"{col.get('id')}\t{col.get('title')}")
            author = col.get("author") or {}
            click.echo(
                f"  by {author.get('name')} — articles={col.get('articles_count')} followers={col.get('followers')}"
            )
            click.echo("--- articles ---")
            n = 0
            async for art in s.iter_column_items(column_id, limit=limit):
                n += 1
                click.echo(
                    f"  [{art['voteup_count'] or 0:>5}] {art['id']}\t{art['title']}"
                )
            click.echo(f"-- {n} articles saved --")
        await storage.close()

    asyncio.run(run())


@zhihu.command("scrape")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--count", default=10, show_default=True, help="Top-N questions per keyword.")
@click.option(
    "--answers-per-q",
    "answers_per_q",
    default=5,
    show_default=True,
    help="Top answers fetched per question (0 to skip).",
)
@click.option(
    "--comments-per-answer",
    "comments_per_answer",
    default=0,
    show_default=True,
    help="Per-answer comments (with author + likes). 0 = skip. Slow — uses browser "
         "scroll-trigger because Zhihu's comment endpoint needs x-zse-96 signing.",
)
@click.option("--concurrency", default=3, show_default=True)
@click.option(
    "--only-new/--no-only-new",
    "only_new",
    default=False,
    show_default=True,
    help="Skip question_ids already linked to this keyword in prior runs.",
)
@click.option(
    "--csv/--no-csv",
    "write_csv",
    default=True,
    show_default=True,
    help="Auto-export each run's posts+comments to CSV under data/exports/.",
)
@click.option(
    "--report/--no-report",
    "write_report",
    default=False,
    show_default=True,
    help="Auto-generate an HTML analysis report after scrape (data/reports/).",
)
@click.pass_obj
def zhihu_scrape(
    cfg: Config,
    keywords: tuple[str, ...],
    count: int,
    answers_per_q: int,
    comments_per_answer: int,
    concurrency: int,
    only_new: bool,
    write_csv: bool,
    write_report: bool,
) -> None:
    """Fan out: for each keyword, search + fetch top-N questions with answers."""

    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            for kw in keywords:
                click.echo(f"--- zhihu scrape: {kw} ---")
                with _ScrapeProgress(label=f"知乎 · {kw}") as prog:
                    summary = await s.scrape_keyword(
                        kw,
                        count=count,
                        answers_per_question=answers_per_q,
                        comments_per_answer=comments_per_answer,
                        concurrency=concurrency,
                        only_new=only_new,
                        progress=prog,
                    )
                _print_scrape_summary(
                    "zhihu", summary,
                    no_comments=(comments_per_answer == 0),
                )
                if summary.get("run_id") is not None:
                    run_ids.append(summary["run_id"])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports(keywords, cfg.storage.path)


@zhihu.command("search")
@click.argument("keyword")
@click.option("--pages", default=1, show_default=True)
@click.pass_obj
def zhihu_search(cfg: Config, keyword: str, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with ZhihuScraper(cfg, storage) as s:
            results = await s.search(keyword, pages=pages)
            for r in results:
                click.echo(f"  [{r['kind']:<10}] {r['target_id']}\t{r['title'][:80]}")
            click.echo(f"-- {len(results)} hits --")
        await storage.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Weibo
# ---------------------------------------------------------------------------


@cli.group()
def weibo() -> None:
    """Weibo commands (m.weibo.cn). Search works anonymously; set weibo.cookie for reliability."""


@weibo.command("search")
@click.argument("keyword")
@click.option("--pages", default=1, show_default=True)
@click.pass_obj
def weibo_search(cfg: Config, keyword: str, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with WeiboScraper(cfg, storage) as s:
            posts = await s.search(keyword, pages=pages, persist=True)
            for p in posts:
                text = (p.text or "").replace("\n", " ")[:80]
                click.echo(f"  {p.id}\t{p.user_name}: {text}")
            click.echo(f"-- {len(posts)} posts saved --")
        await storage.close()

    asyncio.run(run())


@weibo.command("comments")
@click.argument("post_id")
@click.option("--max", "max_count", default=20, show_default=True)
@click.pass_obj
def weibo_comments(cfg: Config, post_id: str, max_count: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with WeiboScraper(cfg, storage) as s:
            n = await s.get_comments(post_id, max_count=max_count)
            click.echo(f"-- {n} comments saved for weibo {post_id} --")
        await storage.close()

    asyncio.run(run())


@weibo.command("scrape")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--count", default=10, show_default=True, help="Top-N posts per keyword.")
@click.option("--comments", "comments_count", default=0, show_default=True,
              help="Hot-flow comments per post (0 to skip).")
@click.option("--only-new/--no-only-new", "only_new", default=False, show_default=True)
@click.option("--csv/--no-csv", "write_csv", default=True, show_default=True)
@click.option("--report/--no-report", "write_report", default=False, show_default=True)
@click.pass_obj
def weibo_scrape(
    cfg: Config,
    keywords: tuple[str, ...],
    count: int,
    comments_count: int,
    only_new: bool,
    write_csv: bool,
    write_report: bool,
) -> None:
    """Fan out: for each keyword, search + fetch top-N posts with comments."""
    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)
        async with WeiboScraper(cfg, storage) as s:
            for kw in keywords:
                click.echo(f"--- weibo scrape: {kw} ---")
                with _ScrapeProgress(label=f"微博 · {kw}") as prog:
                    summary = await s.scrape_keyword(
                        kw, count=count, comments_count=comments_count,
                        only_new=only_new, progress=prog,
                    )
                _print_scrape_summary("weibo", summary, no_comments=(comments_count == 0))
                if summary.get("run_id") is not None:
                    run_ids.append(summary["run_id"])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports(keywords, cfg.storage.path)


# ---------------------------------------------------------------------------
# Tieba
# ---------------------------------------------------------------------------


@cli.group()
def tieba() -> None:
    """Baidu Tieba commands. Keyword search + thread reading work anonymously."""


@tieba.command("search")
@click.argument("keyword")
@click.option("--pages", default=1, show_default=True)
@click.pass_obj
def tieba_search(cfg: Config, keyword: str, pages: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with TiebaScraper(cfg, storage) as s:
            hits = await s.search(keyword, pages=pages)
            for h in hits:
                click.echo(f"  {h['tid']}\t[{h['forum_name']}] {h['title'][:60]}")
            click.echo(f"-- {len(hits)} threads --")
        await storage.close()

    asyncio.run(run())


@tieba.command("thread")
@click.argument("tid")
@click.option("--replies", default=20, show_default=True, help="Max reply floors to fetch.")
@click.pass_obj
def tieba_thread(cfg: Config, tid: str, replies: int) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        async with TiebaScraper(cfg, storage) as s:
            kept = await s.get_thread(tid, max_replies=replies)
            click.echo(f"-- thread {tid} saved, {kept} reply floors --")
        await storage.close()

    asyncio.run(run())


@tieba.command("scrape")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--count", default=10, show_default=True, help="Top-N threads per keyword.")
@click.option("--replies", "replies_per_thread", default=0, show_default=True,
              help="Reply floors per thread (0 to skip).")
@click.option("--only-new/--no-only-new", "only_new", default=False, show_default=True)
@click.option("--csv/--no-csv", "write_csv", default=True, show_default=True)
@click.option("--report/--no-report", "write_report", default=False, show_default=True)
@click.pass_obj
def tieba_scrape(
    cfg: Config,
    keywords: tuple[str, ...],
    count: int,
    replies_per_thread: int,
    only_new: bool,
    write_csv: bool,
    write_report: bool,
) -> None:
    """Fan out: for each keyword, search + fetch top-N threads with replies."""
    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)
        async with TiebaScraper(cfg, storage) as s:
            for kw in keywords:
                click.echo(f"--- tieba scrape: {kw} ---")
                with _ScrapeProgress(label=f"贴吧 · {kw}") as prog:
                    summary = await s.scrape_keyword(
                        kw, count=count, replies_per_thread=replies_per_thread,
                        only_new=only_new, progress=prog,
                    )
                _print_scrape_summary("tieba", summary, no_comments=(replies_per_thread == 0))
                if summary.get("run_id") is not None:
                    run_ids.append(summary["run_id"])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports(keywords, cfg.storage.path)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@cli.command("scrape")
@click.argument("keywords", nargs=-1, required=True)
@click.option(
    "--platforms",
    "platforms_csv",
    default="bili,zhihu,weibo,tieba",
    show_default=True,
    help="Comma-separated subset of platforms to run (bili, zhihu, weibo, tieba).",
)
@click.option("--count", default=10, show_default=True, help="Top-N items per keyword per platform.")
@click.option("--comments-pages", default=1, show_default=True, help="Bilibili: comment pages per video.")
@click.option("--answers-per-q", "answers_per_q", default=5, show_default=True, help="Zhihu: answers per question.")
@click.option("--comments-per-answer", "comments_per_answer", default=0, show_default=True,
              help="Zhihu: per-answer comments. Slow — opt in only.")
@click.option("--weibo-comments", "weibo_comments", default=0, show_default=True,
              help="Weibo: hot-flow comments per post (0 to skip).")
@click.option("--tieba-replies", "tieba_replies", default=0, show_default=True,
              help="Tieba: reply floors per thread (0 to skip).")
@click.option(
    "--only-new/--no-only-new",
    "only_new",
    default=False,
    show_default=True,
    help="Skip post_ids already linked to this keyword in prior runs (saves quota on daily re-runs).",
)
@click.option(
    "--csv/--no-csv",
    "write_csv",
    default=True,
    show_default=True,
    help="Auto-export each run's posts+comments to CSV under data/exports/.",
)
@click.option(
    "--report/--no-report",
    "write_report",
    default=False,
    show_default=True,
    help="Auto-generate an HTML analysis report after scrape (data/reports/).",
)
@click.pass_obj
def scrape_all(
    cfg: Config,
    keywords: tuple[str, ...],
    platforms_csv: str,
    count: int,
    comments_pages: int,
    answers_per_q: int,
    comments_per_answer: int,
    weibo_comments: int,
    tieba_replies: int,
    only_new: bool,
    write_csv: bool,
    write_report: bool,
) -> None:
    """Search-mode scrape across platforms — MediaCrawler-style fan-out.

    Platforms: bili, zhihu, weibo, tieba (default: all four). Restrict with
    --platforms. Example:
      python -m scraper scrape "电动车" --platforms weibo,tieba --count 5
    """
    wanted = {p.strip() for p in platforms_csv.split(",") if p.strip()}
    unknown = wanted - set(_PLATFORMS)
    if unknown:
        raise click.UsageError(f"unknown platform(s): {', '.join(sorted(unknown))}")

    opts = {
        "count": count,
        "comments_pages": comments_pages,
        "answers_per_q": answers_per_q,
        "comments_per_answer": comments_per_answer,
        "weibo_comments": weibo_comments,
        "tieba_replies": tieba_replies,
        "only_new": only_new,
    }
    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)

        async def run_platform(key: str) -> None:
            spec = _PLATFORMS[key]
            try:
                async with spec.scraper_cls(cfg, storage) as s:
                    for kw in keywords:
                        click.echo(f"--- {key}: {kw} ---")
                        with _ScrapeProgress(label=f"{spec.label} · {kw}") as prog:
                            summary = await s.scrape_keyword(
                                kw, **spec.make_kwargs(opts), progress=prog,
                            )
                        _print_scrape_summary(key, summary, no_comments=spec.no_comments(opts))
                        if summary.get("run_id") is not None:
                            run_ids.append(summary["run_id"])
            except ScraperError as e:
                # e.g. Zhihu with no cookie raises AuthRequired up front — skip this
                # platform with a clear notice rather than aborting the whole batch.
                click.echo(f"  [{spec.label}] 跳过：{type(e).__name__}: {e}")

        # Platforms run in parallel; each has its own session and rate limiter.
        # Iterate the registry (stable order) filtered to the requested set.
        await asyncio.gather(*[run_platform(k) for k in _PLATFORMS if k in wanted])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports(keywords, cfg.storage.path)


@cli.command("doctor")
@click.option(
    "--network/--no-network",
    default=True,
    show_default=True,
    help="Run live network probes (TLS / anti-bot / cookie validity). Off-mode is for air-gapped checks.",
)
@click.pass_obj
def doctor(cfg: Config, network: bool) -> None:
    """Diagnostic check — what's missing, what's broken, with fix commands.

    Run this when something feels off. By default also runs live network probes
    that catch the "looks installed but actually broken" cases (stale cookies,
    soft-banned IP, TLS rejection). Use --no-network to skip those if you're
    offline or in a hurry. Returns non-zero exit code if any critical check
    fails so you can chain it in scripts.
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    problems = 0

    def check(label: str, ok: bool, fix: str = "") -> None:
        nonlocal problems
        marker = "[OK]" if ok else "[!!]"
        click.echo(f"  {marker} {label}")
        if not ok:
            problems += 1
            if fix:
                click.echo(f"        修复: {fix}")

    click.echo("=== 体检 ===")
    click.echo()

    # Python version
    check(
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
        sys.version_info >= (3, 12),
        "需要 Python 3.12+，请升级 Python",
    )

    # Core deps
    for mod, name in [
        ("curl_cffi", "curl_cffi"),
        ("aiosqlite", "aiosqlite"),
        ("click", "click"),
        ("loguru", "loguru"),
        ("yaml", "PyYAML"),
        ("bs4", "beautifulsoup4"),
        ("tenacity", "tenacity"),
        ("pydantic", "pydantic"),
    ]:
        try:
            __import__(mod)
            check(f"{name} 已安装", True)
        except ImportError:
            check(f"{name} 未安装", False, f"pip install {name}")

    # Analysis deps
    for mod, name in [("pandas", "pandas"), ("matplotlib", "matplotlib")]:
        try:
            __import__(mod)
            check(f"{name} 已安装", True)
        except ImportError:
            check(f"{name} 未安装（数据分析需要）", False, f"pip install {name}")

    # Playwright + Chromium
    try:
        import playwright  # noqa: F401
        pw_ok = True
    except ImportError:
        pw_ok = False
    check(
        "playwright 已安装" if pw_ok else "playwright 未安装（采集知乎需要）",
        pw_ok,
        "pip install playwright",
    )
    if pw_ok:
        chromium_ok = _check_chromium_present()
        check(
            "Chromium 浏览器已就绪" if chromium_ok else "Chromium 未下载",
            chromium_ok,
            "python -m playwright install chromium",
        )
        # playwright-stealth is optional but strongly recommended — without it the
        # browser fallback's fingerprint defenses are limited to navigator.* tweaks.
        try:
            import playwright_stealth  # noqa: F401
            check("playwright-stealth 已安装（强烈推荐）", True)
        except ImportError:
            check(
                "playwright-stealth 未安装（推荐安装，否则浏览器指纹防御较弱）",
                False,
                "pip install playwright-stealth",
            )

    # config.yaml
    config_exists = Path("config.yaml").exists()
    check(
        "config.yaml 存在" if config_exists else "config.yaml 缺失",
        config_exists,
        "python -m scraper init",
    )

    # Cookie sanity (passive — no network probe)
    if config_exists:
        zhihu_cookie = getattr(cfg.zhihu, "cookie", "") or ""
        if zhihu_cookie:
            check(
                f"Zhihu cookie 已配置 ({len(zhihu_cookie)} 字符)",
                "z_c0=" in zhihu_cookie,
                "cookie 中缺少 z_c0 登录令牌；重新登录并复制 cookie 后写入 config.yaml",
            )
        else:
            check(
                "Zhihu cookie 未配置（只能用 Bilibili）",
                False,
                "在 config.yaml 的 zhihu.cookie 字段粘贴登录后的 cookie",
            )

    # Data dir writable
    data_dir = Path("data")
    writable = True
    try:
        data_dir.mkdir(exist_ok=True)
        probe = data_dir / ".probe"
        probe.write_text("x")
        probe.unlink()
    except Exception:
        writable = False
    check(
        "data/ 目录可写" if writable else "data/ 目录不可写",
        writable,
        "检查文件权限，或换个工作目录",
    )

    # DB present?
    db_path = Path(cfg.storage.path)
    if db_path.exists():
        size_mb = db_path.stat().st_size / 1_048_576
        check(f"数据库已存在 ({size_mb:.1f} MB) — {db_path}", True)
    else:
        check(
            "数据库尚未创建（采一次就会自动生成）", True,
        )

    # ---- 网络探测 -----------------------------------------------------------
    # The passive checks above only verify "looks set up." The probes below
    # actually talk to the platforms — catches stale cookies, soft-bans, and
    # platform-side breakage that no amount of dep-checking would surface.
    if network:
        click.echo()
        click.echo("--- 网络体检（实际连一下 B站 / 知乎）---")
        try:
            net_problems = asyncio.run(_run_network_probes(cfg, check))
            problems += net_problems
        except Exception as e:
            check(
                f"网络探测意外失败: {type(e).__name__}: {e}",
                False,
                "检查 data/scraper.log 查看堆栈；或加 --no-network 跳过此步",
            )

    click.echo()
    if problems == 0:
        click.echo("[OK] 所有检查通过。一切就绪。")
    else:
        click.echo(f"发现 {problems} 个问题，请按上面的修复提示处理。")
        ctx = click.get_current_context()
        ctx.exit(1)


async def _run_network_probes(cfg: Config, check) -> int:
    """Active network probes — return the number of failing probes.

    Each probe is wrapped in its own try/except so one failure doesn't poison
    the others. Probes:
      1. Bilibili: GET /x/web-interface/nav (anonymous, ~50 ms response). Verifies
         TLS handshake, anti-bot pass, and JSON-shape sanity. The response will
         have `code: -101` when no cookie is set (= "not logged in") — that's
         expected and counts as success here.
      2. Zhihu: if cookie configured, GET /api/v4/me. Returns the logged-in
         user's profile blob if cookie is fresh; otherwise an unauth code that
         tells us "cookie expired" specifically.
    """
    from scraper.core.session import HttpSession
    from scraper.core.exceptions import ScraperError

    probe_failures = 0

    # ---- Bilibili reachability ----
    try:
        async with HttpSession(
            rate=cfg.rate_limit.bilibili,
            timeout=8.0,
            max_retries=1,
            cookie=cfg.bilibili.cookie or "",
            headers={"Referer": "https://www.bilibili.com"},
        ) as s:
            body = await s.get("https://api.bilibili.com/x/web-interface/nav")
        # Code -101 is "not logged in" which is the expected anonymous response;
        # code 0 means we sent a working cookie. Either is fine.
        b_code = body.get("code") if isinstance(body, dict) else None
        if b_code in (0, -101):
            check(
                f"Bilibili 接口可达（code={b_code}{'，已登录' if b_code == 0 else '，匿名'}）",
                True,
            )
        elif b_code == -352:
            check(
                "Bilibili 返回 -352（反爬触发）",
                False,
                "等 10–30 分钟再试；或删 data/cookies/bilibili.json 后重跑",
            )
            probe_failures += 1
        else:
            check(
                f"Bilibili 接口返回异常 code={b_code}",
                False,
                "看 data/scraper.log 末尾几行；可能是 B 站接口改了或风控触发",
            )
            probe_failures += 1
    except ScraperError as e:
        check(
            f"Bilibili HTTP 出错: {str(e)[:100]}",
            False,
            "检查网络；如反复出现可能是反爬 — 删 data/cookies/bilibili.json 后重跑",
        )
        probe_failures += 1
    except Exception as e:
        check(
            f"Bilibili 探测意外失败: {type(e).__name__}: {str(e)[:80]}",
            False,
            "检查网络 / data/scraper.log",
        )
        probe_failures += 1

    # ---- Weibo reachability ----
    try:
        async with HttpSession(
            rate=cfg.rate_limit.weibo,
            timeout=8.0,
            max_retries=1,
            cookie=cfg.weibo.cookie or "",
            headers={
                "Referer": "https://m.weibo.cn/",
                "MWeibo-Pwa": "1",
                "X-Requested-With": "XMLHttpRequest",
            },
        ) as s:
            body = await s.get("https://m.weibo.cn/api/config")
        if isinstance(body, dict) and body.get("ok") == 1:
            logged = bool((body.get("data") or {}).get("login"))
            check(f"微博接口可达（{'已登录' if logged else '匿名'}）", True)
        else:
            check(
                "微博接口返回异常", False,
                "可能被风控；等 10–30 分钟再试，或配置 weibo.cookie 提升稳定性",
            )
            probe_failures += 1
    except ScraperError as e:
        check(f"微博 HTTP 出错: {str(e)[:90]}", False, "检查网络；反复出现可能是反爬触发")
        probe_failures += 1
    except Exception as e:
        check(f"微博探测意外失败: {type(e).__name__}: {str(e)[:70]}", False, "检查网络 / 日志")
        probe_failures += 1

    # ---- Tieba reachability ----
    try:
        async with HttpSession(
            rate=cfg.rate_limit.tieba,
            timeout=8.0,
            max_retries=1,
            cookie=cfg.tieba.cookie or "",
            headers={"Referer": "https://tieba.baidu.com/"},
        ) as s:
            html = await s.get(
                "https://tieba.baidu.com/f/search/res",
                params={"qw": "测试", "pn": 1},
                expect_json=False,
            )
        if isinstance(html, str) and ("s_post" in html or "/p/" in html or "贴吧" in html):
            check("贴吧接口可达（搜索页正常）", True)
        else:
            check("贴吧搜索页返回异常", False, "可能被风控或页面结构变化；等会儿再试")
            probe_failures += 1
    except ScraperError as e:
        check(f"贴吧 HTTP 出错: {str(e)[:90]}", False, "检查网络；反复出现可能是反爬触发")
        probe_failures += 1
    except Exception as e:
        check(f"贴吧探测意外失败: {type(e).__name__}: {str(e)[:70]}", False, "检查网络 / 日志")
        probe_failures += 1

    # ---- Zhihu cookie validity ----
    zhihu_cookie = (getattr(cfg.zhihu, "cookie", "") or "").strip()
    if not zhihu_cookie:
        click.echo("  [跳过] Zhihu 未配置 cookie（用 `python -m scraper login zhihu` 配置）")
        return probe_failures

    try:
        async with HttpSession(
            rate=cfg.rate_limit.zhihu,
            timeout=8.0,
            max_retries=1,
            cookie=zhihu_cookie,
            headers={"Referer": "https://www.zhihu.com/"},
        ) as s:
            body = await s.get("https://www.zhihu.com/api/v4/me")
        if isinstance(body, dict) and body.get("id"):
            uname = body.get("name") or "?"
            check(f"Zhihu cookie 有效（已登录: {uname}）", True)
        elif isinstance(body, dict) and body.get("error"):
            code = body["error"].get("code")
            msg = body["error"].get("message", "")
            if code in (100, 401001, 100200000):
                check(
                    f"Zhihu cookie 已失效（{code}: {msg}）",
                    False,
                    "运行 `python -m scraper login zhihu` 重新登录",
                )
            elif code in (10003, 40362, 1003):
                check(
                    f"Zhihu 触发反爬（{code}: {msg}）",
                    False,
                    "等 10–30 分钟；或确认 playwright-stealth 已安装；必要时换网络",
                )
            else:
                check(
                    f"Zhihu 返回未知错误 code={code} {msg}",
                    False,
                    "看 data/scraper.log；可能是接口改了",
                )
            probe_failures += 1
        else:
            check(
                "Zhihu 接口返回了非预期形状",
                False,
                "看 data/scraper.log；可能是接口改了",
            )
            probe_failures += 1
    except ScraperError as e:
        # /api/v4/me returns 401 for invalid cookie, raised as ScraperError("HTTP 401 ...")
        msg = str(e)
        if "401" in msg:
            check(
                "Zhihu cookie 已失效（HTTP 401）",
                False,
                "运行 `python -m scraper login zhihu` 重新登录",
            )
        else:
            check(
                f"Zhihu HTTP 出错: {msg[:100]}",
                False,
                "检查网络；反复出现可能是反爬触发",
            )
        probe_failures += 1
    except Exception as e:
        check(
            f"Zhihu 探测意外失败: {type(e).__name__}: {str(e)[:80]}",
            False,
            "检查网络 / data/scraper.log",
        )
        probe_failures += 1

    return probe_failures


@cli.command("init")
@click.pass_context
def init(ctx: click.Context) -> None:
    """Interactive first-time setup — config, cookie, Playwright in one go.

    Run this right after cloning / pip-installing. It'll get you from zero to
    "ready to scrape" in about 60 seconds.
    """
    # init runs BEFORE config.yaml exists, so we don't use cfg here.
    import shutil

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    click.echo("=== 爬虫工具 — 初始化向导 ===\n")

    # ---- Step 1: config.yaml ------------------------------------------------
    config_path = Path("config.yaml")
    example_path = Path("config.example.yaml")
    if config_path.exists():
        click.echo(f"[1/4] config.yaml 已存在，跳过创建。")
    else:
        if not example_path.exists():
            click.echo(
                "[1/4] [!!] 找不到 config.example.yaml — 请确认你在项目根目录运行此命令。"
            )
            return
        shutil.copy(example_path, config_path)
        click.echo(f"[1/4] [OK] 已从模板创建 config.yaml")

    # ---- Step 2: Zhihu cookie -----------------------------------------------
    click.echo()
    click.echo("[2/4] Zhihu Cookie")
    click.echo("    Zhihu 接口需要登录后的 cookie。获取方法：")
    click.echo("      1. 浏览器打开 https://www.zhihu.com 并登录")
    click.echo("      2. 按 F12 → Network → 刷新页面 → 点任意一条 zhihu.com 请求")
    click.echo("      3. Request Headers 里找到 cookie: 整段复制")
    click.echo()
    set_cookie = click.confirm("    现在粘贴 Zhihu cookie? (跳过则只能用 Bilibili)", default=True)
    if set_cookie:
        cookie = click.prompt("    粘贴 cookie 后回车", type=str, default="", show_default=False)
        cookie = cookie.strip()
        if cookie:
            _set_cookie_in_config(config_path, "zhihu", cookie)
            if "z_c0=" in cookie:
                click.echo("    [OK] Cookie 已保存（检测到 z_c0 登录令牌）")
            else:
                click.echo("    [!]  Cookie 已保存，但未检测到 z_c0 令牌 — 知乎接口可能拒绝")
        else:
            click.echo("    跳过 cookie 设置。后续可手动编辑 config.yaml。")

    # ---- Step 3: Playwright -------------------------------------------------
    click.echo()
    click.echo("[3/4] Playwright（采集知乎问题/回答必需）")
    pw_installed = False
    try:
        import playwright  # noqa: F401
        pw_installed = True
        click.echo("    [OK] playwright Python 包已安装")
    except ImportError:
        click.echo("    [!!] playwright 未安装")

    chromium_present = _check_chromium_present()
    if pw_installed and chromium_present:
        click.echo("    [OK] Chromium 浏览器已就绪")
    else:
        if not pw_installed:
            do_pip = click.confirm("    现在安装 playwright? (~10 MB)", default=True)
            if do_pip:
                import subprocess
                click.echo("    pip install playwright ...")
                r = subprocess.run([sys.executable, "-m", "pip", "install", "playwright"])
                if r.returncode == 0:
                    pw_installed = True
                    click.echo("    [OK] playwright 安装完成")
        if pw_installed and not chromium_present:
            do_chrome = click.confirm("    现在下载 Chromium? (~150 MB)", default=True)
            if do_chrome:
                import subprocess
                click.echo("    python -m playwright install chromium ...")
                click.echo("    ⏳ 下载约 150 MB，需要 1–3 分钟。看到进度条/'Downloading' 字样就是正常的，")
                click.echo("       请耐心等待 — 不要 Ctrl+C 中断。")
                r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
                if r.returncode == 0:
                    click.echo("    [OK] Chromium 下载完成")
                else:
                    click.echo("    [!!] 下载失败 — 可能是网络问题。可以重新运行 `python -m scraper init` 重试，")
                    click.echo("         或先解决网络后手动跑 `python -m playwright install chromium`。")

    # ---- Step 4: validate ---------------------------------------------------
    click.echo()
    click.echo("[4/4] 体检")
    # init may have just created config.yaml; the cfg the group loaded at startup
    # is now stale, so reload before doctor runs against it.
    ctx.obj = load_config("config.yaml")
    try:
        ctx.invoke(doctor)
    except SystemExit:
        # doctor exits non-zero if it found problems — that's its job. Don't let it
        # abort init's "next steps" message below.
        pass

    click.echo()
    click.echo("=== 完成！下一步 ===")
    click.echo("  采集示例:  python -m scraper interactive")
    click.echo("  或一键采:  python -m scraper scrape \"电动车\" --count 10")
    click.echo("  查看现状:  python -m scraper status")


def _set_cookie_in_config(config_path: Path, platform: str, cookie: str) -> None:
    """In-place edit `<platform>.cookie:` line in YAML without pulling in PyYAML.

    Simple string replace is safer here than a YAML roundtrip — preserves comments,
    spacing, and avoids accidentally reformatting the user's file.
    """
    text = config_path.read_text(encoding="utf-8")
    import re
    # Match either empty quotes or any existing value, on the line immediately
    # after a `<platform>:` block opener.
    pattern = re.compile(
        rf'({re.escape(platform)}:\s*\n(?:\s*#.*\n)*\s*cookie:\s*)"[^"]*"',
        re.MULTILINE,
    )
    new_text, n = pattern.subn(rf'\1"{cookie}"', text, count=1)
    if n == 0:
        # Fallback for unusual config shapes — append a stanza.
        new_text = text.rstrip() + f'\n\n{platform}:\n  cookie: "{cookie}"\n'
    config_path.write_text(new_text, encoding="utf-8")


def _check_chromium_present() -> bool:
    """Best-effort check for an installed Playwright Chromium without invoking it."""
    candidates = [
        Path.home() / "AppData" / "Local" / "ms-playwright",  # Windows
        Path.home() / ".cache" / "ms-playwright",             # Linux
        Path.home() / "Library" / "Caches" / "ms-playwright", # macOS
    ]
    for base in candidates:
        if base.exists() and any(p.name.startswith("chromium") for p in base.iterdir()):
            return True
    return False


# Per-platform login config. URL is where we open the browser; cookie_domain filters
# the captured cookie jar; auth_marker is the cookie name that indicates a successful
# login (used to warn the user "you didn't actually log in" before we save).
_LOGIN_TARGETS = {
    "zhihu": {
        "url": "https://www.zhihu.com/signin",
        "cookie_domain": ".zhihu.com",
        "auth_marker": "z_c0",
    },
    "bilibili": {
        "url": "https://passport.bilibili.com/login",
        "cookie_domain": ".bilibili.com",
        "auth_marker": "SESSDATA",
    },
    "weibo": {
        "url": "https://passport.weibo.cn/signin/login",
        "cookie_domain": ".weibo.cn",
        "auth_marker": "SUB",
    },
    "tieba": {
        "url": "https://wappass.baidu.com/passport/?login",
        "cookie_domain": ".baidu.com",
        "auth_marker": "BDUSS",
    },
}


@cli.command("login")
@click.argument("platform", type=click.Choice(list(_LOGIN_TARGETS), case_sensitive=False))
def login(platform: str) -> None:
    """Open a browser, let the user log in, capture cookies into config.yaml.

    Replaces the F12-copy-paste cookie flow. Requires Playwright + Chromium
    (install via `python -m scraper init` if you haven't).
    """
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    target = _LOGIN_TARGETS[platform]
    config_path = Path("config.yaml")
    if not config_path.exists():
        example_path = Path("config.example.yaml")
        if not example_path.exists():
            click.echo("[!!] 找不到 config.yaml 或 config.example.yaml — 请在项目根目录运行。")
            return
        import shutil
        shutil.copy(example_path, config_path)
        click.echo(f"[OK] 已从模板创建 config.yaml")

    click.echo(f"=== 浏览器登录：{platform} ===")
    click.echo("即将打开浏览器窗口。请在浏览器里完成登录（账号密码 / 手机验证码 / 扫码均可），")
    click.echo("登录成功后回到这里按 Enter，我会自动把 cookie 写进 config.yaml。")
    click.echo()

    profile_dir = f"data/browser-profile/{platform}"
    try:
        cookie_str = asyncio.run(_browser_login(
            target["url"], target["cookie_domain"], profile_dir,
        ))
    except ImportError:
        click.echo("[!!] playwright 未安装。请先运行：python -m scraper init")
        return
    except Exception as e:
        click.echo(f"[!!] 浏览器登录失败：{type(e).__name__}: {e}")
        return

    if not cookie_str:
        click.echo("[!!] 没有捕获到任何 cookie。请确认你在浏览器里完成了登录后再按 Enter。")
        return

    marker = target["auth_marker"]
    if marker not in cookie_str:
        if not click.confirm(
            f"    [!]  cookie 里没看到 {marker}（登录令牌）— 仍然保存吗？",
            default=False,
        ):
            click.echo("    已取消保存。")
            return

    _set_cookie_in_config(config_path, platform, cookie_str)
    click.echo(f"[OK] {platform} cookie 已写入 config.yaml ({len(cookie_str)} 字符)")
    click.echo("下一步：python -m scraper interactive")


async def _browser_login(url: str, cookie_domain: str, profile_dir: str) -> str:
    """Open a visible browser to `url`, wait for the user to confirm login, return cookie string.

    Uses a persistent context at `profile_dir` so the login state survives — subsequent
    scrape runs that point at the same `user_data_dir` will reuse this session without
    re-prompting the user. The cookie string is still extracted + written to config.yaml
    so the httpx-only paths (search, columns) work too.

    Returns a `name=value; name=value; ...` string filtered to `cookie_domain` (and its
    subdomains, since e.g. Zhihu sets cookies on both `.zhihu.com` and `www.zhihu.com`).
    """
    from playwright.async_api import async_playwright

    domain_suffix = cookie_domain.lstrip(".")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception:
            # Network hiccups during goto shouldn't kill the flow — user can manually
            # navigate in the visible window.
            pass

        # Block on stdin in a thread so the playwright event loop keeps the browser
        # responsive while we wait. (click.prompt() would freeze the whole loop.)
        await asyncio.to_thread(
            input,
            "登录完成后回到此终端按 Enter 继续... ",
        )

        cookies = await context.cookies()
        await context.close()

    pairs: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if not (domain == domain_suffix or domain.endswith("." + domain_suffix)):
            continue
        name = c.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={c.get('value', '')}")
    return "; ".join(pairs)


@cli.command("status")
@click.pass_obj
def status(cfg: Config) -> None:
    """Quick health snapshot — what's in the DB, last runs, cookie state.

    Reads only — no network probes — so it always returns in well under a second.
    Run this before sitting down to a notebook to see what you have without
    grepping logs or writing SQL.
    """
    import sqlite3
    from datetime import datetime

    # Force UTF-8 stdout on Windows so Chinese keywords and the status glyphs
    # don't trip cp936/GBK encoding on redirected output.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    db_path = Path(cfg.storage.path)
    if not db_path.exists():
        click.echo(f"DB not found at {db_path}. Run a scrape first.")
        return

    db = sqlite3.connect(str(db_path))
    size_mb = db_path.stat().st_size / 1_048_576

    click.echo(f"=== Scraper Status — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    click.echo()
    click.echo(f"DB:      {db_path}")
    click.echo(f"Size:    {size_mb:.2f} MB")
    click.echo()

    # Row counts for the content tables that actually matter.
    click.echo("--- Tables ---")
    tables = [
        "bili_videos", "bili_comments", "bili_dynamics",
        "zhihu_questions", "zhihu_answers", "zhihu_answer_comments",
        "zhihu_columns", "zhihu_articles",
        "weibo_posts", "weibo_comments", "tieba_threads", "tieba_comments",
        "keyword_posts", "scrape_runs", "engagement_snapshots",
    ]
    for t in tables:
        try:
            n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n > 0:
                click.echo(f"  {t:<24} {n:>8,}")
        except sqlite3.OperationalError:
            continue  # table doesn't exist on older DBs
    click.echo()

    # Distinct keywords scraped + total post coverage
    try:
        kw_count = db.execute(
            "SELECT COUNT(DISTINCT keyword) FROM keyword_posts"
        ).fetchone()[0]
        post_count = db.execute(
            "SELECT COUNT(DISTINCT post_id) FROM keyword_posts"
        ).fetchone()[0]
        click.echo(f"Keywords scraped: {kw_count}")
        click.echo(f"Unique posts linked to keywords: {post_count:,}")
    except sqlite3.OperationalError:
        pass

    # Top keywords by post count
    try:
        rows = db.execute("""
            SELECT keyword, COUNT(*) FROM keyword_posts
            GROUP BY keyword ORDER BY 2 DESC LIMIT 5
        """).fetchall()
        if rows:
            click.echo()
            click.echo("--- Top keywords by posts collected ---")
            for kw, n in rows:
                click.echo(f"  {kw:<30} {n:>4}")
    except sqlite3.OperationalError:
        pass

    # Recent scrape runs
    try:
        rows = db.execute("""
            SELECT run_id, keyword, platform, posts_fetched, comments_fetched,
                   datetime(started_at,'unixepoch','localtime'),
                   COALESCE(finished_at - started_at, 0),
                   error
            FROM scrape_runs ORDER BY run_id DESC LIMIT 5
        """).fetchall()
        if rows:
            click.echo()
            click.echo("--- Recent scrape runs ---")
            for rid, kw, plat, posts, comments, started, dur, err in rows:
                ok = "[!!]" if err else "[OK]"
                click.echo(
                    f"  {ok} #{rid:<3} {plat:<6} {kw:<20} "
                    f"posts={posts or 0:<3} comments={comments or 0:<3} "
                    f"{dur}s @ {started}"
                )
                if err:
                    click.echo(f"      error: {err[:120]}")
    except sqlite3.OperationalError:
        pass

    # Exports folder size
    exports = Path("data/exports")
    if exports.exists():
        files = list(exports.iterdir())
        total = sum(f.stat().st_size for f in files)
        click.echo()
        click.echo(f"Exports: {len(files)} CSVs, {total/1024:.1f} KB at data/exports/")

    # Cookie state — passive check (don't probe network)
    click.echo()
    click.echo("--- Cookies (configured?) ---")
    bili_cookie = getattr(cfg.bilibili, "cookie", "") or ""
    zhihu_cookie = getattr(cfg.zhihu, "cookie", "") or ""
    weibo_cookie = getattr(cfg.weibo, "cookie", "") or ""
    tieba_cookie = getattr(cfg.tieba, "cookie", "") or ""
    click.echo(f"  bilibili: {'set' if bili_cookie else 'empty'} ({len(bili_cookie)} chars)")
    click.echo(f"  zhihu:    {'set' if zhihu_cookie else 'empty'} ({len(zhihu_cookie)} chars)")
    click.echo(f"  weibo:    {'set' if weibo_cookie else 'empty'} ({len(weibo_cookie)} chars)  (匿名也能搜)")
    click.echo(f"  tieba:    {'set' if tieba_cookie else 'empty'} ({len(tieba_cookie)} chars)  (匿名也能搜)")
    if zhihu_cookie and "z_c0=" not in zhihu_cookie:
        click.echo("    [!] no z_c0 token — most Zhihu endpoints will reject this cookie")

    db.close()


# Order matters: more specific (answer URL with /answer/ID) must match before
# the generic /question/<id> pattern.
_GRAB_URL_PATTERNS: list[tuple[Any, str]] = [
    (re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", re.IGNORECASE), "bili_video"),
    (re.compile(r"zhihu\.com/question/(\d+)/answer/(\d+)"), "zhihu_answer"),
    (re.compile(r"zhihu\.com/question/(\d+)"), "zhihu_question"),
    (re.compile(r"zhuanlan\.zhihu\.com/p/(\d+)"), "zhihu_article"),
    (re.compile(r"zhuanlan\.zhihu\.com/(c_[0-9a-zA-Z]+)"), "zhihu_column"),
    # Weibo: numeric id (m.weibo.cn/detail|status/<id>) or base-62 bid (weibo.com/<uid>/<bid>).
    (re.compile(r"(?:m\.)?weibo\.c(?:n|om)/(?:detail|status|u/status)/(\d+)"), "weibo_post"),
    (re.compile(r"weibo\.com/\d+/([A-Za-z][A-Za-z0-9]+)"), "weibo_post"),
    (re.compile(r"tieba\.baidu\.com/p/(\d+)"), "tieba_thread"),
]


def _expand_short_url(url: str) -> str:
    """Follow b23.tv (and similar) redirects to the canonical URL. Returns the
    input unchanged on any failure — caller will then fail the regex and report
    'unrecognized URL' instead of a network error."""
    if "b23.tv" not in url:
        return url
    try:
        from urllib.request import Request, urlopen
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as r:  # noqa: S310 — user-supplied URL, intentional
            return r.url or url
    except Exception:
        return url


@cli.command("sentiment")
@click.argument("keyword")
@click.option("--top", "top_k", default=20, show_default=True, help="热词数量 (TF-IDF Top-N).")
@click.option("--by-day/--no-by-day", "by_day", default=False, show_default=True,
              help="额外按发布日期打印情感趋势。")
@click.option("--sources", type=click.Choice(["all", "posts", "comments"]), default="all",
              show_default=True, help="分析哪些文本：all=帖子+回答+评论, posts=帖子+回答, comments=仅评论。")
@click.pass_obj
def sentiment_command(cfg: Config, keyword: str, top_k: int, by_day: bool, sources: str) -> None:
    """情感分析 — 对某关键词的帖子/回答/评论做中文情感打分 + 热词提取。

    纯本地分析（不抓取），读 data 里已有的数据。先 `scrape "<关键词>"` 再跑这个。
    """
    _ensure_utf8_streams()
    src_map = {
        "all": ("posts", "answers", "comments"),
        "posts": ("posts", "answers"),
        "comments": ("comments",),
    }
    src = src_map[sources]
    from scraper.analyze import sentiment_summary, sentiment_by_day, top_keywords

    db_path = cfg.storage.path
    # jieba is a lazy import inside scraper.nlp, so a missing-dependency error
    # surfaces here at call time (not at import) — catch it and give the fix.
    try:
        summary = sentiment_summary(keyword, sources=src, db_path=db_path)
    except ScraperError as e:
        click.echo(f"无法分析: {e}")
        click.echo("提示: 情感分析需要 jieba — 跑 `pip install jieba`")
        sys.exit(1)
    if summary.empty:
        click.echo(f"没有可分析的文本。先跑一次: python -m scraper scrape \"{keyword}\"")
        sys.exit(1)
    row = summary.iloc[0]

    click.echo("=" * 50)
    click.echo(f"《{keyword}》情感分析  (来源: {sources})")
    click.echo("=" * 50)
    n = int(row["样本数"])
    pos, neu, neg = int(row["正面"]), int(row["中性"]), int(row["负面"])

    def _bar(count: int) -> str:
        width = round((count / n) * 30) if n else 0
        return "█" * width

    click.echo(f"样本数: {n}")
    click.echo(f"  正面 {pos:>5} ({pos / n:5.1%})  {_bar(pos)}")
    click.echo(f"  中性 {neu:>5} ({neu / n:5.1%})  {_bar(neu)}")
    click.echo(f"  负面 {neg:>5} ({neg / n:5.1%})  {_bar(neg)}")
    click.echo(f"平均情感得分: {row['平均情感得分']}  →  整体倾向: {row['情感倾向']}")

    kw = top_keywords(keyword, topK=top_k, sources=src, db_path=db_path)
    if not kw.empty:
        click.echo("-" * 50)
        click.echo(f"热词 Top {len(kw)}:")
        terms = "  ".join(f"{r['词']}({r['权重']:.2f})" for _, r in kw.iterrows())
        click.echo(f"  {terms}")

    if by_day:
        daily = sentiment_by_day(keyword, sources=src, db_path=db_path)
        if not daily.empty:
            click.echo("-" * 50)
            click.echo("按发布日期:")
            for _, r in daily.iterrows():
                click.echo(
                    f"  {r['日期']}  样本{int(r['样本数']):>4}  "
                    f"均分{r['平均情感得分']:>7}  "
                    f"(正{int(r['正面'])}/中{int(r['中性'])}/负{int(r['负面'])})"
                )
    click.echo("=" * 50)
    click.echo(f"提示: 跑 `python -m scraper report \"{keyword}\"` 可生成含情感图表的 HTML 报告。")


@cli.command("report")
@click.argument("keyword")
@click.option(
    "--days", type=int, default=None,
    help="Only include posts published within the last N days (default: all).",
)
@click.option(
    "--open/--no-open", "open_after", default=True, show_default=True,
    help="Open the generated report in your default browser when done.",
)
@click.pass_obj
def report_command(cfg: Config, keyword: str, days: Optional[int], open_after: bool) -> None:
    """Generate a self-contained HTML report for one keyword.

    Reads from data/scraped.db. Writes to data/reports/<keyword>_<date>.html.
    The HTML embeds all charts as base64 PNG — no external assets, can be emailed.

    Example:
      python -m scraper report "电动车"
      python -m scraper report "电动车" --days 7
    """
    _ensure_utf8_streams()
    from scraper.report import build_report

    try:
        path = build_report(keyword, days=days, db_path=cfg.storage.path)
    except Exception as e:
        click.echo(f"[!!] 报告生成失败: {type(e).__name__}: {e}")
        sys.exit(1)
    click.echo(f"[OK] 报告已生成: {path}")
    if open_after:
        import webbrowser
        webbrowser.open(path.as_uri())


@cli.command("grab")
@click.argument("url")
@click.option(
    "--comments-pages", default=1, show_default=True,
    help="B站: comment pages to fetch alongside the video (0 to skip).",
)
@click.option(
    "--answers", default=10, show_default=True,
    help="子内容数量: 知乎=回答数, 微博=评论数, 贴吧=楼层数。",
)
@click.pass_obj
def grab_command(cfg: Config, url: str, comments_pages: int, answers: int) -> None:
    """Grab one post from a B站 / 知乎 / 微博 / 贴吧 URL — paste the link and go.

    Supported URLs:
      B站 视频:  https://www.bilibili.com/video/BV1xx...  (b23.tv 短链会自动跳转)
      知乎 问题: https://www.zhihu.com/question/<id>
      知乎 回答: https://www.zhihu.com/question/<qid>/answer/<aid>
      知乎 文章: https://zhuanlan.zhihu.com/p/<id>
      知乎 专栏: https://zhuanlan.zhihu.com/<column_id>
      微博:      https://m.weibo.cn/detail/<id>  或  https://weibo.com/<uid>/<bid>
      贴吧 帖子: https://tieba.baidu.com/p/<tid>
    """
    _ensure_utf8_streams()

    url = _expand_short_url(url.strip())

    matched: Optional[tuple[str, tuple[str, ...]]] = None
    for pat, kind in _GRAB_URL_PATTERNS:
        m = pat.search(url)
        if m:
            matched = (kind, m.groups())
            break
    if matched is None:
        click.echo(f"无法识别 URL: {url!r}")
        click.echo("支持: B站视频 / 知乎问题、回答、专栏、文章。看 `grab --help` 列出格式。")
        sys.exit(1)

    kind, groups = matched

    async def run() -> None:
        storage = await _open_storage(cfg)
        try:
            if kind == "bili_video":
                bvid = groups[0]
                async with BilibiliScraper(cfg, storage) as s:
                    v = await s.get_video(bvid, with_tags=True)
                    click.echo(f"[B站] ✓ {v.bvid}\t{v.title}")
                    click.echo(
                        f"  UP: {v.owner.get('name')}  "
                        f"播放={v.stat.get('view')} 点赞={v.stat.get('like')} 评论={v.stat.get('reply')}"
                    )
                    if comments_pages > 0:
                        n = 0
                        async for _ in s.iter_comments(bvid, pages=comments_pages):
                            n += 1
                        click.echo(f"  评论已抓 {n} 条")
            elif kind in ("zhihu_question", "zhihu_answer"):
                qid = int(groups[0])
                from scraper.core.browser import BrowserSession
                async with ZhihuScraper(cfg, storage) as s:
                    s._require_cookie()
                    async with BrowserSession(
                        rate=s._rate, cookie=s._cookie,
                        cookie_domain=".zhihu.com",
                        user_data_dir="data/browser-profile/zhihu",
                    ) as browser:
                        ans_ids = await s._fetch_question_via_browser(
                            browser, qid, max_answers=answers,
                        )
                    extra = ""
                    if kind == "zhihu_answer":
                        extra = f"  (含目标回答 {groups[1]})" if int(groups[1]) in ans_ids else f"  (目标回答 {groups[1]} 不在首屏)"
                    click.echo(f"[知乎] ✓ question {qid}  回答 {len(ans_ids)}{extra}")
            elif kind == "zhihu_article":
                article_id = int(groups[0])
                async with ZhihuScraper(cfg, storage) as s:
                    try:
                        art = await s.get_article(article_id)
                        click.echo(f"[知乎] ✓ article {article_id}\t{art.get('title')}")
                    except ScraperError as e:
                        # Article endpoint needs x-zse-96 signing we don't implement —
                        # surface the failure clearly instead of swallowing it.
                        click.echo(f"[知乎] 文章 {article_id} 抓取失败 (端点需要签名): {e}")
            elif kind == "zhihu_column":
                column_id = groups[0]
                async with ZhihuScraper(cfg, storage) as s:
                    col = await s.get_column(column_id)
                    click.echo(f"[知乎] ✓ column {column_id}\t{col.get('title')}")
            elif kind == "weibo_post":
                wid = groups[0]
                async with WeiboScraper(cfg, storage) as s:
                    post, n = await s.get_status(wid, with_comments=answers)
                    text = (post.text or "").replace("\n", " ")[:120]
                    click.echo(f"[微博] ✓ {post.id}  {post.user_name}: {text}")
                    click.echo(
                        f"  转发={post.reposts_count} 评论={post.comments_count} "
                        f"赞={post.attitudes_count}" + (f"  已抓评论 {n}" if n else "")
                    )
            elif kind == "tieba_thread":
                tid = groups[0]
                async with TiebaScraper(cfg, storage) as s:
                    kept = await s.get_thread(tid, max_replies=answers)
                    click.echo(f"[贴吧] ✓ thread {tid}  已抓楼层 {kept}")
        finally:
            await storage.close()

    asyncio.run(run())


@cli.command("last")
@click.option("-n", "limit", default=10, show_default=True, help="How many recent posts to list.")
@click.option(
    "--platform",
    type=click.Choice(["bili", "zhihu", "weibo", "tieba", "all"], case_sensitive=False),
    default="all",
    show_default=True,
)
@click.pass_obj
def last_command(cfg: Config, limit: int, platform: str) -> None:
    """Show the N posts most recently linked into a keyword scrape.

    Closes the loop after a scrape — copy an ID into `view` or `open` without
    grepping CSVs or logs.
    """
    import sqlite3
    from datetime import datetime

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    db_path = Path(cfg.storage.path)
    if not db_path.exists():
        click.echo(f"DB not found at {db_path}. Run a scrape first.")
        return
    db = sqlite3.connect(str(db_path))

    # Join keyword_posts → posts_unified for the title; left join so missing rows
    # (e.g. failed fetch where only the keyword_posts stub got written) still appear.
    sql = """
        SELECT kp.platform, kp.post_id, pu.标题 AS title, kp.keyword,
               kp.fetched_at
        FROM keyword_posts kp
        LEFT JOIN posts_unified pu
          ON kp.post_id = pu.帖子ID
         AND ((kp.platform = 'bili' AND pu.平台 = 'bili')
           OR (kp.platform = 'zhihu' AND pu.平台 = 'zhihu_question')
           OR (kp.platform = 'weibo' AND pu.平台 = 'weibo')
           OR (kp.platform = 'tieba' AND pu.平台 = 'tieba'))
    """
    params: list = []
    if platform.lower() != "all":
        sql += " WHERE kp.platform = ?"
        params.append(platform.lower())
    sql += " ORDER BY kp.fetched_at DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    if not rows:
        click.echo("(no posts yet — run a scrape first)")
        db.close()
        return

    click.echo(f"=== Last {len(rows)} posts ===")
    click.echo()
    for plat, pid, title, kw, ts in rows:
        when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
        head = {"bili": "B", "zhihu": "Z", "weibo": "W", "tieba": "T"}.get(plat, "?")
        click.echo(f"  [{head}] {pid:<14} {(title or '(标题未抓到)')[:40]:<40}  kw={kw}  {when}")
    click.echo()
    click.echo("Tip: `python -m scraper view <video|question|answer> <ID>` 查看详情")
    click.echo("     `python -m scraper open <ID>` 用浏览器打开原始页面")
    db.close()


@cli.command("open")
@click.argument("post_id")
def open_command(post_id: str) -> None:
    """Open the original B站/知乎 page for a post_id in your default browser.

    Infers the platform from the ID shape: `BV…` / `BV1…` → B站 video,
    pure numeric → 知乎 question. No DB lookup needed.
    """
    import webbrowser

    pid = post_id.strip()
    url: Optional[str] = None
    if pid.upper().startswith("BV") and len(pid) >= 10:
        url = f"https://www.bilibili.com/video/{pid}"
    elif pid.isdigit():
        url = f"https://www.zhihu.com/question/{pid}"
    if url is None:
        click.echo(
            f"无法识别 ID 形状: {pid!r}\n"
            "支持: BV… (B站视频) / 纯数字 (知乎问题)"
        )
        sys.exit(1)
    click.echo(f"打开: {url}")
    webbrowser.open(url)


@cli.command("view")
@click.argument(
    "kind",
    type=click.Choice(
        ["video", "dynamic", "question", "answer", "article", "column", "comment",
         "weibo", "thread"],
        case_sensitive=False,
    ),
)
@click.argument("post_id")
@click.option("--comments", "show_comments", default=5, show_default=True,
              help="For video/answer: how many top comments to show alongside.")
@click.pass_obj
def view(cfg: Config, kind: str, post_id: str, show_comments: int) -> None:
    """Pretty-print a single record from the DB. No network calls.

    Examples:
      python -m scraper view video BV1xx411c7XW
      python -m scraper view question 536080693
      python -m scraper view answer 2199786648
    """
    import sqlite3

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    db = sqlite3.connect(cfg.storage.path)
    db.row_factory = sqlite3.Row

    table_map = {
        "video":    ("bili_videos",      "bvid"),
        "dynamic":  ("bili_dynamics",    "dynamic_id"),
        "question": ("zhihu_questions",  "id"),
        "answer":   ("zhihu_answers",    "id"),
        "article":  ("zhihu_articles",   "id"),
        "column":   ("zhihu_columns",    "id"),
        "comment":  ("zhihu_answer_comments", "comment_id"),
        "weibo":    ("weibo_posts",      "id"),
        "thread":   ("tieba_threads",    "tid"),
    }
    table, pk = table_map[kind.lower()]

    row = db.execute(f"SELECT * FROM {table} WHERE {pk} = ?", (post_id,)).fetchone()
    if row is None:
        click.echo(f"No {kind} found with {pk}={post_id} in {table}.")
        click.echo(f"Tip: have you scraped this post yet? Try `python -m scraper status` to see what's in your DB.")
        return

    d = dict(row)
    click.echo(f"=== {kind.upper()} {post_id} ===")
    click.echo()

    # Order of fields to surface. Anything not in this list is appended at the end.
    priority_fields = [
        "title", "uname", "owner_name", "author_name", "author_id",
        "pubdate", "created", "created_time", "ctime", "pub_ts",
        "view", "like", "voteup_count", "like_count",
        "coin", "favorite", "reply", "comment_count", "answer_count",
        "share", "forward_count", "follower_count", "view_count", "danmaku",
        "duration", "tags", "topics", "type",
        "desc", "detail", "text", "content", "message", "excerpt",
        "fetched_at",
    ]
    shown = set()
    for f in priority_fields:
        if f in d and d[f] not in (None, ""):
            _print_field(f, d[f])
            shown.add(f)
    # Spill any remaining fields except raw
    for f, v in d.items():
        if f in shown or f == "raw" or v in (None, ""):
            continue
        _print_field(f, v)

    # Related comments for video/answer
    if kind == "video" and show_comments > 0:
        # bili_comments key by oid (the aid), not bvid
        aid = d.get("aid")
        if aid:
            comms = db.execute(
                "SELECT uname, message, likes FROM bili_comments "
                "WHERE oid = ? ORDER BY likes DESC NULLS LAST LIMIT ?",
                (aid, show_comments),
            ).fetchall()
            if comms:
                click.echo()
                click.echo(f"--- Top {len(comms)} comments ---")
                for c in comms:
                    click.echo(f"  [{c['likes'] or 0:>4}] {c['uname']}: {(c['message'] or '')[:160]}")
    elif kind == "answer" and show_comments > 0:
        comms = db.execute(
            "SELECT author_name, content, like_count FROM zhihu_answer_comments "
            "WHERE answer_id = ? ORDER BY like_count DESC NULLS LAST LIMIT ?",
            (post_id, show_comments),
        ).fetchall()
        if comms:
            click.echo()
            click.echo(f"--- Top {len(comms)} comments ---")
            for c in comms:
                click.echo(f"  [{c['like_count'] or 0:>4}] {c['author_name']}: {(c['content'] or '')[:160]}")
    elif kind == "weibo" and show_comments > 0:
        comms = db.execute(
            "SELECT user_name, text, like_count FROM weibo_comments "
            "WHERE post_id = ? ORDER BY like_count DESC NULLS LAST LIMIT ?",
            (post_id, show_comments),
        ).fetchall()
        if comms:
            click.echo()
            click.echo(f"--- Top {len(comms)} comments ---")
            for c in comms:
                click.echo(f"  [{c['like_count'] or 0:>4}] {c['user_name']}: {(c['text'] or '')[:160]}")
    elif kind == "thread" and show_comments > 0:
        comms = db.execute(
            "SELECT author_name, content, floor FROM tieba_comments "
            "WHERE tid = ? ORDER BY floor LIMIT ?",
            (post_id, show_comments),
        ).fetchall()
        if comms:
            click.echo()
            click.echo(f"--- {len(comms)} reply floors ---")
            for c in comms:
                click.echo(f"  [{c['floor'] or 0:>3}F] {c['author_name']}: {(c['content'] or '')[:160]}")

    db.close()


def _print_field(name: str, value: Any) -> None:
    """One row of `key: value`, content fields wrapped, timestamps humanized."""
    import textwrap
    from datetime import datetime

    label_w = 16
    # Humanize unix timestamps
    if name in ("pubdate", "created", "created_time", "ctime", "pub_ts", "fetched_at", "updated_time", "updated", "created_ts"):
        try:
            ts = int(value)
            value = f"{ts} ({datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')})"
        except (TypeError, ValueError):
            pass
    s = str(value)
    if len(s) > 80:
        wrapped = textwrap.fill(
            s, width=100,
            initial_indent=f"  {name:<{label_w}}: ",
            subsequent_indent="  " + " " * (label_w + 2),
        )
        click.echo(wrapped)
    else:
        click.echo(f"  {name:<{label_w}}: {s}")


@cli.command("interactive")
@click.pass_context
def interactive(ctx: click.Context) -> None:
    """与不带参数运行 `python -m scraper` 等价 — 进入引导式交互循环。

    保留这个显式命令名是为了方便：(a) 在脚本/文档里写得更清楚，
    (b) 用户从 --help 输出里能发现这个模式。日常使用直接跑
    `python -m scraper` 就行，不用每次都打 `interactive`。

    Loops: after each scrape, asks for another keyword. Enter on empty input
    (or 'q') exits. Settings (platforms, count, comments) are remembered across
    iterations — only the keyword changes by default; type 's' to change settings.

    If config.yaml is missing or the Zhihu cookie isn't set, you'll be offered
    `init` and `login` inline — no separate command needed.
    """
    # Windows consoles often run cp936 (GBK) for Chinese stdin/stdout. Force UTF-8
    # so typed/pasted Chinese keywords decode cleanly; without this you get
    # surrogate characters that fail SQLite's UTF-8 encode downstream.
    if sys.platform == "win32":
        try:
            sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    click.echo("=== 爬虫工具 — guided scrape ===")
    click.echo("(关键词回车开始爬，空回车或 q 退出，s 修改设置)\n")

    cfg = ctx.obj
    # Self-heal: first-run config, then offer browser login for missing Zhihu cookie.
    cfg = _ensure_ready(cfg)
    ctx.obj = cfg

    # Initial settings — asked once, reused across the loop until user types 's'.
    settings = _prompt_scrape_settings(cfg, defaults=None)

    last_keyword = ""  # carries forward so user can press Enter to re-scrape it
    while True:
        if last_keyword:
            prompt_label = (
                f"\nKeyword 关键词 (回车 = 再爬一次 “{last_keyword}” / q 退出 / s 改设置)"
            )
        else:
            prompt_label = "\nKeyword 关键词 (回车退出 / s 改设置)"
        try:
            raw = click.prompt(prompt_label, type=str, default="", show_default=False)
        except click.Abort:
            click.echo("\n再见。")
            return
        keyword = raw.strip().encode("utf-8", "ignore").decode("utf-8", "ignore")

        if keyword.lower() in {"q", "quit", "exit"}:
            click.echo("再见。")
            return

        # Empty input: re-run last keyword if we have one, otherwise quit.
        if not keyword:
            if last_keyword:
                keyword = last_keyword
            else:
                click.echo("再见。")
                return

        if keyword.lower() in {"s", "settings", "设置"}:
            settings = _prompt_scrape_settings(cfg, defaults=settings)
            continue

        # Mid-loop self-heal: user picked a Zhihu-including platform but the cookie
        # is empty — offer to launch browser login without restarting interactive.
        if "zhihu" in settings["platforms_csv"].split(","):
            cfg = _maybe_offer_zhihu_login(cfg, soft=True)
            ctx.obj = cfg

        _run_one_keyword(cfg, keyword, settings)
        last_keyword = keyword


def _ensure_ready(cfg: Config) -> Config:
    """Block-and-fix any setup gaps before the loop starts.

    Returns the (possibly reloaded) config. Reloading matters because `init` /
    browser login both rewrite config.yaml, and the cfg loaded at startup is now
    stale.
    """
    config_path = Path("config.yaml")
    if not config_path.exists():
        click.echo("[!] config.yaml 不存在。")
        if click.confirm("    现在运行 init 向导? (会创建 config.yaml + 提示登录 Zhihu)", default=True):
            ctx = click.get_current_context()
            try:
                ctx.invoke(init)
            except SystemExit:
                pass
            cfg = load_config("config.yaml")
        else:
            click.echo("    继续，但只能用 Bilibili。")
            return cfg

    cfg = _maybe_offer_zhihu_login(cfg, soft=False)
    return cfg


def _maybe_offer_zhihu_login(cfg: Config, *, soft: bool) -> Config:
    """If Zhihu cookie is empty, offer to launch browser login.

    `soft=True`: only offer; defaults to no (mid-loop, user already started typing).
    `soft=False`: at startup; default yes.
    """
    zhihu_cookie = getattr(cfg.zhihu, "cookie", "") or ""
    if zhihu_cookie and "z_c0=" in zhihu_cookie:
        return cfg
    if zhihu_cookie:
        click.echo("[!] Zhihu cookie 里没有 z_c0 登录令牌，知乎接口会被拒绝。")
    else:
        click.echo("[!] Zhihu cookie 未配置 — 只能爬 Bilibili。")

    default = not soft
    if not click.confirm("    现在打开浏览器登录 Zhihu? (推荐 — 比手动粘贴 cookie 简单)", default=default):
        click.echo("    跳过 — 知乎部分将被忽略。")
        return cfg

    ctx = click.get_current_context()
    try:
        ctx.invoke(login, platform="zhihu")
    except SystemExit:
        pass
    return load_config("config.yaml")


def _prompt_scrape_settings(cfg: Config, defaults: Optional[dict]) -> dict:
    """Ask the user for scrape options. `defaults` carries the prior iteration's choices."""
    d = defaults or {
        "count": 10,
        "platforms_csv": "bili,zhihu,weibo,tieba",
        "comments_pages": 1,
        "answers_per_q": 5,
        "comments_per_answer": 0,
        "weibo_comments": 0,
        "tieba_replies": 0,
        "only_new": False,
        "write_csv": True,
        "write_report": False,
    }

    count = click.prompt("Posts per platform (帖子数)", type=int, default=d["count"])

    # Free-text comma list validated against the registry; 'all' selects every platform.
    while True:
        raw = click.prompt(
            "Platforms (逗号分隔 bili,zhihu,weibo,tieba — 或 all)",
            type=str, default=d["platforms_csv"],
        )
        sel = raw.strip().lower()
        if sel in ("all", "both"):
            platforms_csv = ",".join(_PLATFORMS)
            break
        tokens = [t.strip() for t in sel.split(",") if t.strip()]
        unknown = [t for t in tokens if t not in _PLATFORMS]
        if tokens and not unknown:
            platforms_csv = ",".join(tokens)
            break
        click.echo(f"  无法识别: {', '.join(unknown) or '(空)'}；可选 {', '.join(_PLATFORMS)}")

    plats = set(platforms_csv.split(","))
    comments_pages = d["comments_pages"]
    answers_per_q = d["answers_per_q"]
    comments_per_answer = d["comments_per_answer"]
    weibo_comments = d.get("weibo_comments", 0)
    tieba_replies = d.get("tieba_replies", 0)
    if click.confirm("Include comments per post? (slower)", default=True):
        if "bili" in plats:
            comments_pages = click.prompt("  Bilibili comment pages", type=int, default=comments_pages)
        if "zhihu" in plats:
            answers_per_q = click.prompt("  Zhihu answers per question", type=int, default=answers_per_q)
            if click.confirm(
                "  Also fetch per-answer comments? (slow)",
                default=comments_per_answer > 0,
            ):
                comments_per_answer = click.prompt(
                    "    Per-answer comment count", type=int, default=comments_per_answer or 10,
                )
            else:
                comments_per_answer = 0
        if "weibo" in plats:
            weibo_comments = click.prompt("  Weibo comments per post", type=int, default=weibo_comments or 10)
        if "tieba" in plats:
            tieba_replies = click.prompt("  Tieba reply floors per thread", type=int, default=tieba_replies or 10)

    only_new = click.confirm(
        "Only fetch posts not seen for this keyword before? (跳过已抓过的，省配额)",
        default=d.get("only_new", False),
    )
    write_csv = click.confirm("Export results to CSV when done?", default=d["write_csv"])
    write_report = click.confirm(
        "Generate HTML analysis report after scrape? (含图表，自动打开)",
        default=d.get("write_report", False),
    )

    return {
        "count": count,
        "platforms_csv": platforms_csv,
        "comments_pages": comments_pages,
        "answers_per_q": answers_per_q,
        "comments_per_answer": comments_per_answer,
        "weibo_comments": weibo_comments,
        "tieba_replies": tieba_replies,
        "only_new": only_new,
        "write_csv": write_csv,
        "write_report": write_report,
    }


def _run_one_keyword(cfg: Config, keyword: str, settings: dict) -> None:
    """Single scrape iteration — broken out of `interactive` so the loop body stays small."""
    platforms_csv = settings["platforms_csv"]
    write_csv = settings["write_csv"]
    write_report = settings.get("write_report", False)
    only_new = settings.get("only_new", False)
    opts = {
        "count": settings["count"],
        "comments_pages": settings["comments_pages"],
        "answers_per_q": settings["answers_per_q"],
        "comments_per_answer": settings["comments_per_answer"],
        "weibo_comments": settings.get("weibo_comments", 0),
        "tieba_replies": settings.get("tieba_replies", 0),
        "only_new": only_new,
    }
    wanted = {p.strip() for p in platforms_csv.split(",") if p.strip()}

    click.echo()
    click.echo(
        f"Running: scrape {keyword!r} count={opts['count']} platforms={platforms_csv} "
        f"only_new={only_new} csv={write_csv}"
    )
    click.echo()

    run_ids: list[int] = []

    async def run() -> None:
        storage = await _open_storage(cfg)

        async def run_platform(key: str) -> None:
            spec = _PLATFORMS[key]
            try:
                async with spec.scraper_cls(cfg, storage) as s:
                    click.echo(f"--- {key}: {keyword} ---")
                    with _ScrapeProgress(label=f"{spec.label} · {keyword}") as prog:
                        summary = await s.scrape_keyword(
                            keyword, **spec.make_kwargs(opts), progress=prog,
                        )
                    _print_scrape_summary(key, summary, no_comments=spec.no_comments(opts))
                    if summary.get("run_id") is not None:
                        run_ids.append(summary["run_id"])
            except ScraperError as e:
                click.echo(f"  [{spec.label}] 跳过：{type(e).__name__}: {e}")

        await asyncio.gather(*[run_platform(k) for k in _PLATFORMS if k in wanted])
        await storage.close()

    asyncio.run(run())
    if write_csv:
        _export_runs_csv(run_ids, cfg.storage.path)
    if write_report:
        _maybe_emit_reports([keyword], cfg.storage.path, do_open=True)

    # Show a quick analysis preview so the user immediately sees their data.
    click.echo()
    click.echo("=== Quick preview (volume_by_day) ===")
    try:
        from scraper.analyze import volume_by_day, scrape_run_summary
        df = volume_by_day(keyword)
        if df.empty:
            click.echo("  (no posts linked to this keyword yet — check the logs above)")
        else:
            click.echo(df.to_string(index=False))
        click.echo()
        click.echo("=== Last scrape run ===")
        runs = scrape_run_summary(keyword=keyword).head(2)
        click.echo(runs[["运行ID", "平台", "实际帖子数", "实际评论数", "结束时间"]].to_string(index=False))
    except Exception as e:
        click.echo(f"  (preview failed: {type(e).__name__}: {e})")


@cli.command("export")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), required=True)
@click.option("--out", "out_path", required=True, type=click.Path())
@click.option("--table", help="Specific table (required for CSV).")
@click.pass_obj
def export(cfg: Config, fmt: str, out_path: str, table: Optional[str]) -> None:
    async def run() -> None:
        storage = await _open_storage(cfg)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tables = [
            "bili_videos",
            "bili_comments",
            "bili_dynamics",
            "bili_search",
            "zhihu_questions",
            "zhihu_answers",
            "zhihu_columns",
            "zhihu_articles",
            "zhihu_search",
            "weibo_posts",
            "weibo_comments",
            "tieba_threads",
            "tieba_comments",
        ]
        if fmt == "json":
            dump: dict = {}
            chosen = [table] if table else tables
            for t in chosen:
                dump[t] = await storage.fetch_all(t)
            out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if not table:
                raise click.UsageError("CSV export requires --table")
            rows = await storage.fetch_all(table)
            if not rows:
                out.write_text("", encoding="utf-8")
            else:
                with out.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
        click.echo(f"wrote {out}")
        await storage.close()

    asyncio.run(run())


if __name__ == "__main__":
    cli()

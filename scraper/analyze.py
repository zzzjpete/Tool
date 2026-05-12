"""ML-facing analysis helpers. Reads from the scraper's SQLite DB and returns
pandas DataFrames with Chinese column names matching the unified views.

Usage from a notebook:

    from scraper.analyze import posts_for_keyword, volume_by_day, comments_for_post

    df = posts_for_keyword("电动车")
    daily = volume_by_day("电动车")
    daily.plot(x="日期", y="帖子数", kind="bar")

All functions accept an optional `db_path` and fall back to config.yaml's
storage.path. pandas is imported lazily so installing the scraper without it
still works.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from scraper.core.config import load_config
from scraper.core.exceptions import ScraperError


def _pandas() -> Any:
    """Lazy import. Keeps pandas optional for users who only run the scraper."""
    try:
        import pandas as pd  # noqa: F401
        return pd
    except ImportError as e:
        raise ScraperError(
            "pandas is not installed. Run: pip install pandas"
        ) from e


def _resolve_db_path(db_path: Optional[str | Path]) -> str:
    if db_path is not None:
        return str(db_path)
    cfg = load_config("config.yaml")
    return cfg.storage.path


def _connect(db_path: Optional[str | Path]) -> sqlite3.Connection:
    return sqlite3.connect(_resolve_db_path(db_path))


def as_dataframe(
    table: str,
    *,
    db_path: Optional[str | Path] = None,
    where: Optional[str] = None,
    parse_raw: bool = False,
) -> Any:
    """Generic table → DataFrame. Works on tables AND views.

    If `parse_raw=True` and the table has a `raw` column, parse it into a
    `raw_parsed` column of dicts for downstream feature extraction.
    """
    pd = _pandas()
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    with _connect(db_path) as db:
        df = pd.read_sql_query(sql, db)
    if parse_raw and "raw" in df.columns:
        df["raw_parsed"] = df["raw"].apply(
            lambda s: json.loads(s) if isinstance(s, str) and s else None
        )
    return df


def posts_for_keyword(
    keyword: str,
    *,
    platforms: Optional[list[str]] = None,
    db_path: Optional[str | Path] = None,
) -> Any:
    """Return the unified posts view filtered to one keyword.

    Joins through `keyword_posts` so you only get posts that were actually pulled
    by a `scrape <keyword>` run. Pass `platforms=["bili"]` to restrict.
    """
    pd = _pandas()
    plat_clause = ""
    params: list[Any] = [keyword]
    if platforms:
        # keyword_posts.platform is 'bili' or 'zhihu'; posts_unified.平台 includes
        # 'zhihu_question' and 'zhihu_answer' — map accordingly.
        plat_clause = " AND (" + " OR ".join(
            ["kp.platform = ?"] * len(platforms)
        ) + ")"
        params.extend(platforms)
    sql = f"""
        SELECT pu.*
        FROM posts_unified pu
        JOIN keyword_posts kp
          ON kp.post_id = pu.帖子ID
         AND (
              (kp.platform = 'bili' AND pu.平台 = 'bili')
           OR (kp.platform = 'zhihu' AND pu.平台 IN ('zhihu_question','zhihu_answer'))
         )
        WHERE kp.keyword = ?{plat_clause}
        ORDER BY pu.发布时间 DESC
    """
    with _connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=params)


def comments_for_post(
    post_id: str,
    *,
    db_path: Optional[str | Path] = None,
) -> Any:
    """All comments belonging to a single post (bili video aid or zhihu answer id)."""
    pd = _pandas()
    sql = "SELECT * FROM comments_unified WHERE 帖子ID = ? ORDER BY 点赞数量 DESC NULLS LAST"
    with _connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=[post_id])


def volume_by_day(
    keyword: str,
    *,
    db_path: Optional[str | Path] = None,
) -> Any:
    """Per-day discussion-volume aggregates for one keyword.

    Returns columns:
        日期 (date), 平台, 帖子数, 评论总数, 点赞总数, 收藏总数, 播放总数

    Groups by the post's *publish* date (发布时间), not the scrape date — so a
    keyword scraped today gives you a historical volume curve based on when each
    post was actually published.
    """
    pd = _pandas()
    df = posts_for_keyword(keyword, db_path=db_path)
    if df.empty:
        return df.assign(日期=[], 帖子数=[], 评论总数=[], 点赞总数=[], 收藏总数=[], 播放总数=[])
    # Coerce publish timestamps (unix seconds) to date strings; drop rows missing one.
    df = df.dropna(subset=["发布时间"]).copy()
    df["日期"] = pd.to_datetime(df["发布时间"], unit="s").dt.date.astype(str)
    agg = (
        df.groupby(["日期", "平台"], as_index=False)
        .agg(
            帖子数=("帖子ID", "count"),
            评论总数=("评论数量", "sum"),
            点赞总数=("点赞数量", "sum"),
            收藏总数=("收藏数量", "sum"),
            播放总数=("播放数量", "sum"),
        )
        .sort_values(["日期", "平台"])
        .reset_index(drop=True)
    )
    return agg


def engagement_history(
    post_id: str,
    *,
    db_path: Optional[str | Path] = None,
) -> Any:
    """Time-series of engagement metrics for a single post across re-scrapes.

    Each row is one snapshot — re-run `scrape <keyword>` to add new rows.
    Useful for measuring growth/decay rates of views/likes/comments over time.
    """
    pd = _pandas()
    sql = """
        SELECT platform AS 平台,
               post_id  AS 帖子ID,
               datetime(fetched_at, 'unixepoch') AS 时间,
               view_count     AS 播放数量,
               like_count     AS 点赞数量,
               comment_count  AS 评论数量,
               favorite_count AS 收藏数量,
               share_count    AS 转发数量
        FROM engagement_snapshots
        WHERE post_id = ?
        ORDER BY fetched_at
    """
    with _connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=[post_id])


def scrape_run_summary(
    *,
    keyword: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> Any:
    """List past scrape runs as a DataFrame. Filter by keyword if given."""
    pd = _pandas()
    sql = """
        SELECT run_id     AS 运行ID,
               keyword    AS 关键词,
               platform   AS 平台,
               requested_count AS 请求数量,
               posts_fetched   AS 实际帖子数,
               comments_fetched AS 实际评论数,
               datetime(started_at,'unixepoch')  AS 开始时间,
               datetime(finished_at,'unixepoch') AS 结束时间,
               error      AS 错误信息
        FROM scrape_runs
    """
    params: list[Any] = []
    if keyword:
        sql += " WHERE keyword = ?"
        params.append(keyword)
    sql += " ORDER BY run_id DESC"
    with _connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=params)


def answers_for_keyword(
    keyword: str,
    *,
    db_path: Optional[str | Path] = None,
) -> Any:
    """All Zhihu answers under questions that were brought in by this keyword.

    `posts_for_keyword` only returns *direct* hits (questions), because that's
    what `keyword_posts` records. This helper does the 1-hop join to also fetch
    the answers underneath. Useful for content/sentiment analysis on actual
    discussion, not just the question titles.
    """
    pd = _pandas()
    sql = """
        SELECT pu.*
        FROM posts_unified pu
        WHERE pu.平台 = 'zhihu_answer'
          AND pu.帖子ID IN (
              SELECT CAST(a.id AS TEXT)
              FROM zhihu_answers a
              JOIN keyword_posts kp
                ON kp.post_id = CAST(a.question_id AS TEXT)
              WHERE kp.keyword = ?
          )
        ORDER BY pu.点赞数量 DESC
    """
    with _connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=[keyword])


def export_run_to_csv(
    run_id: int,
    *,
    out_dir: str | Path = "data/exports",
    include_comments: bool = True,
    db_path: Optional[str | Path] = None,
) -> dict[str, str]:
    """Export everything collected by one scrape run to CSV files.

    Writes two files (or one if include_comments=False) into `out_dir`:
      - `<keyword>_run<run_id>_posts.csv`     — posts table, MediaCrawler schema
      - `<keyword>_run<run_id>_comments.csv`  — comments table (if any)

    Uses UTF-8 with BOM so Excel opens it correctly on Windows. The CSV is
    filtered to *only* this run's content — re-runs of the same keyword get
    their own files instead of overwriting.

    Returns a dict mapping role → absolute path of files written.
    """
    pd = _pandas()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Look up which keyword this run was for, so the filename is meaningful.
    with _connect(db_path) as db:
        row = db.execute(
            "SELECT keyword, platform FROM scrape_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if not row:
        raise ScraperError(f"run_id {run_id} not found in scrape_runs")
    keyword, _platform = row
    # Sanitize keyword for filesystem use (drop slashes, quotes, etc.).
    safe_kw = "".join(c for c in keyword if c.isalnum() or c in ("_", "-", "."))[:80] or "keyword"

    # --- posts: keyword_posts members + the zhihu answers hanging off them ---
    posts_sql = """
        SELECT pu.*
        FROM posts_unified pu
        WHERE pu.帖子ID IN (
            SELECT post_id FROM keyword_posts WHERE run_id = ?
        )
        UNION ALL
        SELECT pu.*
        FROM posts_unified pu
        WHERE pu.平台 = 'zhihu_answer'
          AND pu.帖子ID IN (
              SELECT CAST(a.id AS TEXT)
              FROM zhihu_answers a
              JOIN keyword_posts kp
                ON kp.post_id = CAST(a.question_id AS TEXT)
              WHERE kp.run_id = ?
          )
        ORDER BY 平台, 发布时间 DESC
    """
    with _connect(db_path) as db:
        posts_df = pd.read_sql_query(posts_sql, db, params=[run_id, run_id])

    paths: dict[str, str] = {}
    posts_path = out / f"{safe_kw}_run{run_id}_posts.csv"
    # encoding="utf-8-sig" writes a BOM — required for Excel on Windows to
    # detect UTF-8 and render Chinese correctly.
    posts_df.to_csv(posts_path, index=False, encoding="utf-8-sig")
    paths["posts"] = str(posts_path.resolve())

    if include_comments:
        comments_sql = """
            SELECT cu.*
            FROM comments_unified cu
            WHERE
              -- bili comments live under a video; match on bili posts in this run
              (cu.平台 = 'bili' AND cu.帖子ID IN (
                  SELECT CAST(v.aid AS TEXT) FROM bili_videos v
                  JOIN keyword_posts kp ON kp.post_id = v.bvid
                  WHERE kp.run_id = ?
              ))
              OR
              -- zhihu answer comments — match on answers under this run's questions
              (cu.平台 = 'zhihu_answer' AND cu.帖子ID IN (
                  SELECT CAST(a.id AS TEXT)
                  FROM zhihu_answers a
                  JOIN keyword_posts kp
                    ON kp.post_id = CAST(a.question_id AS TEXT)
                  WHERE kp.run_id = ?
              ))
            ORDER BY 平台, 点赞数量 DESC
        """
        with _connect(db_path) as db:
            comments_df = pd.read_sql_query(comments_sql, db, params=[run_id, run_id])
        if not comments_df.empty:
            comments_path = out / f"{safe_kw}_run{run_id}_comments.csv"
            comments_df.to_csv(comments_path, index=False, encoding="utf-8-sig")
            paths["comments"] = str(comments_path.resolve())
    return paths


__all__ = [
    "as_dataframe",
    "posts_for_keyword",
    "answers_for_keyword",
    "comments_for_post",
    "volume_by_day",
    "engagement_history",
    "scrape_run_summary",
    "export_run_to_csv",
]

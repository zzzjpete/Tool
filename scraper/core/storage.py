from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import aiosqlite


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS bili_videos (
        bvid TEXT PRIMARY KEY,
        aid INTEGER,
        title TEXT,
        desc TEXT,
        owner_mid INTEGER,
        owner_name TEXT,
        pubdate INTEGER,
        duration INTEGER,
        view INTEGER,
        like INTEGER,
        coin INTEGER,
        favorite INTEGER,
        reply INTEGER,
        share INTEGER,
        danmaku INTEGER,
        tags TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bili_comments (
        rpid INTEGER PRIMARY KEY,
        oid INTEGER,
        parent INTEGER,
        mid INTEGER,
        uname TEXT,
        message TEXT,
        ctime INTEGER,
        likes INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bili_dynamics (
        dynamic_id TEXT PRIMARY KEY,
        mid INTEGER,
        uname TEXT,
        type TEXT,
        text TEXT,
        pub_ts INTEGER,
        like_count INTEGER,
        comment_count INTEGER,
        forward_count INTEGER,
        bvid TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bili_search (
        keyword TEXT,
        bvid TEXT,
        title TEXT,
        author TEXT,
        play INTEGER,
        rank INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (keyword, bvid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_questions (
        id INTEGER PRIMARY KEY,
        title TEXT,
        detail TEXT,
        answer_count INTEGER,
        follower_count INTEGER,
        view_count INTEGER,
        created INTEGER,
        topics TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_answers (
        id INTEGER PRIMARY KEY,
        question_id INTEGER,
        author_id TEXT,
        author_name TEXT,
        voteup_count INTEGER,
        comment_count INTEGER,
        created_time INTEGER,
        updated_time INTEGER,
        content TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_columns (
        id TEXT PRIMARY KEY,
        title TEXT,
        author_id TEXT,
        author_name TEXT,
        description TEXT,
        articles_count INTEGER,
        followers INTEGER,
        created INTEGER,
        updated INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_articles (
        id INTEGER PRIMARY KEY,
        column_id TEXT,
        title TEXT,
        author_id TEXT,
        author_name TEXT,
        voteup_count INTEGER,
        comment_count INTEGER,
        created INTEGER,
        updated INTEGER,
        excerpt TEXT,
        content TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_search (
        keyword TEXT,
        kind TEXT,
        target_id TEXT,
        title TEXT,
        excerpt TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (keyword, kind, target_id)
    )
    """,
    # --- ML infrastructure (longitudinal analysis) ----------------------------
    """
    CREATE TABLE IF NOT EXISTS scrape_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT,
        platform TEXT,
        requested_count INTEGER,
        started_at INTEGER DEFAULT (strftime('%s','now')),
        finished_at INTEGER,
        posts_fetched INTEGER,
        comments_fetched INTEGER,
        config TEXT,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS keyword_posts (
        keyword TEXT,
        platform TEXT,
        post_id TEXT,
        run_id INTEGER,
        rank INTEGER,
        fetched_at INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (keyword, platform, post_id, run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS engagement_snapshots (
        platform TEXT,
        post_id TEXT,
        fetched_at INTEGER,
        view_count INTEGER,
        like_count INTEGER,
        comment_count INTEGER,
        favorite_count INTEGER,
        share_count INTEGER,
        PRIMARY KEY (platform, post_id, fetched_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS zhihu_answer_comments (
        comment_id INTEGER PRIMARY KEY,
        answer_id INTEGER,
        author_id TEXT,
        author_name TEXT,
        content TEXT,
        like_count INTEGER,
        created_time INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    # --- Weibo (m.weibo.cn) ---------------------------------------------------
    # `id` is the numeric status id (stringified); `mblogid` is the base-62 bid
    # used in weibo.com/<uid>/<bid> links. created_ts is best-effort epoch parsed
    # from Weibo's messy created_at string (which is kept raw alongside).
    """
    CREATE TABLE IF NOT EXISTS weibo_posts (
        id TEXT PRIMARY KEY,
        mblogid TEXT,
        user_id TEXT,
        user_name TEXT,
        text TEXT,
        created_at TEXT,
        created_ts INTEGER,
        source TEXT,
        reposts_count INTEGER,
        comments_count INTEGER,
        attitudes_count INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS weibo_comments (
        comment_id TEXT PRIMARY KEY,
        post_id TEXT,
        user_id TEXT,
        user_name TEXT,
        text TEXT,
        like_count INTEGER,
        created_at TEXT,
        created_ts INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    # --- Baidu Tieba ----------------------------------------------------------
    # `tid` is the thread id (kz). tieba_comments holds reply floors (2..N), keyed
    # by the reply/post id (pid). content is plain text extracted from the segment
    # list the mobile API returns; raw keeps the full payload.
    """
    CREATE TABLE IF NOT EXISTS tieba_threads (
        tid TEXT PRIMARY KEY,
        title TEXT,
        author_name TEXT,
        author_id TEXT,
        forum_name TEXT,
        content TEXT,
        reply_num INTEGER,
        created_at TEXT,
        created_ts INTEGER,
        url TEXT,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tieba_comments (
        pid TEXT PRIMARY KEY,
        tid TEXT,
        floor INTEGER,
        author_name TEXT,
        author_id TEXT,
        content TEXT,
        created_at TEXT,
        created_ts INTEGER,
        raw TEXT,
        fetched_at INTEGER DEFAULT (strftime('%s','now'))
    )
    """,
    # --- ML-facing unified views ---------------------------------------------
    # Chinese column names matching the MediaCrawler-style export schema so the
    # output is directly usable in analysis notebooks. Underlying tables stay
    # English for code maintainability.
    """
    DROP VIEW IF EXISTS posts_unified
    """,
    """
    CREATE VIEW posts_unified AS
    SELECT
        'bili'                                              AS 平台,
        bvid                                                AS 帖子ID,
        'https://www.bilibili.com/video/' || bvid           AS 笔记链接,
        CAST(owner_mid AS TEXT)                             AS 作者ID,
        owner_name                                          AS 作者昵称,
        pubdate                                             AS 发布时间,
        title                                               AS 标题,
        desc                                                AS 内容,
        tags                                                AS 标签,
        reply                                               AS 评论数量,
        "like"                                              AS 点赞数量,
        favorite                                            AS 收藏数量,
        share                                               AS 转发数量,
        view                                                AS 播放数量,
        fetched_at                                          AS 抓取时间
    FROM bili_videos
    UNION ALL
    SELECT
        'zhihu_question'                                    AS 平台,
        CAST(id AS TEXT)                                    AS 帖子ID,
        'https://www.zhihu.com/question/' || id             AS 笔记链接,
        NULL                                                AS 作者ID,
        NULL                                                AS 作者昵称,
        created                                             AS 发布时间,
        title                                               AS 标题,
        detail                                              AS 内容,
        topics                                              AS 标签,
        answer_count                                        AS 评论数量,
        NULL                                                AS 点赞数量,
        follower_count                                      AS 收藏数量,
        NULL                                                AS 转发数量,
        view_count                                          AS 播放数量,
        fetched_at                                          AS 抓取时间
    FROM zhihu_questions
    UNION ALL
    SELECT
        'zhihu_answer'                                      AS 平台,
        CAST(id AS TEXT)                                    AS 帖子ID,
        'https://www.zhihu.com/question/' || question_id ||
            '/answer/' || id                                AS 笔记链接,
        author_id                                           AS 作者ID,
        author_name                                         AS 作者昵称,
        created_time                                        AS 发布时间,
        NULL                                                AS 标题,
        content                                             AS 内容,
        NULL                                                AS 标签,
        comment_count                                       AS 评论数量,
        voteup_count                                        AS 点赞数量,
        NULL                                                AS 收藏数量,
        NULL                                                AS 转发数量,
        NULL                                                AS 播放数量,
        fetched_at                                          AS 抓取时间
    FROM zhihu_answers
    UNION ALL
    SELECT
        'weibo'                                             AS 平台,
        id                                                  AS 帖子ID,
        'https://m.weibo.cn/detail/' || id                  AS 笔记链接,
        user_id                                             AS 作者ID,
        user_name                                           AS 作者昵称,
        created_ts                                          AS 发布时间,
        NULL                                                AS 标题,
        text                                                AS 内容,
        NULL                                                AS 标签,
        comments_count                                      AS 评论数量,
        attitudes_count                                     AS 点赞数量,
        NULL                                                AS 收藏数量,
        reposts_count                                       AS 转发数量,
        NULL                                                AS 播放数量,
        fetched_at                                          AS 抓取时间
    FROM weibo_posts
    UNION ALL
    SELECT
        'tieba'                                             AS 平台,
        tid                                                 AS 帖子ID,
        'https://tieba.baidu.com/p/' || tid                 AS 笔记链接,
        author_id                                           AS 作者ID,
        author_name                                         AS 作者昵称,
        created_ts                                          AS 发布时间,
        title                                               AS 标题,
        content                                             AS 内容,
        forum_name                                          AS 标签,
        reply_num                                           AS 评论数量,
        NULL                                                AS 点赞数量,
        NULL                                                AS 收藏数量,
        NULL                                                AS 转发数量,
        NULL                                                AS 播放数量,
        fetched_at                                          AS 抓取时间
    FROM tieba_threads
    """,
    """
    DROP VIEW IF EXISTS comments_unified
    """,
    """
    CREATE VIEW comments_unified AS
    SELECT
        'bili'                                              AS 平台,
        CAST(oid AS TEXT)                                   AS 帖子ID,
        CAST(rpid AS TEXT)                                  AS 评论ID,
        CAST(mid AS TEXT)                                   AS 作者ID,
        uname                                               AS 作者昵称,
        ctime                                               AS 发布时间,
        message                                             AS 内容,
        likes                                               AS 点赞数量,
        fetched_at                                          AS 抓取时间
    FROM bili_comments
    UNION ALL
    SELECT
        'zhihu_answer'                                      AS 平台,
        CAST(answer_id AS TEXT)                             AS 帖子ID,
        CAST(comment_id AS TEXT)                            AS 评论ID,
        author_id                                           AS 作者ID,
        author_name                                         AS 作者昵称,
        created_time                                        AS 发布时间,
        content                                             AS 内容,
        like_count                                          AS 点赞数量,
        fetched_at                                          AS 抓取时间
    FROM zhihu_answer_comments
    UNION ALL
    SELECT
        'weibo'                                             AS 平台,
        post_id                                             AS 帖子ID,
        comment_id                                          AS 评论ID,
        user_id                                             AS 作者ID,
        user_name                                           AS 作者昵称,
        created_ts                                          AS 发布时间,
        text                                                AS 内容,
        like_count                                          AS 点赞数量,
        fetched_at                                          AS 抓取时间
    FROM weibo_comments
    UNION ALL
    SELECT
        'tieba'                                             AS 平台,
        tid                                                 AS 帖子ID,
        pid                                                 AS 评论ID,
        author_id                                           AS 作者ID,
        author_name                                         AS 作者昵称,
        created_ts                                          AS 发布时间,
        content                                             AS 内容,
        NULL                                                AS 点赞数量,
        fetched_at                                          AS 抓取时间
    FROM tieba_comments
    """,
    # Convenience: posts joined to the keywords that brought them in. Many-to-many.
    """
    DROP VIEW IF EXISTS keyword_posts_unified
    """,
    """
    CREATE VIEW keyword_posts_unified AS
    SELECT
        kp.keyword                                          AS 关键词,
        kp.platform                                         AS 平台,
        kp.post_id                                          AS 帖子ID,
        kp.run_id                                           AS 运行ID,
        kp.rank                                             AS 排名,
        sr.started_at                                       AS 运行开始时间
    FROM keyword_posts kp
    LEFT JOIN scrape_runs sr USING (run_id)
    """,
]


# Columns to add to existing tables for users whose DB was created before the
# ML-infrastructure migration. Idempotent — checked at init time.
_COLUMN_MIGRATIONS = [
    ("bili_videos", "tags", "TEXT"),
    ("zhihu_questions", "topics", "TEXT"),
]


class SqliteStorage:
    """Thin async SQLite wrapper. Stores normalized columns + the raw JSON blob."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        for stmt in SCHEMA:
            await self._db.execute(stmt)
        # Migrate older DBs: add columns that didn't exist when the user created
        # their data/scraped.db. SQLite's ALTER TABLE ADD COLUMN errors on duplicate,
        # so we check PRAGMA table_info first.
        for table, col, col_type in _COLUMN_MIGRATIONS:
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if col not in existing:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("storage not initialized; call await storage.init() first")
        return self._db

    async def upsert(self, table: str, row: Mapping[str, Any]) -> None:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        update_list = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("fetched_at",))
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT DO UPDATE SET {update_list}"
            if update_list
            else f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        )
        values = [
            json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
            for v in row.values()
        ]
        await self.db.execute(sql, values)
        await self.db.commit()

    async def start_run(
        self,
        keyword: str,
        platform: str,
        requested_count: int,
        config: Optional[Mapping[str, Any]] = None,
    ) -> int:
        """Open a scrape_runs row and return its run_id. Pair with finish_run()."""
        cur = await self.db.execute(
            "INSERT INTO scrape_runs (keyword, platform, requested_count, config) "
            "VALUES (?, ?, ?, ?)",
            (
                keyword,
                platform,
                requested_count,
                json.dumps(dict(config or {}), ensure_ascii=False),
            ),
        )
        run_id = cur.lastrowid
        await cur.close()
        await self.db.commit()
        return int(run_id)

    async def finish_run(
        self,
        run_id: int,
        *,
        posts_fetched: int,
        comments_fetched: int,
        error: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            "UPDATE scrape_runs "
            "SET finished_at = strftime('%s','now'), "
            "    posts_fetched = ?, comments_fetched = ?, error = ? "
            "WHERE run_id = ?",
            (posts_fetched, comments_fetched, error, run_id),
        )
        await self.db.commit()

    async def keyword_post_ids(self, *, keyword: str, platform: str) -> set[str]:
        """Return the set of post_ids already linked to `keyword` for `platform`.

        Used by the `only_new` scrape mode to skip post_ids we've already pulled
        for this keyword on prior runs — saves rate-limit quota on daily re-runs.
        """
        cur = await self.db.execute(
            "SELECT DISTINCT post_id FROM keyword_posts WHERE keyword = ? AND platform = ?",
            (keyword, platform),
        )
        rows = await cur.fetchall()
        await cur.close()
        return {r[0] for r in rows if r[0] is not None}

    async def record_keyword_post(
        self,
        *,
        keyword: str,
        platform: str,
        post_id: str,
        run_id: int,
        rank: int,
    ) -> None:
        # INSERT OR IGNORE: re-runs of the same keyword + run + post are a no-op
        # (shouldn't happen within one run, but keeps us safe across reruns).
        await self.db.execute(
            "INSERT OR IGNORE INTO keyword_posts (keyword, platform, post_id, run_id, rank) "
            "VALUES (?, ?, ?, ?, ?)",
            (keyword, platform, str(post_id), run_id, rank),
        )
        await self.db.commit()

    async def record_engagement(
        self,
        *,
        platform: str,
        post_id: str,
        view_count: Optional[int] = None,
        like_count: Optional[int] = None,
        comment_count: Optional[int] = None,
        favorite_count: Optional[int] = None,
        share_count: Optional[int] = None,
    ) -> None:
        """Append a time-series snapshot of engagement metrics. Idempotent within
        the same second (PRIMARY KEY collisions are silently ignored)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO engagement_snapshots "
            "(platform, post_id, fetched_at, view_count, like_count, "
            " comment_count, favorite_count, share_count) "
            "VALUES (?, ?, strftime('%s','now'), ?, ?, ?, ?, ?)",
            (
                platform, str(post_id),
                view_count, like_count, comment_count, favorite_count, share_count,
            ),
        )
        await self.db.commit()

    async def upsert_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        n = 0
        for r in rows:
            await self.upsert(table, r)
            n += 1
        return n

    async def fetch_all(self, table: str) -> list[dict[str, Any]]:
        cur = await self.db.execute(f"SELECT * FROM {table}")
        cols = [c[0] for c in cur.description]
        rows = await cur.fetchall()
        await cur.close()
        return [dict(zip(cols, r)) for r in rows]

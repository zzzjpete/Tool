"""Fetch a Zhihu column (专栏) and walk its articles. Requires zhihu.cookie in config.yaml."""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper import SqliteStorage, ZhihuScraper, load_config


async def main() -> None:
    cfg = load_config("config.yaml")
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()

    column_id = "c_1234567890"  # Replace with the column slug from zhuanlan.zhihu.com/<id>

    async with ZhihuScraper(cfg, storage) as zhihu:
        col = await zhihu.get_column(column_id)
        print(f"{col.get('title')}")
        author = col.get("author") or {}
        print(
            f"by {author.get('name')}  —  articles={col.get('articles_count')}  followers={col.get('followers')}"
        )

        print("\n--- articles ---")
        async for art in zhihu.iter_column_items(column_id, limit=20):
            print(f"[{art['voteup_count'] or 0:>5}] {art['title']}")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

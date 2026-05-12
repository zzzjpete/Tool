"""Fetch a Bilibili video and its first 2 pages of top-level comments."""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper import BilibiliScraper, SqliteStorage, load_config


async def main() -> None:
    cfg = load_config("config.yaml")
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()

    bvid = "BV1xx411c7XW"  # Replace with the video you want

    async with BilibiliScraper(cfg, storage) as bili:
        video = await bili.get_video(bvid)
        print(f"{video.title}  —  {video.owner.get('name')}")
        print(f"views={video.stat.get('view')}  likes={video.stat.get('like')}")

        print("\n--- comments ---")
        async for c in bili.iter_comments(bvid, pages=2):
            print(f"{c['uname']}: {(c['message'] or '')[:80]}")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

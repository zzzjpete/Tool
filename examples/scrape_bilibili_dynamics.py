"""Walk a Bilibili user's dynamic feed (动态) and persist each item."""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper import BilibiliScraper, SqliteStorage, load_config


async def main() -> None:
    cfg = load_config("config.yaml")
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()

    mid = 2  # Replace with the user mid you want (2 = 碧诗, an early test account)

    async with BilibiliScraper(cfg, storage) as bili:
        print(f"--- dynamics for mid={mid} ---")
        async for d in bili.iter_user_dynamics(mid, pages=2):
            tag = (d.get("type") or "").replace("DYNAMIC_TYPE_", "")
            text = (d.get("text") or "").replace("\n", " ")[:100]
            print(f"[{tag:<8}] {d['uname']}: {text}")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Fetch a Zhihu question and the top 30 answers. Requires zhihu.cookie in config.yaml."""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scraper import SqliteStorage, ZhihuScraper, load_config


async def main() -> None:
    cfg = load_config("config.yaml")
    storage = SqliteStorage(cfg.storage.path)
    await storage.init()

    question_id = 123456789  # Replace with the question id you want

    async with ZhihuScraper(cfg, storage) as zhihu:
        q = await zhihu.get_question(question_id)
        print(f"{q.get('title')}")
        print(f"answers={q.get('answer_count')}  followers={q.get('follower_count')}")

        print("\n--- top answers ---")
        async for ans in zhihu.iter_answers(question_id, limit=30):
            print(f"[{ans['voteup_count']:>5}] {ans['author_name']}: {(ans['content'] or '')[:100]}")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

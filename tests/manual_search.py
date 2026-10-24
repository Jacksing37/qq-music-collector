"""真联网搜几个词，看 Bing 还能不能解析出结果。**不进回归**（会被反爬，也不该天天去打人家）。

用法: python tests/manual_search.py "尊界 v800 售价" "尊界 v800 刹车事件"
      不传参数就用下面 DEFAULT_QUERIES。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import websearch as ws  # noqa: E402

DEFAULT_QUERIES = ["尊界 v800 售价", "尊界 v800 刹车事件"]


async def main(queries: list[str]) -> int:
    bad = 0
    for query in queries:
        found = await ws.search(query, max_results=5, timeout=20.0)
        print("=" * 70)
        print(f"查询: {query}")
        print(f"引擎: {found['engine']}   错误: {found['error'] or '（无）'}")
        rows = found["results"]
        if not rows:
            bad += 1
            print("!! 0 条结果 —— 可能被反爬、或 Bing 改版导致解析失效")
            continue
        for index, row in enumerate(rows, 1):
            print(f"\n{index}. {row['title']}")
            print(f"   {row['snippet'][:150]}")
            print(f"   {row['url'][:120]}")
        print(f"\n--- 共 {len(rows)} 条 ---")
    return bad


if __name__ == "__main__":
    args = sys.argv[1:] or DEFAULT_QUERIES
    failed = asyncio.run(main(args))
    print(f"\n{len(args) - failed}/{len(args)} 个查询拿到结果")
    sys.exit(1 if failed else 0)

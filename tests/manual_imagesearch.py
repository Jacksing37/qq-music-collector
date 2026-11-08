"""手动验证：真联网跑一次图片搜索 + 真下载一张（**不进回归**，会受网络影响）。

    ./.venv/Scripts/python.exe tests/manual_imagesearch.py            # 默认词
    ./.venv/Scripts/python.exe tests/manual_imagesearch.py 柴犬 表情包

看三件事：
1. Bing / 百度到底解析不解析得出来（页面结构变了这里最先暴露）；
2. 解析出来的直链能不能真下载（防盗链、链接过期）；
3. 报给模型的文案长什么样。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSIC_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="qcmc-imgsearch-")))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import imagesearch as isr  # noqa: E402
from music_collector import stickers as st  # noqa: E402


async def main() -> int:
    query = " ".join(sys.argv[1:]) or "柴犬 表情包"
    print(f"=== 搜「{query}」===")
    for engine in ("bing", "baidu"):
        rows, error = await isr.search(query, 6, 20.0, engines=(engine,))
        print(f"[{engine}] {len(rows)} 条  {error}")
        for row in rows[:3]:
            print(f"   - {row['title'][:30]} | {row['url'][:90]}")
        isr.reset()

    rows, error = await isr.search(query, 6, 20.0)
    if not rows:
        print(f"两个引擎都没结果：{error}")
        return 1
    print("\n=== 报给模型的文案 ===")
    print(isr.format_results(query, rows)[:800])

    print("\n=== 真下载前 3 条 ===")
    ok = 0
    for row in rows[:3]:
        try:
            data = await st.download(row["url"], 20.0)
            ext, mime = st.sniff(data)
            print(f"   OK  {len(data) // 1024} KB {mime} {row['url'][:80]}")
            ok += 1
        except ValueError as exc:
            print(f"   --  {exc}  ← {row['url'][:80]}")
    print(f"\n下载成功 {ok}/{min(3, len(rows))}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

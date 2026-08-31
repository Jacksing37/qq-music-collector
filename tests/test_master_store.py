"""总库虚拟窗口（__master__）在 store 层的隔离与去重。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_master_store.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.models import Song  # noqa: E402
from music_collector.store import MASTER_KEY, Store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def _song(sid: str, title: str, platform: str = "netease") -> Song:
    return Song(
        platform=platform, song_id=sid, title=title, artists="测试歌手",
        sharer_id=1, sharer_name="张三", url="",
    )


async def test_master_key_constant():
    print("\n[A] MASTER_KEY 常量")
    check("常量为 __master__", MASTER_KEY == "__master__", MASTER_KEY)


async def test_master_excluded_from_lists():
    print("\n[B] 总库不出现在普通窗口列表 / 广播")
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    await store.init()
    gid = 7001
    await store.add_song(gid, "W1", _song("1", "歌一"))
    await store.add_song(gid, "W2", _song("2", "歌二"))
    await store.add_song(gid, MASTER_KEY, _song("1", "歌一"))  # 总库

    groups = await store.all_groups()
    check("all_groups 不含总库自身", MASTER_KEY not in groups and gid in groups, f"-> {groups}")
    wins = await store.windows_with_counts(gid)
    keys = [w for w, _ in wins]
    check("windows_with_counts 不含 __master__", MASTER_KEY not in keys, f"-> {keys}")
    check("windows_with_counts 含 W1/W2", "W1" in keys and "W2" in keys, f"-> {keys}")


async def test_master_cross_window_dedup():
    print("\n[C] 总库自身跨窗口去重（唯一约束）")
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    await store.init()
    gid = 7002
    # 同一首歌在 W1 与 W2 各一份（不同窗口，允许并存）
    i1, _ = await store.add_song(gid, "W1", _song("9", "同一首"))
    i2, _ = await store.add_song(gid, "W2", _song("9", "同一首"))
    check("不同窗口可并存同一歌", i1 and i2, f"i1={i1} i2={i2}")

    # 进总库两次：第二次应被唯一约束去重（只留 1 条）
    m1, _ = await store.add_song(gid, MASTER_KEY, _song("9", "同一首"))
    m2, _ = await store.add_song(gid, MASTER_KEY, _song("9", "同一首"))
    check("总库内重复只新增一次", m1 and not m2, f"m1={m1} m2={m2}")
    cnt = await store.count(gid, MASTER_KEY)
    check("总库该歌仅 1 条", cnt == 1, f"cnt={cnt}")


async def main() -> None:
    await test_master_key_constant()
    await test_master_excluded_from_lists()
    await test_master_cross_window_dedup()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

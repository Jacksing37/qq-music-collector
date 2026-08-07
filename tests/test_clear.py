"""清理已收集歌曲功能验证：归档后自动清 / 手动批量清 / 定时清。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_clear.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")  # 触发插件加载（require apscheduler)


from music_collector.commands import _parse_indices
from music_collector.service import CollectorService as _CollectorService
from music_collector.config import config_manager
from music_collector.models import Song
from music_collector.store import Store

PASSED = 0
FAILED = 0


def check(name: str, cond: bool) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}")


def _song(sid: str, title: str, ts: float) -> Song:
    return Song(
        platform="netease",
        song_id=sid,
        title=title,
        artists="测试歌手",
        created_at=ts,
    )


async def _seed(store: Store, gid: int, wkey: str, n: int, base_ts: float | None = None) -> None:
    base = base_ts if base_ts is not None else time.time()
    for i in range(n):
        # 每首间隔 1 秒，保证 created_at 不同；song_id 用纯数字以便直接命中网易云
        await store.add_song(gid, wkey, _song(f"{100 + i}", f"歌{i}", base - i))


class _StubNetease:
    logged_in = True

    def create_playlist(self, name, privacy):
        return 999

    def add_tracks(self, pid, batch):
        return None

    def update_description(self, pid, desc):
        return None

    def playlist_url(self, pid):
        return f"https://music.163.com/playlist/{pid}"


async def test_store() -> None:
    print("\n[A] store 层删除")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    gid = 1001
    now = time.time()
    await _seed(store, gid, "W1", 5)
    await _seed(store, gid, "W2", 3)

    check("初始计数 W1=5", await store.count(gid, "W1") == 5)
    check("初始计数 W2=3", await store.count(gid, "W2") == 3)

    # 批量不连续
    removed = await store.delete_songs_by_indices(gid, "W1", [1, 3, 5])
    check("批量删除 3 首", removed == 3)
    check("剩余 W1=2", await store.count(gid, "W1") == 2)

    # 范围
    removed = await store.delete_songs_by_indices(gid, "W1", [1, 2])
    check("清空 W1 剩余 2 首", removed == 2 and await store.count(gid, "W1") == 0)

    # 按窗口删除不影响其它窗口
    n = await store.delete_window(gid, "W2")
    check("按窗口删除 W2=3", n == 3 and await store.count(gid, "W2") == 0)

    # prune_old：老的删、新的留
    await _seed(store, gid, "W3", 2, base_ts=now - 100 * 86400)  # 100 天前
    await _seed(store, gid, "W4", 2, base_ts=now - 1)            # 刚创建
    before = now - 30 * 86400
    removed = await store.prune_old(before)
    check("prune_old 删除 2 首老的", removed == 2)
    check("prune 后新数据保留", await store.count(gid, "W4") == 2)

    # windows_with_counts
    wins = await store.windows_with_counts(gid)
    keys = [k for k, _ in wins]
    check("windows_with_counts 含 W4", "W4" in keys)


async def test_service_after_archive() -> None:
    print("\n[B] 归档后自动清空本期")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    svc = _CollectorService()
    svc.store = store
    svc.netease = _StubNetease()

    gid = 2002
    state = svc.current_window()
    await _seed(store, gid, state.key, 4)

    # 关闭 seq 自增避免落盘副作用；开启归档后自动清
    config_manager.config.playlist.seq_auto_increment = False
    config_manager.config.clear.after_archive = True

    report = await svc.run_archive(gid, window=state)
    check("归档成功", report.ok)
    check("归档后本期已清空", await store.count(gid, state.key) == 0)

    # 关闭 after_archive 时不清空
    config_manager.config.clear.after_archive = False
    await _seed(store, gid, state.key, 2)
    report = await svc.run_archive(gid, window=state)
    check("不自动清时保留", report.ok and await store.count(gid, state.key) == 2)


async def test_parse_indices() -> None:
    print("\n[C] 命令参数解析")
    check("单序号", _parse_indices(["3"]) == [3])
    check("范围 1-5", _parse_indices(["1-5"]) == [1, 2, 3, 4, 5])
    check("多个不连续", _parse_indices(["2", "4", "7"]) == [2, 4, 7])
    check("范围+单", _parse_indices(["1-3", "5"]) == [1, 2, 3, 5])
    check("去重", _parse_indices(["2", "2", "3"]) == [2, 3])
    check("非法忽略", _parse_indices(["abc", "x-1", "9"]) == [9])


async def test_scheduled_prune() -> None:
    print("\n[D] 定时清理调用")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    svc = _CollectorService()
    svc.store = store
    gid = 3003
    now = time.time()
    await _seed(store, gid, "OLD", 3, base_ts=now - 100 * 86400)
    removed = await svc.prune_old(30)
    check("prune_old 经 service 删除 3 首", removed == 3)


async def main() -> None:
    await test_store()
    await test_service_after_archive()
    await test_parse_indices()
    await test_scheduled_prune()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

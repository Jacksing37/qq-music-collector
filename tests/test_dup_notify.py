"""重复歌曲提示回归：开启总库后，同一首歌重复分享仍要发 notify_duplicate 提示。

修复前：master.enabled 时，已存在总库的歌走 master_duplicated 并 continue，
跳过了同窗口去重判定，导致 result.duplicated 不被填充、notify_duplicate 形同虚设——
重复分享完全静默（除非单独打开 compare_on_share）。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_dup_notify.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.service import CollectorService  # noqa: E402
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


class StubNetease:
    logged_in = True

    async def create_playlist(self, name: str, privacy: bool) -> int:
        return 9000

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> None:
        pass

    async def remove_tracks(self, playlist_id: int, batch: list[str]) -> None:
        pass

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        return True, "ok"

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


def _song(sid: str, title: str, platform: str = "netease") -> Song:
    return Song(
        platform=platform, song_id=sid, title=title, artists="测试歌手",
        sharer_id=1, sharer_name="张三", url="", netease_id=sid,
    )


def _make_svc(tmp: Path):
    store = Store(tmp / "svc.db")
    api = StubNetease()
    svc = CollectorService()
    svc.store = store
    svc.netease = api
    svc.archiver = Archiver(api, store)
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.collect_override = "on"  # 强制收集期
    return svc, store


async def test_same_window_dup_with_master():
    """开启总库后，当前窗口内重复分享同一首歌 -> 仍记 duplicated（notify_duplicate 才会发消息）。"""
    print("\n[A] 总库开启时同窗口重复分享应触发 duplicated")
    tmp = Path(tempfile.mkdtemp())
    svc, store = _make_svc(tmp)
    await store.init()
    gid = 6201
    config_manager.config.master.enabled = True
    config_manager.config.master.compare_on_share = False  # 隔离：只看 notify_duplicate 路径

    async def _resolve(link):
        return _song("555", "孤勇者")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        # 第一次分享：入当前窗口 + 入总库
        r1 = await svc.handle_segments(gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "张三")
        check("首次分享被收录", len(r1.accepted) == 1, str(r1.accepted))
        check("首次分享同时进总库", await store.count(gid, MASTER_KEY) == 1,
              str(await store.count(gid, MASTER_KEY)))

        # 第二次分享同一首（同一窗口）：应判为同窗口重复 -> duplicated
        r2 = await svc.handle_segments(gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "李四")
        check("重复分享不再收录(accepted=0)", len(r2.accepted) == 0, str(r2.accepted))
        check("重复分享记 duplicated（notify_duplicate 据此发消息）",
              len(r2.duplicated) == 1, str(r2.duplicated))
        check("重复分享不重复计入总库", await store.count(gid, MASTER_KEY) == 1,
              str(await store.count(gid, MASTER_KEY)))
    finally:
        svc.providers.resolve = real


async def test_cross_window_dup_still_master():
    """总库开启、歌在总库但不在当前窗口（跨窗口重复）-> 仍走 master_duplicated。"""
    print("\n[B] 跨窗口重复（在总库、不在当前窗口）仍记 master_duplicated")
    tmp = Path(tempfile.mkdtemp())
    svc, store = _make_svc(tmp)
    await store.init()
    gid = 6202
    config_manager.config.master.enabled = True
    config_manager.config.master.compare_on_share = True

    # 预置：该歌已在总库（来自另一个窗口），但不在当前窗口
    await store.add_song(gid, MASTER_KEY, _song("555", "孤勇者"), src_window="W-首发期")

    async def _resolve(link):
        return _song("555", "孤勇者")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r = await svc.handle_segments(gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "李四")
        check("跨窗口重复不入当前窗口(accepted=0)", len(r.accepted) == 0, str(r.accepted))
        check("跨窗口重复记 master_duplicated", len(r.master_duplicated) == 1, str(r.master_duplicated))
        check("跨窗口重复不记 duplicated", len(r.duplicated) == 0, str(r.duplicated))
    finally:
        svc.providers.resolve = real


async def test_same_window_dup_without_master():
    """未开启总库时，同窗口重复分享 -> duplicated（基线行为不回退）。"""
    print("\n[C] 未开启总库时同窗口重复分享应触发 duplicated")
    tmp = Path(tempfile.mkdtemp())
    svc, store = _make_svc(tmp)
    await store.init()
    gid = 6203
    config_manager.config.master.enabled = False

    async def _resolve(link):
        return _song("555", "孤勇者")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r1 = await svc.handle_segments(gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "张三")
        check("首次分享被收录", len(r1.accepted) == 1, str(r1.accepted))
        r2 = await svc.handle_segments(gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "李四")
        check("重复分享记 duplicated", len(r2.duplicated) == 1, str(r2.duplicated))
        check("重复分享不重复收录", len(r2.accepted) == 0, str(r2.accepted))
    finally:
        svc.providers.resolve = real


async def main() -> None:
    await test_same_window_dup_with_master()
    await test_cross_window_dup_still_master()
    await test_same_window_dup_without_master()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

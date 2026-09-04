"""归档/同步后歌单顺序须与简介顺序一致（增量追加不再倒序）。

网易云 add 会把整批歌倒序插到歌单顶部；开启「分享即归档」后每首歌是单独增量追加，
若不做处理，歌单会是最新在上、简介是最旧在上，两者相反。本测试用模拟「顶部插入」
的 stub 验证重排逻辑（移除本 bot 管理的曲目再按窗口正序重新加入，不依赖网易云
专用 reorder 接口）能把歌单纠正为与简介一致的顺序，且不会误删用户手动加的歌。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_archive_order.py
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

from music_collector.models import Song  # noqa: E402
from music_collector.service import CollectorService as _CollectorService  # noqa: E402
from music_collector.store import Store  # noqa: E402

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


class NeteaseStub:
    """模拟网易云：add 把整批歌倒序顶到歌单顶部；支持重排与读取曲目。"""

    logged_in = True

    def __init__(self) -> None:
        self.pid = 0
        self.tracks: dict[int, list[str]] = {}
        self.descs: dict[int, str] = {}

    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.pid += 1
        self.tracks[self.pid] = []
        return self.pid

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> dict:
        # 模仿网易云：整批倒序后插到顶部
        self.tracks[playlist_id] = list(reversed(batch)) + self.tracks[playlist_id]
        return {"code": 200}

    async def remove_tracks(self, playlist_id: int, batch: list[str]) -> dict:
        for tid in batch:
            if tid in self.tracks[playlist_id]:
                self.tracks[playlist_id].remove(tid)
        return {"code": 200}

    async def playlist_track_ids(self, playlist_id: int) -> list[str]:
        return list(self.tracks.get(playlist_id, []))

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        self.descs[playlist_id] = desc
        return True, "ok"

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


def _song(sid: str, title: str) -> Song:
    return Song(platform="netease", song_id=sid, title=title, artists="测试歌手")


async def test_auto_archive_keeps_order() -> None:
    """分享即归档逐首追加，歌单最终顺序须与简介（正序）一致。"""
    print("\n[A] 分享即归档：歌单顺序 == 简介顺序")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    svc = _CollectorService()
    svc.store = store
    svc.netease = NeteaseStub()
    svc.archiver = svc.archiver.__class__(svc.netease, store)

    gid = 6001
    state = svc.current_window()
    api: NeteaseStub = svc.netease

    for sid, title in (("1", "歌一"), ("2", "歌二"), ("3", "歌三")):
        # 模拟真实链路：handle_segments 落库后把带 row_id 的 stored 传给自动归档，
        # 这样 archive 内的 match_netease_id 才能把 netease_id 写回 store。
        stored = (await store.add_song(gid, state.key, _song(sid, title)))[1]
        await svc.auto_archive_songs(gid, state, [stored])

    pid = next(iter(api.tracks))
    check("歌单曲目数为 3", len(api.tracks[pid]) == 3, f"-> {api.tracks[pid]}")
    check("歌单顺序为正序 1,2,3", api.tracks[pid] == ["1", "2", "3"], f"-> {api.tracks[pid]}")

    desc = api.descs[pid]
    # 简介里「歌一」应在「歌二」之前、「歌二」在「歌三」之前
    i1, i2, i3 = desc.find("歌一"), desc.find("歌二"), desc.find("歌三")
    check("简介顺序也为正序", 0 <= i1 < i2 < i3, f"-> 索引 {i1},{i2},{i3}")


async def test_reorder_preserves_user_added() -> None:
    """重排时不能误删用户在歌单里手动加的歌。"""
    print("\n[B] 重排保留用户手动加入的歌")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    svc = _CollectorService()
    svc.store = store
    svc.netease = NeteaseStub()
    svc.archiver = svc.archiver.__class__(svc.netease, store)

    gid = 6101
    state = svc.current_window()
    api: NeteaseStub = svc.netease

    for sid in ("1", "2", "3"):
        stored = (await store.add_song(gid, state.key, _song(sid, f"歌{sid}")))[1]
        await svc.auto_archive_songs(gid, state, [stored])

    pid = next(iter(api.tracks))
    # 模拟用户手动在歌单里加了一首 bot 不知道的歌
    api.tracks[pid].append("999")
    check("注入用户歌后曲目含 999", "999" in api.tracks[pid])

    stored4 = (await store.add_song(gid, state.key, _song("4", "歌四")))[1]
    await svc.auto_archive_songs(gid, state, [stored4])

    check("用户歌 999 仍保留", "999" in api.tracks[pid], f"-> {api.tracks[pid]}")
    check("顺序为正序且 999 在末尾",
          api.tracks[pid] == ["1", "2", "3", "4", "999"], f"-> {api.tracks[pid]}")


async def main() -> None:
    await test_auto_archive_keeps_order()
    await test_reorder_preserves_user_added()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""归档性能修复验证：

- A. archive() 对已匹配（netease_id 非空）的歌不再重复搜索，避免总库/窗口
  歌曲一多就每次归档都搜上千次、被网易云限流、阻塞消息回复。
- B. 总库「分享即归档」在歌单已存在时只把新歌增量追加到顶部，不再对整库重排
  （reorder_to_match 不应被调用），总库再大也不卡。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_archive_perf.py
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
from music_collector.models import Song  # noqa: E402
from music_collector.service import CollectorService as _CollectorService  # noqa: E402
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


class NeteaseStub:
    """模拟网易云：add 倒序顶到顶部；记录 add_tracks 调用；支持重排/简介/读曲目。"""

    logged_in = True

    def __init__(self) -> None:
        self.pid = 0
        self.tracks: dict[int, list[str]] = {}
        self.descs: dict[int, str] = {}
        self.add_calls = 0

    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.pid += 1
        self.tracks[self.pid] = []
        return self.pid

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> dict:
        self.add_calls += 1
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


# 统计 match_netease_id 调用次数（修复 A 的核心断言）
_MATCH_CALLS = {"n": 0}
_REORDER_CALLED = {"n": 0}


async def _spy_match(self, song, cfg):
    _MATCH_CALLS["n"] += 1
    # 已匹配直接复用；未匹配的（测试里设为 netease 数字 id）模拟成功匹配
    return song.netease_id or song.song_id


async def _spy_reorder(self, *args, **kwargs):
    _REORDER_CALLED["n"] += 1


def _song(sid: str, title: str, netease_id: str | None = None) -> Song:
    return Song(
        platform="netease", song_id=sid, title=title, artists="测试歌手",
        netease_id=netease_id, matched=bool(netease_id),
    )


async def test_archive_skips_matched() -> None:
    """100 首已匹配 + 2 首未匹配 → match_netease_id 只应被调用 2 次。"""
    print("\n[A] archive() 跳过已匹配歌的重复搜索")
    Archiver.match_netease_id = _spy_match  # type: ignore[assignment]
    _MATCH_CALLS["n"] = 0

    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    svc = _CollectorService()
    svc.store = store
    svc.netease = NeteaseStub()
    svc.archiver = Archiver(svc.netease, store)

    gid = 7001
    state = svc.current_window()
    # 100 首已匹配的歌
    matched = [_song(str(i), f"老歌{i}", netease_id=str(i)) for i in range(100)]
    # 2 首未匹配（netease_id 为空）
    unmatched = [_song("9001", "新歌A"), _song("9002", "新歌B")]
    songs = matched + unmatched

    report = await svc.archiver.archive(
        gid, state.key, "测试窗口", songs, svc.config.playlist,
        start_at=None, end_at=None, desc_songs=songs,
    )
    check("归档成功", report.ok, report.message)
    check("match_netease_id 仅被调用 2 次（未匹配数）",
          _MATCH_CALLS["n"] == 2, f"-> 实际 {_MATCH_CALLS['n']} 次")
    check("已匹配的 100 首都进入歌单", report.added >= 100, f"-> added={report.added}")


async def test_master_incremental_append() -> None:
    """总库歌单已存在，分享 2 首新歌：仅增量追加，不整库重排（reorder_to_match 不被调用）。"""
    print("\n[B] 总库分享即归档：增量追加，不整库重排")
    Archiver.match_netease_id = _spy_match  # type: ignore[assignment]
    Archiver.reorder_to_match = _spy_reorder  # type: ignore[assignment]
    _MATCH_CALLS["n"] = 0
    _REORDER_CALLED["n"] = 0

    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    svc = _CollectorService()
    svc.store = store
    svc.netease = NeteaseStub()
    svc.archiver = Archiver(svc.netease, store)
    # 开启总库 + 分享即归档
    svc.config.master.enabled = True
    svc.config.master.auto_archive = True

    gid = 7101
    # 总库里预先有 100 首已匹配歌
    existing_ids = [str(i) for i in range(100)]
    for sid in existing_ids:
        await store.add_song(gid, MASTER_KEY, _song(sid, f"总库老歌{sid}", netease_id=sid))
    # 预先建好总库歌单（模拟已归档过）
    api: NeteaseStub = svc.netease
    pid = await api.create_playlist("总库", False)
    api.tracks[pid] = list(existing_ids)  # 当前歌单含老 100 首
    await store.record_archive(
        gid, MASTER_KEY, str(pid), api.playlist_url(pid),
        len(existing_ids), len(existing_ids), 0, added_ids=existing_ids,
    )

    # 本次新分享 2 首（未匹配）
    new_songs = [_song("9101", "新总库歌A"), _song("9102", "新总库歌B")]
    await svc.auto_archive_master(gid, new_songs)

    check("reorder_to_match 未被调用（增量追加，不整库重排）",
          _REORDER_CALLED["n"] == 0, f"-> {_REORDER_CALLED['n']} 次")
    check("add_tracks 仅调用 1 次（一批新歌）",
          api.add_calls == 1, f"-> {api.add_calls} 次")
    check("歌单从 100 增至 102 首",
          len(api.tracks[pid]) == 102, f"-> {len(api.tracks[pid])}")
    # 新歌应在顶部（newest_first）
    check("新歌在歌单顶部", api.tracks[pid][:2] == ["9101", "9102"], f"-> {api.tracks[pid][:4]}")
    # added_ids 已合并
    arch = await store.get_archive(gid, MASTER_KEY)
    merged = set(arch.get("added_ids") or [])
    check("added_ids 合并为 102 首", merged == set(existing_ids) | {"9101", "9102"},
          f"-> {len(merged)} 首")


async def main() -> None:
    await test_archive_skips_matched()
    await test_master_incremental_append()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""总库相关新功能测试：跨平台匹配同步 / 总库去重拦截窗口 / 歌单链接导入 / 总库最新在上。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_master_import_dedup.py
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


class FullStub:
    """功能较全的网易云桩：建歌单/加歌(顶插)/删歌/读曲目/搜索/详情都可用。"""

    logged_in = True

    def __init__(self) -> None:
        self.pid = 9000
        self.tracks: dict[int, list[str]] = {}
        self.descs: dict[int, str] = {}
        self._seq = 9000
        self._name2id: dict[str, str] = {}

    # ---- 歌单写读 ----
    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.pid += 1
        self.tracks[self.pid] = []
        return self.pid

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> dict:
        # 模仿网易云：整批倒序插到顶部
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

    # ---- 跨平台匹配用 ----
    async def search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        parts = (keyword or "").split(" ", 1)
        name = parts[0]
        artist = parts[1] if len(parts) > 1 else "测试歌手"
        if name not in self._name2id:
            self._seq += 1
            self._name2id[name] = str(self._seq)
        return [{"id": self._name2id[name], "name": name,
                 "artists": [{"name": artist}], "dt": 200000}]

    # ---- 歌单导入用 ----
    async def song_detail(self, song_ids: list[str]) -> list[dict]:
        out = []
        for sid in song_ids:
            out.append({
                "id": int(sid),
                "name": f"曲{sid}",
                "artists": [{"name": "测试歌手"}],
                "album": {"name": "测试专辑"},
                "dt": 210000,
            })
        return out


def _song(sid: str, title: str, platform: str = "netease", netease_id: object = None) -> Song:
    return Song(
        platform=platform, song_id=sid, title=title, artists="测试歌手",
        sharer_id=1, sharer_name="张三", url="",
        netease_id=netease_id if netease_id is not None else (sid if platform == "netease" else None),
    )


def _make_svc(tmp: Path):
    store = Store(tmp / "svc.db")
    api = FullStub()
    svc = CollectorService()
    svc.store = store
    svc.netease = api
    svc.archiver = Archiver(api, store)
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.collect_override = "on"
    return svc, store, api


async def test_sync_matches_other_platform():
    """#1 同步歌单会把非网易云平台的歌也匹配进歌单。"""
    print("\n[1] 同步歌单：跨平台匹配")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7001

    # 先放一首网易云歌并同步一次（建歌单）
    await store.add_song(gid, svc.current_window().key, _song("1", "歌一"))
    await svc.sync_playlist(gid)
    pid = next(iter(api.tracks))
    check("首次同步建了歌单且含歌一", api.tracks[pid] == ["1"], f"-> {api.tracks[pid]}")

    # 再放一首 QQ 音乐歌（无 netease_id）
    qq = (await store.add_song(
        gid, svc.current_window().key, _song("qq9", "歌Q", platform="qqmusic", netease_id=None)
    ))[1]
    check("QQ 歌初始无 netease_id", qq.netease_id in (None, ""), str(qq.netease_id))

    # 再次同步（复用歌单路径）—— 应当匹配并把 QQ 歌加进歌单
    await svc.sync_playlist(gid)
    check("QQ 歌已被匹配出 netease_id", qq.row_id is not None
          and (await store.get_song_by_index(gid, svc.current_window().key, 2)) is not None)
    rel = await store.list_songs(gid, svc.current_window().key)
    matched_ids = {s.netease_id for s in rel if s.netease_id}
    check("QQ 歌的 netease_id 已落库", len(matched_ids) >= 2, str(matched_ids))
    # 歌单里应当出现匹配到的 QQ 歌 id（顶插后存在即可）
    added_qq = [tid for tid in api.tracks[pid] if tid not in ("1",)]
    check("歌单含匹配后的 QQ 歌 id", bool(added_qq), f"-> {api.tracks[pid]}")


async def test_master_dedup_intercepts_window():
    """#2 已存在于总库的歌，不再收录进当前窗口。"""
    print("\n[2] 总库去重拦截窗口收录")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7002
    config_manager.config.master.enabled = True
    config_manager.config.master.compare_on_share = True
    wk = svc.current_window().key

    # 先把某歌放进总库，模拟「已被其它窗口分享过」
    await store.add_song(gid, MASTER_KEY, _song("555", "孤勇者"), src_window="W-首发期")

    real = svc.providers.resolve

    async def _resolve_555(link):
        return _song("555", "孤勇者")
    svc.providers.resolve = _resolve_555
    try:
        r = await svc.handle_segments(
            gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "李四"
        )
        check("已在总库 -> 不入当前窗口(accepted=0)", len(r.accepted) == 0, str(r.accepted))
        check("已在总库 -> 记跨窗口重复", len(r.master_duplicated) == 1, str(r.master_duplicated))
        check("窗口歌曲数仍为 0", await store.count(gid, wk) == 0, str(await store.count(gid, wk)))
        check("首发来源窗口正确", r.master_duplicated[0].src_window == "W-首发期",
              str(r.master_duplicated[0].src_window))
    finally:
        svc.providers.resolve = real

    # 全新歌：应正常收录进窗口与总库
    async def _resolve_777(link):
        return _song("777", "全新歌")
    svc.providers.resolve = _resolve_777
    r2 = await svc.handle_segments(
        gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=777"}}], 123, "王五"
    )
    check("全新歌入窗口", len(r2.accepted) == 1, str(r2.accepted))
    check("全新歌不报重复", len(r2.master_duplicated) == 0, str(r2.master_duplicated))
    check("全新歌进入总库(共2首)", await store.count(gid, MASTER_KEY) == 2,
          str(await store.count(gid, MASTER_KEY)))
    check("全新歌进入当前窗口(共1首)", await store.count(gid, wk) == 1,
          str(await store.count(gid, wk)))


async def test_import_playlist_to_master():
    """#3 从歌单链接导入歌曲到总库。"""
    print("\n[3] 歌单链接导入总库")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7003
    config_manager.config.master.enabled = True

    # 桩：给定歌单 id 返回 3 首曲目
    async def _track_ids(pid: int) -> list[str]:
        return ["100", "200", "300"]
    api.playlist_track_ids = _track_ids

    res = await svc.import_playlist_to_master(
        gid, "https://music.163.com/#/playlist?id=123456"
    )
    check("导入成功", res.get("ok") is True, str(res))
    check("导入 3 首", res.get("added") == 3 and res.get("total") == 3, str(res))
    check("总库新增 3 首", await store.count(gid, MASTER_KEY) == 3,
          str(await store.count(gid, MASTER_KEY)))

    songs = await store.list_songs(gid, MASTER_KEY)
    check("导入的歌标记 matched=1 且平台=netease",
          all(s.matched and s.platform == "netease" for s in songs), str(songs))
    check("导入的歌 src_window=import",
          all(s.src_window == "import" for s in songs), str([s.src_window for s in songs]))

    # 重复导入同一歌单应去重（added=0）
    res2 = await svc.import_playlist_to_master(
        gid, "https://music.163.com/#/playlist?id=123456"
    )
    check("重复导入去重(added=0)", res2.get("added") == 0, str(res2))
    check("总库仍为 3 首", await store.count(gid, MASTER_KEY) == 3,
          str(await store.count(gid, MASTER_KEY)))

    # 非法链接
    bad = await svc.import_playlist_to_master(gid, "不是链接")
    check("非法链接返回失败", bad.get("ok") is False, str(bad))


async def test_master_newest_first():
    """#4 总库列表按收录时间倒序（越新越靠前）。"""
    print("\n[4] 总库最新在上")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7004

    import time
    base = time.time()
    for i, ts in enumerate((base, base + 10, base + 20)):
        s = _song(str(100 + i), f"歌{i}")
        s.created_at = ts
        await store.add_song(gid, MASTER_KEY, s, src_window="import")

    newest = await store.list_songs(gid, MASTER_KEY, newest_first=True)
    oldest = await store.list_songs(gid, MASTER_KEY, newest_first=False)
    check("最新在前第一项应为 歌2", newest[0].title == "歌2", str([s.title for s in newest]))
    check("默认顺序第一项应为 歌0", oldest[0].title == "歌0", str([s.title for s in oldest]))
    check("两种排序互为逆序", [s.title for s in newest] == [s.title for s in oldest][::-1],
          f"{[s.title for s in newest]} vs {[s.title for s in oldest]}")


async def main() -> None:
    await test_sync_matches_other_platform()
    await test_master_dedup_intercepts_window()
    await test_import_playlist_to_master()
    await test_master_newest_first()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

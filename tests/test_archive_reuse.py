"""同窗口复用歌单增量归档 + 分享即归档开关验证。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_archive_reuse.py
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
from music_collector.config import PlaylistConfig, config_manager  # noqa: E402
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


class StubNetease:
    """异步 stub：记录建歌单 / 加歌 / 写简介调用。"""

    logged_in = True

    def __init__(self) -> None:
        self.created: list[tuple[str, bool]] = []
        self.added: list[tuple[int, list[str]]] = []
        self.descs: dict[int, str] = {}

    async def create_playlist(self, name: str, privacy: bool) -> int:
        pid = 9000 + len(self.created)
        self.created.append((name, privacy))
        return pid

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> None:
        self.added.append((playlist_id, list(batch)))

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        self.descs[playlist_id] = desc
        return True, "ok"

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


def _song(sid: str, title: str) -> Song:
    return Song(platform="netease", song_id=sid, title=title, artists="测试歌手")


async def test_store_migration() -> None:
    """老库（archives 无 added_ids 列）init 后自动补列。"""
    print("\n[A] 老库迁移 added_ids")
    import aiosqlite

    tmp = Path(tempfile.mkdtemp())
    db = tmp / "old.db"
    async with aiosqlite.connect(db) as d:
        await d.execute(
            "CREATE TABLE archives (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_id INTEGER NOT NULL, window_key TEXT NOT NULL, playlist_id TEXT, "
            "playlist_url TEXT, total INTEGER NOT NULL DEFAULT 0, "
            "added INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL NOT NULL, UNIQUE(group_id, window_key))"
        )
        await d.commit()

    store = Store(db)
    await store.init()
    await store.record_archive(1001, "W1", "8888", "https://x", 2, 2, 0)
    arch = await store.get_archive(1001, "W1")
    check("老库迁移后可读 added_ids", "added_ids" in arch and arch["added_ids"] == set(),
          f"-> {arch}")
    check("老库 playlist_id 保留", str(arch.get("playlist_id")) == "8888")


async def test_reuse_append() -> None:
    """同窗口二次归档：复用歌单、只加新歌、不重复建。"""
    print("\n[B] 同窗口复用歌单增量追加")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    api = StubNetease()
    archiver = Archiver(api, store)
    cfg = PlaylistConfig()

    gid, wkey = 2001, "W20260825"
    a, b, c = _song("1", "歌一"), _song("2", "歌二"), _song("3", "歌三")

    # 首次归档 [1,2]
    r1 = await archiver.archive(gid, wkey, "测试窗口", [a, b], cfg)
    check("首次归档成功", r1.ok and r1.created_new, r1.message)
    check("新建 1 个歌单", len(api.created) == 1)
    check("加歌 2 首", r1.added == 2)

    # 二次归档 [1,2,3]：3 是新歌
    r2 = await archiver.archive(gid, wkey, "测试窗口", [a, b, c], cfg)
    check("二次归档成功且复用", r2.ok and not r2.created_new, r2.message)
    check("不再新建歌单", len(api.created) == 1, f"created={len(api.created)}")
    check("只追加新歌 1 首", r2.added == 1, f"added={r2.added}")
    check("追加的是新歌 id", r2.playlist_id == api.added[-1][0] and api.added[-1][1] == ["3"],
          f"-> {api.added[-1]}")

    arch = await store.get_archive(gid, wkey)
    check("added_ids 合并为 3 个", arch["added_ids"] == {"1", "2", "3"}, f"-> {arch['added_ids']}")

    # 三次归档 [1,2,3]：无新歌
    r3 = await archiver.archive(gid, wkey, "测试窗口", [a, b, c], cfg)
    check("无新歌时 ok 且复用", r3.ok and not r3.created_new)
    check("无新歌 added=0", r3.added == 0)
    check("summary 提示已最新", "已是最新" in r3.summary())


async def test_new_playlist_for_new_window() -> None:
    """不同窗口互不影响：新窗口归档仍新建歌单。"""
    print("\n[C] 不同窗口各自独立")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()
    api = StubNetease()
    archiver = Archiver(api, store)
    cfg = PlaylistConfig()

    gid = 3001
    r1 = await archiver.archive(gid, "W1", "窗口1", [_song("1", "一")], cfg)
    r2 = await archiver.archive(gid, "W2", "窗口2", [_song("2", "二")], cfg)
    check("W1 新建", r1.ok and r1.created_new)
    check("W2 新建（不串用 W1 歌单）", r2.ok and r2.created_new)
    check("建了 2 个歌单", len(api.created) == 2)


async def test_run_archive_only_new_consumes_seq() -> None:
    """复用追加不消耗 pending_name / 期号；新建才消耗。"""
    print("\n[D] run_archive 期号/一次性名只在新建时消耗")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    svc = _CollectorService()
    svc.store = store
    svc.netease = StubNetease()
    svc.archiver = Archiver(svc.netease, store)

    # 把配置落盘指向临时文件，避免污染真实 data/config.yaml
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.playlist.seq = 5
    config_manager.config.playlist.pending_name = "临时歌单名"
    config_manager.config.playlist.seq_auto_increment = True

    gid = 4001
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "一"))

    r1 = await svc.run_archive(gid, window=state)
    check("首次归档成功", r1.ok and r1.created_new)
    check("期号自增到 6", config_manager.config.playlist.seq == 6, f"seq={config_manager.config.playlist.seq}")
    check("一次性名已清空", config_manager.config.playlist.pending_name == "")

    # 再分享一首新歌，再次归档（复用）
    await store.add_song(gid, state.key, _song("2", "二"))
    r2 = await svc.run_archive(gid, window=state)
    check("二次归档复用", r2.ok and not r2.created_new)
    check("复用不消耗期号", config_manager.config.playlist.seq == 6, f"seq={config_manager.config.playlist.seq}")
    check("复用不生成新名", config_manager.config.playlist.pending_name == "")


async def test_auto_archive_on_share() -> None:
    """分享即归档：新歌入库后自动追加到当前窗口歌单。"""
    print("\n[E] 分享即归档")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "c.db")
    await store.init()

    svc = _CollectorService()
    svc.store = store
    svc.netease = StubNetease()
    svc.archiver = Archiver(svc.netease, store)

    gid = 5001
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "一"))

    await svc.auto_archive_songs(gid, state, [_song("1", "一")])
    arch = await store.get_archive(gid, state.key)
    check("自动归档后 added_ids 含该歌", arch is not None and "1" in arch["added_ids"],
          f"-> {arch}")

    # 再分享一首，自动追加；歌单总数 2，且未新建（同窗口）
    api: StubNetease = svc.netease
    created_before = len(api.created)
    await store.add_song(gid, state.key, _song("2", "二"))
    await svc.auto_archive_songs(gid, state, [_song("2", "二")])
    arch2 = await store.get_archive(gid, state.key)
    check("二次自动归档复用歌单", len(api.created) == created_before, f"created={len(api.created)}")
    check("added_ids 累积 2 个", arch2 is not None and arch2["added_ids"] == {"1", "2"})


async def main() -> None:
    await test_store_migration()
    await test_reuse_append()
    await test_new_playlist_for_new_window()
    await test_run_archive_only_new_consumes_seq()
    await test_auto_archive_on_share()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

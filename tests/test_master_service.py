"""总库在 service 层的填充 / 查重 / 归档 / 同步（真实 store + 桩 netease）。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_master_service.py
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
    """桩：记录建歌单 / 加歌 / 删歌 / 写简介调用。"""

    logged_in = True

    def __init__(self) -> None:
        self.created: list[tuple[str, bool]] = []
        self.added: list[tuple[int, list[str]]] = []
        self.removed: list[tuple[int, list[str]]] = []
        self.descs: dict[int, str] = {}

    async def create_playlist(self, name: str, privacy: bool) -> int:
        pid = 9000 + len(self.created)
        self.created.append((name, privacy))
        return pid

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> None:
        self.added.append((playlist_id, list(batch)))

    async def remove_tracks(self, playlist_id: int, batch: list[str]) -> None:
        self.removed.append((playlist_id, list(batch)))

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        self.descs[playlist_id] = desc
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
    # 隔离配置：指向临时文件，load 会复制示例配置（含 master 段）
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.collect_override = "on"  # 强制收集期，便于 handle_segments
    return svc, store, api


async def test_aggregate_to_master():
    print("\n[A] aggregate_to_master 回填 + 跨窗口去重")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6001
    await store.add_song(gid, "W1", _song("1", "歌一"))
    await store.add_song(gid, "W1", _song("2", "歌二"))
    await store.add_song(gid, "W2", _song("1", "歌一"))  # 与 W1 重复
    await store.add_song(gid, "W2", _song("3", "歌三"))

    n = await svc.aggregate_to_master(gid)
    check("汇总新增 3 首（歌一/二/三去重）", n == 3, f"n={n}")
    cnt = await store.count(gid, MASTER_KEY)
    check("总库共 3 首", cnt == 3, f"cnt={cnt}")


async def test_handle_segments_master_dup():
    print("\n[B] handle_segments 跨窗口重复检测")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6002
    config_manager.config.master.enabled = True
    config_manager.config.master.compare_on_share = True

    # 先手动把这首歌放进总库，模拟「已被其他窗口分享过」
    await store.add_song(gid, MASTER_KEY, _song("555", "孤勇者"))

    # 用真实 detector 解析链接（无需联网），只桩 providers.resolve 返回固定歌曲
    real_resolve = svc.providers.resolve

    async def _resolve(link):
        return _song("555", "孤勇者")

    svc.providers.resolve = _resolve
    try:
        r = await svc.handle_segments(
            gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=555"}}], 123, "李四"
        )
        check("本次窗口收录成功", len(r.accepted) == 1, str(r.accepted))
        check("命中总库已存在 -> master_duplicated", len(r.master_duplicated) == 1, str(r.master_duplicated))
    finally:
        svc.providers.resolve = real_resolve

    # 反向：一首全新的歌，不应判为总库重复，且应进入总库
    async def _resolve2(link):
        return _song("777", "全新歌")

    svc.providers.resolve = _resolve2
    r2 = await svc.handle_segments(
        gid, [{"type": "text", "data": {"text": "https://music.163.com/song?id=777"}}], 123, "王五"
    )
    check("全新歌入总库不报重复", len(r2.master_duplicated) == 0, str(r2.master_duplicated))
    check("全新歌同时进入总库", await store.count(gid, MASTER_KEY) == 2, str(await store.count(gid, MASTER_KEY)))


async def test_run_master_archive_seq():
    print("\n[C] run_master_archive 建歌单 + 期号/一次性名消耗")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6003
    config_manager.config.master.seq = 1
    config_manager.config.master.pending_name = "总库临时名"
    config_manager.config.master.seq_auto_increment = True
    await store.add_song(gid, MASTER_KEY, _song("1", "歌一"))
    await store.add_song(gid, MASTER_KEY, _song("2", "歌二"))

    rep = await svc.run_master_archive(gid)
    check("归档成功且新建歌单", rep.ok and rep.created_new, rep.message)
    check("建了 1 个歌单", len(api.created) == 1, f"created={len(api.created)}")
    check("一次性名已清空", config_manager.config.master.pending_name == "")
    check("期号自增到 2", config_manager.config.master.seq == 2, f"seq={config_manager.config.master.seq}")
    arch = await store.get_archive(gid, MASTER_KEY)
    check("归档记录指向总库歌单", arch is not None and str(arch["playlist_id"]) == str(rep.playlist_id), f"-> {arch}")
    check("added_ids 含两首", arch is not None and arch["added_ids"] == {"1", "2"}, f"-> {arch['added_ids'] if arch else None}")


async def test_sync_master_playlist():
    print("\n[D] sync_master_playlist 增 + 删 + 简介")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6004
    await store.add_song(gid, MASTER_KEY, _song("1", "歌一"))
    await store.add_song(gid, MASTER_KEY, _song("2", "歌二"))

    r1 = await svc.sync_master_playlist(gid)
    check("首次同步建歌单成功", r1["ok"], r1)
    pid = r1["playlist_id"]
    arch = await store.get_archive(gid, MASTER_KEY)
    check("歌单收录两首", arch is not None and arch["added_ids"] == {"1", "2"}, f"-> {arch['added_ids'] if arch else None}")
    check("archiver 实际加歌", any("1" in b for _, b in api.added), f"added={api.added}")

    # 删掉歌二，再同步 -> 应移除 1 首
    await store.delete_songs_by_indices(gid, MASTER_KEY, [2])
    r2 = await svc.sync_master_playlist(gid)
    check("二次同步移除 1 首", r2["removed"] == 1, f"removed={r2['removed']}")
    check("remove_tracks 被调用", any(pid == p for p, _ in api.removed), f"removed={api.removed}")


async def test_preview_master():
    print("\n[E] 总库命名 / 简介预览")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6005
    await store.add_song(gid, MASTER_KEY, _song("1", "歌一"))
    name = await svc.preview_master_name(gid)
    desc = await svc.preview_master_description(gid)
    check("歌单名含群号", str(gid) in name, name)
    check("简介含总数", "共 1 首" in desc, desc)


async def test_auto_archive_master():
    print("\n[F] auto_archive_master 静默追加到总库歌单")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 6006
    await store.add_song(gid, MASTER_KEY, _song("1", "歌一"))
    await svc.auto_archive_master(gid)
    arch = await store.get_archive(gid, MASTER_KEY)
    check("自动归档后总库有歌单", arch is not None, f"arch={arch}")
    check("added 含该歌", arch and "1" in arch["added_ids"], f"-> {arch}")


async def main() -> None:
    await test_aggregate_to_master()
    await test_handle_segments_master_dup()
    await test_run_master_archive_seq()
    await test_sync_master_playlist()
    await test_preview_master()
    await test_auto_archive_master()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

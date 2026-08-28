"""简介补写一致性验证：补写必须按当前数据重新生成，不能重推旧文本。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_desc_rebuild.py
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


class FlakyNetease:
    """前 N 次写简介失败（模拟网易云频控），之后成功。"""

    logged_in = True

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_remaining = fail_times
        self.descs: dict[int, str] = {}

    async def create_playlist(self, name: str, privacy: bool) -> int:
        return 7001

    async def add_tracks(self, playlist_id: int, batch: list[str]) -> None:
        return None

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            return False, "频控拦截"
        self.descs[playlist_id] = desc
        return True, "ok"

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


def _song(sid: str, title: str, sharer: str, sharer_id: int) -> Song:
    return Song(
        platform="netease", song_id=sid, title=title, artists="测试歌手",
        sharer_id=sharer_id, sharer_name=sharer,
    )


async def _fresh_service(tmp: Path):
    store = Store(tmp / "c.db")
    await store.init()
    svc = _CollectorService()
    svc.store = store
    svc.netease = FlakyNetease(fail_times=1)
    svc.archiver = Archiver(svc.netease, store)
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.playlist = PlaylistConfig(desc_retry=1)
    return svc, store


async def test_pending_carries_context() -> None:
    """归档失败入队时要带上 window_key 与歌曲快照。"""
    print("\n[A] 入队时保存补写上下文")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    gid = 6001
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "晴天", "张三", 111))
    await store.add_song(gid, state.key, _song("2", "稻香", "李四", 222))

    cfg = PlaylistConfig(desc_retry=1)
    rep = await svc.archiver.archive(gid, state.key, state.label,
                                     await store.list_songs(gid, state.key), cfg)
    check("归档流程走完（简介写入失败）", rep.ok and not rep.desc_ok, rep.message)

    pending = await store.list_pending_desc(gid)
    check("已入队 1 条", len(pending) == 1, f"-> {len(pending)}")
    row = pending[0]
    check("存档 window_key", row.get("window_key") == state.key, f"-> {row.get('window_key')}")
    snap = row.get("snapshot") or "{}"
    check("存档 snapshot 含 2 首歌", '"晴天"' in snap and '"稻香"' in snap)


async def test_retry_reflects_deleted_song() -> None:
    """补写前删掉的歌，不该再出现在补写的简介里。"""
    print("\n[B] 补写反映删歌")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    gid = 6002
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "晴天", "张三", 111))
    await store.add_song(gid, state.key, _song("2", "稻香", "李四", 222))

    cfg = PlaylistConfig(desc_retry=1)
    songs = await store.list_songs(gid, state.key)
    await svc.archiver.archive(gid, state.key, state.label, songs, cfg)
    old_desc = (await store.list_pending_desc(gid))[0]["description"]
    check("旧文本含两首歌", "晴天" in old_desc and "稻香" in old_desc)

    # 删掉第 1 首（晴天 / 张三）
    await store.delete_songs_by_indices(gid, state.key, [1])

    ok, failed = await svc.retry_pending_desc(gid)
    check("补写成功", ok == 1 and failed == 0, f"ok={ok} failed={failed}")

    written = svc.netease.descs.get(7001, "")
    check("补写文本已重新生成", written != old_desc)
    check("被删的歌不再出现", "晴天" not in written, f"-> {written[:80]}")
    check("保留的歌仍在", "稻香" in written and "李四" in written)


async def test_retry_reflects_alias_change() -> None:
    """补写前改了昵称映射，补写文本要用新名字。"""
    print("\n[C] 补写反映昵称映射变更")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    gid = 6003
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "晴天", "张三", 111))

    cfg = PlaylistConfig(desc_retry=1)
    await svc.archiver.archive(gid, state.key, state.label,
                               await store.list_songs(gid, state.key), cfg)

    # 归档后改昵称映射
    config_manager.config.playlist.sharer_aliases = {"张三": "Jacksing"}

    ok, _ = await svc.retry_pending_desc(gid)
    check("补写成功", ok == 1)
    written = svc.netease.descs.get(7001, "")
    check("简介用新昵称", "Jacksing" in written, f"-> {written[:80]}")
    check("简介不含旧昵称", "张三" not in written)


async def test_retry_uses_snapshot_when_cleared() -> None:
    """窗口歌曲被清空时，用归档快照重建而不是写空清单。"""
    print("\n[D] 歌曲清空后按快照重建")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    gid = 6004
    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1", "晴天", "张三", 111))
    await store.add_song(gid, state.key, _song("2", "稻香", "李四", 222))

    cfg = PlaylistConfig(desc_retry=1)
    await svc.archiver.archive(gid, state.key, state.label,
                               await store.list_songs(gid, state.key), cfg)

    # 归档后清空本期（after_archive 场景）
    await store.delete_window(gid, state.key)
    check("窗口已清空", await store.count(gid, state.key) == 0)

    ok, _ = await svc.retry_pending_desc(gid)
    check("补写成功", ok == 1)
    written = svc.netease.descs.get(7001, "")
    check("快照重建出完整清单", "晴天" in written and "稻香" in written, f"-> {written[:80]}")
    check("分享人仍在", "张三" in written and "李四" in written)


async def test_retry_falls_back_to_old_text() -> None:
    """既无当前歌曲也无快照时，退回存档的旧文本。"""
    print("\n[E] 无数据可重建时退回旧文本")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    gid = 6005
    # 这条用例不走归档，先把 stub 的"频控失败"额度清掉
    svc.netease.fail_remaining = 0
    # 手工塞一条既无 window_key 也无快照的历史记录
    await store.save_pending_desc("7001", "老歌单", gid, "旧简介文本", "频控")
    ok, _ = await svc.retry_pending_desc(gid)
    check("补写成功", ok == 1)
    written = svc.netease.descs.get(7001, "")
    check("退回旧文本", written == "旧简介文本", f"-> {written[:60]}")


async def main() -> None:
    await test_pending_carries_context()
    await test_retry_reflects_deleted_song()
    await test_retry_reflects_alias_change()
    await test_retry_uses_snapshot_when_cleared()
    await test_retry_falls_back_to_old_text()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

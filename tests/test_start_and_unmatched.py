"""验证三项改动：未识别不入榜 / 未匹配标注分享者 / 开始收录广播。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_start_and_unmatched.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（require apscheduler）

from music_collector.models import MusicLink, Song
from music_collector.service import service
import music_collector.scheduler as sch_mod
from music_collector.store import Store
from music_collector.archiver import Archiver
from music_collector.config import PlaylistConfig


# ---------------------------------------------------------------- 1. 未识别不入榜

def test_unidentified_skipped_but_recognized_added():
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    asyncio.run(store.init())
    real_store = service.store
    real_resolve = service.providers.resolve
    service.store = store

    state = {"unidentified": True}

    async def _fake_resolve(link: MusicLink) -> Song:
        if state["unidentified"]:
            return Song(platform=link.platform, song_id=link.key, title="未识别歌曲")
        return Song(platform="netease", song_id="123", title="孤勇者", artists="陈奕迅")

    service.providers.resolve = _fake_resolve
    # 一个能被 detector 识别成音乐链接的片段（无需联网，resolve 已被 mock）
    segments = [{"type": "text", "data": {"text": "https://music.163.com/song?id=123"}}]
    try:
        # 第一次：resolve 返回"未识别" -> 不应入榜
        r1 = asyncio.run(service.handle_segments(999, segments, 123, "张三"))
        assert len(r1.unidentified) == 1, r1.unidentified
        assert len(r1.accepted) == 0, r1.accepted
        songs_after_unident = asyncio.run(store.list_songs(999, service.current_window().key))
        assert len(songs_after_unident) == 0, songs_after_unident

        # 第二次：resolve 返回正常歌曲 -> 不应被判为未识别
        state["unidentified"] = False
        r2 = asyncio.run(service.handle_segments(999, segments, 123, "张三"))
        assert len(r2.unidentified) == 0, r2.unidentified
        assert len(r2.accepted) + len(r2.outside_window) == 1, (r2.accepted, r2.outside_window)
    finally:
        service.store = real_store
        service.providers.resolve = real_resolve


# ---------------------------------------------------------------- 2. 未匹配标注分享者

class _StubAPI:
    def __init__(self) -> None:
        self.logged_in = True
        self.calls: list[tuple] = []
        self._pid = 555

    async def search_songs(self, keyword, limit=10):
        return []

    async def create_playlist(self, name, privacy=False):
        self.calls.append(("create", name, privacy))
        return self._pid

    async def add_tracks(self, playlist_id, track_ids):
        self.calls.append(("add", playlist_id, list(track_ids)))
        return {"code": 200}

    async def update_description(self, playlist_id, desc, name=""):
        self.calls.append(("desc", playlist_id, desc, name))

    def playlist_url(self, playlist_id):
        return f"https://music.163.com/#/playlist?id={playlist_id}"


class _StubStore:
    async def mark_matched(self, row_id, netease_id):
        pass

    async def record_archive(self, *a, **k):
        pass


def test_unmatched_lists_sharer_in_description_and_summary():
    api = _StubAPI()
    archiver = Archiver(api, _StubStore())
    songs = [
        # 能被匹配（网易云数字 id）
        Song(platform="netease", song_id="111", title="孤勇者", artists="陈奕迅",
             sharer_id=1, sharer_name="张三", netease_id="111", matched=True),
        # 匹配不到（QQ 来源，无 netease_id，搜索兜底也空）
        Song(platform="qq", song_id="abc", title="晴天", artists="周杰伦",
             sharer_id=2, sharer_name="李四"),
    ]
    cfg = PlaylistConfig()
    report = asyncio.run(
        archiver.archive(123, "W20260808", "测试窗口", songs, cfg,
                         start_at=None, end_at=None)
    )
    assert report.ok, report.message
    assert len(report.unmatched) == 1

    # summary 含分享者
    assert "李四" in report.summary(), report.summary()

    # 简介含分享者（方便在网易云里查找）
    desc_calls = [c for c in api.calls if c[0] == "desc"]
    _, pid, desc, name = desc_calls[0]
    assert "李四" in desc, desc
    assert "晴天" in desc


# ---------------------------------------------------------------- 3. 开始收录广播

def test_job_start_broadcasts_to_known_groups():
    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def send_group_msg(self, group_id, message):
            self.calls.append((group_id, str(message)))

    fake = FakeBot()
    sch_mod._get_bot = lambda: fake  # noqa: SLF001
    real_all = service.store.all_groups

    async def _all():
        return [555, 666]

    service.store.all_groups = _all
    try:
        asyncio.run(sch_mod.job_start())
    finally:
        service.store.all_groups = real_all

    assert fake.calls, "开始提醒没有广播出去"
    assert any(g in (555, 666) for g, _ in fake.calls)
    assert any("开始" in msg for _, msg in fake.calls), [m for _, m in fake.calls]


if __name__ == "__main__":
    test_unidentified_skipped_but_recognized_added()
    test_unmatched_lists_sharer_in_description_and_summary()
    test_job_start_broadcasts_to_known_groups()
    print("start_and_unmatched tests OK")

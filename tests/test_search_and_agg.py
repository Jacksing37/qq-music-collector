"""搜索与「当前窗口汇总到总库」的后端覆盖。

直接跑（不依赖 pytest）：``python tests/test_search_and_agg.py``
"""
import asyncio
import datetime
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src/plugins")

import nonebot

nonebot.init(driver="~fastapi")

from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.service import CollectorService  # noqa: E402
from music_collector.store import MASTER_KEY, Store  # noqa: E402


class StubNetease:
    logged_in = True

    async def create_playlist(self, name, privacy):
        return 9000

    async def add_tracks(self, pid, batch):
        pass

    async def remove_tracks(self, pid, batch):
        pass

    async def update_description(self, pid, desc, name=""):
        return True, "ok"

    def playlist_url(self, pid):
        return f"https://music.163.com/playlist/{pid}"


def _mk(song_id, title, sharer, day):
    ca = time.mktime(datetime.datetime(2026, 9, day, 12, 0, 0).timetuple())
    return Song(
        platform="netease", song_id=song_id, title=title, artists="测试歌手",
        sharer_id=1, sharer_name=sharer, url="", netease_id=song_id, created_at=ca,
    )


async def main():
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "svc.db")
    api = StubNetease()
    svc = CollectorService()
    svc.store = store
    svc.netease = api
    svc.archiver = Archiver(api, store)
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.collect_override = "on"
    await store.init()

    gid = 6201
    wk = "W20260911-1200"
    for day, sid, title, sharer in [
        (1, "111", "孤勇者", "张三"),
        (2, "222", "晴天", "李四"),
        (3, "333", "稻香", "张三"),
    ]:
        await store.add_song(gid, wk, _mk(sid, title, sharer, day))

    # 关键词：标题
    assert len(await store.search_songs(gid, wk, "晴天")) == 1
    # 关键词：分享者（不区分大小写）
    assert len(await store.search_songs(gid, wk, "张三")) == 2
    # 关键词：song_id
    assert len(await store.search_songs(gid, wk, "111")) == 1
    # 日期
    r = await store.search_songs(gid, wk, "", "2026-09-02")
    assert len(r) == 1 and r[0].song_id == "222", r
    # 日期 + 关键词组合
    r = await store.search_songs(gid, wk, "稻香", "2026-09-03")
    assert len(r) == 1, r
    # 无命中
    assert len(await store.search_songs(gid, wk, "不存在的歌")) == 0

    # 当前窗口汇总到总库
    n = await svc.aggregate_window_to_master(gid, wk)
    assert n == 3, n
    # 重复汇总不再计入
    assert await svc.aggregate_window_to_master(gid, wk) == 0
    # 总库里能检索到
    mr = await store.search_songs(gid, MASTER_KEY, "孤勇者", newest_first=True)
    assert len(mr) == 1, mr

    print("ALL_PASS search + aggregate_window_to_master 覆盖通过")


if __name__ == "__main__":
    asyncio.run(main())

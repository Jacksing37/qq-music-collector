"""验证歌单简介在归档时正确写入（含分享者清单与歌单名）。

复现用户反馈的「简介还是空的」：根因是 update_description 用了已下线的
/playlist/desc/update 接口并静默吞掉异常。修复后改走 playlist/update 且必须带 name。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（require apscheduler）

from datetime import datetime

from music_collector.archiver import Archiver
from music_collector.config import PlaylistConfig
from music_collector.models import Song


class _StubAPI:
    """记录所有歌单写操作（不联网）。"""

    def __init__(self) -> None:
        self.logged_in = True
        self.calls: list[tuple] = []
        self._pid = 777

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
        return True, "stub"

    def playlist_url(self, playlist_id):
        return f"https://music.163.com/#/playlist?id={playlist_id}"


class _StubStore:
    async def mark_matched(self, row_id, netease_id):
        pass

    async def drop_pending_desc(self, playlist_id):
        pass

    async def record_archive(self, *a, **k):
        pass


def _song(title, artist, sharer, sid):
    return Song(
        platform="netease",
        song_id=str(sid),
        title=title,
        artists=artist,
        sharer_id=1000 + sid,
        sharer_name=sharer,
        netease_id=str(sid),
        matched=True,
    )


def test_description_written_with_sharer_list_and_name():
    api = _StubAPI()
    archiver = Archiver(api, _StubStore())
    songs = [
        _song("孤勇者", "周杰伦", "张三", 1),
        _song("晴天", "周杰伦", "李四", 2),
    ]
    cfg = PlaylistConfig()  # 默认 include_sharers=True, sharer_style="list"
    report = asyncio.run(
        archiver.archive(
            123456, "W20260808", "测试窗口", songs, cfg,
            start_at=datetime(2026, 8, 8), end_at=datetime(2026, 8, 8),
        )
    )
    assert report.ok, report.message

    # 必须有一次 update_description 调用
    desc_calls = [c for c in api.calls if c[0] == "desc"]
    assert desc_calls, "update_description 没有被调用"
    _, pid, desc, name = desc_calls[0]

    # 简介非空，且包含「谁分享了什么歌」
    assert desc.strip(), "简介为空（bug 未修复）"
    assert "张三" in desc and "孤勇者" in desc, "简介缺少分享者清单"
    assert "李四" in desc and "晴天" in desc, "简介缺少分享者清单"

    # 必须带上 name，否则标题会被清空
    assert name, "update_description 未传 name，标题会被清空"
    # 默认模板：群歌单 测试窗口
    assert name == "群歌单 测试窗口", f"name 渲染异常: {name!r}"


def test_description_endpoint_is_desc_update():
    """验证底层 API 实际走的通道：

    - linuxapi ``/playlist/desc/update`` 写简介（payload 含 id + desc）
    - 写后读回校验，防止"接口 200 其实没写进去"的假成功
    - 歌单标题（name）在 create_playlist 时已正确设置，desc 通道不碰标题，
      因此 update_description 成功路径不依赖 name 也能保证标题不被清空
    """
    from music_collector.netease_api import NeteaseAPI

    captured: dict[str, dict] = {}

    class _Fake(NeteaseAPI):
        async def _linux_post(self, path, payload):
            captured[path] = dict(payload)
            return {"code": 200}

        async def playlist_description(self, playlist_id):
            # 读回校验：返回与刚写入一致的内容
            return captured.get("/playlist/desc/update", {}).get("desc", "")

    api = _Fake.__new__(_Fake)
    NeteaseAPI.__init__(api, Path(tempfile.mktemp(suffix=".json")))  # 不实际落盘
    api._cookies["MUSIC_U"] = "dummy"  # 视为已登录
    ok, note = asyncio.run(api.update_description(5, "测试简介内容", name="歌单名"))
    assert ok, note

    desc_call = captured.get("/playlist/desc/update")
    assert desc_call, "未调用 /playlist/desc/update 写简介"
    assert desc_call.get("desc") == "测试简介内容"
    assert desc_call.get("id") == "5"


if __name__ == "__main__":
    test_description_written_with_sharer_list_and_name()
    test_description_endpoint_is_desc_update()
    print("description tests OK")

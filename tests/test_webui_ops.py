"""WebUI 预览与实时操作逻辑测试（纯函数，不依赖 HTTP / 真实数据库）。

用假 service 验证 build_overview / dispatch_action 的取数与分支。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")

from music_collector.webui import build_overview, dispatch_action  # noqa: E402


class _Song:
    def __init__(self, title, artists="", sharer_name="", platform="netease",
                 netease_id=None, matched=False, sharer_id=0):
        self.title = title
        self.artists = artists
        self.sharer_name = sharer_name
        self.sharer_id = sharer_id
        self.platform = platform
        self.netease_id = netease_id
        self.matched = matched


class _Playlist:
    """webui 的 _song_item 会读 playlist.sharer_aliases。"""

    def __init__(self):
        self.sharer_aliases = {}


class _State:
    def __init__(self, key="2026-W33", label="第33周", collecting=True):
        self.key = key
        self.label = label
        self.collecting = collecting


class _Report:
    def __init__(self, ok=True, added=3, message="", playlist_url="https://music.163.com/pl/1", playlist_id=1):
        self.ok = ok
        self.added = added
        self.message = message
        self.playlist_url = playlist_url
        self.playlist_id = playlist_id


class _FakeService:
    def __init__(self, songs_by_group=None, groups=None, windows=None, logged_in=True):
        self._songs = songs_by_group or {123: [_Song("晴天", "周杰伦", "张三"), _Song("稻香", "周杰伦", "李四")]}
        self._groups = groups
        self._windows = windows or [("2026-W33", 2)]
        self._logged = logged_in
        self.override_calls = []
        self.archive_calls = []
        self.clear_calls = []
        self.clear_idx_calls = []
        self.preview_name = "Wk.1歌单"
        self.preview_desc = "1. 张三 分享《晴天》"

    class _Cfg:
        def __init__(self, groups=None):
            self.groups = groups or []
            self.collect_override = "auto"
            self.playlist = _Playlist()
    class _Win:
        def __init__(self): self.key = "2026-W33"
    @property
    def config(self): return self._Cfg(self._groups)
    def current_window(self): return _State()
    @property
    def netease(self):
        class _N:
            logged_in = self._logged
        return _N()
    class _Store:
        def __init__(self, parent): self._p = parent
        async def groups_in_window(self, wk): return list(self._p._songs.keys())
        async def list_songs(self, gid, wk): return self._p._songs.get(gid, [])
    @property
    def store(self): return self._Store(self)
    def set_collect_override(self, value):
        self.override_calls.append(value)
        return {"on": "已开启", "off": "已关闭", "auto": "已恢复"}[value]
    async def target_groups(self, wk): return list(self._songs.keys())
    async def windows_with_counts(self, gid=None): return list(self._windows)
    async def run_archive(self, gid, window=None, name_override=""):
        self.archive_calls.append((gid, name_override))
        return _Report()
    async def clear_window(self, gid, wk):
        self.clear_calls.append((gid, wk)); return 2
    async def clear_indices(self, gid, wk, indices):
        self.clear_idx_calls.append((gid, wk, list(indices))); return len(indices)
    async def preview_playlist_name(self, gid): return self.preview_name
    async def rebuild_description(self, gid): return self.preview_desc


def _patch(service):
    import music_collector.webui as w
    w.service = service


def test_build_overview_structure():
    svc = _FakeService()
    _patch(svc)
    import asyncio
    ov = asyncio.get_event_loop().run_until_complete(build_overview())
    assert ov["window"]["key"] == "2026-W33"
    assert ov["netease_logged_in"] is True
    assert len(ov["windows"]) == 1
    g = ov["groups"][0]
    assert g["group_id"] == 123
    assert g["count"] == 2
    assert g["songs"][0]["title"] == "晴天"
    assert g["songs"][0]["platform_name"] == "网易云音乐"
    assert g["songs"][0]["index"] == 1


def test_dispatch_start_stop_auto():
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(dispatch_action({"action": "start"}))
    assert r["ok"] and svc.override_calls == ["on"]
    r = asyncio.get_event_loop().run_until_complete(dispatch_action({"action": "stop"}))
    assert svc.override_calls[-1] == "off"
    r = asyncio.get_event_loop().run_until_complete(dispatch_action({"action": "auto"}))
    assert svc.override_calls[-1] == "auto"


def test_dispatch_archive_and_all():
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "archive", "group_id": 123}))
    assert r["ok"] and svc.archive_calls == [(123, "")]
    r = asyncio.get_event_loop().run_until_complete(dispatch_action({"action": "archive_all"}))
    assert r["ok"] and svc.archive_calls[-1][0] == 123


def test_dispatch_delete_and_clear():
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "delete", "group_id": 123, "window_key": "2026-W33", "indices": [1, 2]}))
    assert r["ok"] and svc.clear_idx_calls == [(123, "2026-W33", [1, 2])]
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "clear", "group_id": 123, "window_key": "2026-W33"}))
    assert r["ok"] and svc.clear_calls == [(123, "2026-W33")]


def test_dispatch_preview():
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "preview_name", "group_id": 123}))
    assert r["data"]["name"] == "Wk.1歌单"
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "preview_desc", "group_id": 123}))
    assert "晴天" in r["data"]["description"]


def test_build_overview_includes_current_window_when_empty():
    # windows_with_counts 返回空（还没有任何收集记录）时，当前窗口仍需出现在下拉框
    svc = _FakeService(windows=[])
    _patch(svc)
    import asyncio
    ov = asyncio.get_event_loop().run_until_complete(build_overview())
    keys = [w["key"] for w in ov["windows"]]
    assert "2026-W33" in keys, "当前窗口必须出现在 windows 列表"
    assert keys[0] == "2026-W33"


def test_dispatch_preview_combined():
    # 合并预览：一次返回歌单名 + 简介 + 歌曲列表
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "preview", "group_id": 123}))
    assert r["ok"]
    assert r["data"]["name"] == "Wk.1歌单"
    assert "晴天" in r["data"]["description"]
    assert len(r["data"]["songs"]) == 2
    assert r["data"]["songs"][0]["title"] == "晴天"
    assert r["data"]["songs"][0]["index"] == 1
    assert r["data"]["window_key"] == "2026-W33"


def test_dispatch_aliases_from_old_frontend():
    # 旧版前端按钮发的简写必须映射到规范操作名
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "pname", "group_id": 123}))
    assert r["ok"] and r["data"]["name"] == "Wk.1歌单"
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "pdesc", "group_id": 123}))
    assert r["ok"] and "晴天" in r["data"]["description"]
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "del", "group_id": 123, "window_key": "2026-W33", "indices": [1]}))
    assert r["ok"] and svc.clear_idx_calls == [(123, "2026-W33", [1])]


def test_dispatch_unknown_and_bad_param():
    svc = _FakeService(); _patch(svc)
    import asyncio
    r = asyncio.get_event_loop().run_until_complete(dispatch_action({"action": "nope"}))
    assert not r["ok"]
    r = asyncio.get_event_loop().run_until_complete(
        dispatch_action({"action": "archive", "group_id": "abc"}))
    assert not r["ok"] and "参数错误" in r["message"]


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print("OK", fn.__name__)

"""WebUI 收集管理 dispatch 动作测试（fake service，不依赖真实库 / 网络）。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.webui import dispatch_action  # noqa: E402


class _FakeService:
    def __init__(self):
        self.calls: list = []

    class _State:
        key = "W1"

    def current_window(self):
        return self._State()

    async def manual_add_song(self, gid, wk, song):
        self.calls.append(("add", gid, wk, dict(song)))
        return {"ok": True, "inserted": True, "song": {"title": song.get("title")}, "message": "已添加"}

    async def edit_song(self, gid, wk, idx, fields):
        self.calls.append(("edit", gid, wk, idx, dict(fields)))
        return {"ok": True, "message": "已保存修改"}

    async def match_song(self, gid, wk, idx, link):
        self.calls.append(("match", gid, wk, idx, link))
        return {"ok": True, "message": "已绑定", "song": {"title": "x", "netease_id": "1", "matched": True}}

    async def reorder_songs(self, gid, wk, ordered):
        self.calls.append(("reorder", gid, wk, list(ordered)))
        return {"ok": True, "count": len(ordered), "message": "已重排"}

    async def sync_playlist(self, gid):
        self.calls.append(("sync", gid))
        return {"ok": True, "added": 1, "removed": 0, "desc_ok": True,
                "playlist_url": "u", "message": "已同步"}


def _patch(svc):
    import music_collector.webui as w
    w.service = svc


async def main() -> None:
    svc = _FakeService()
    _patch(svc)

    r = await dispatch_action({"action": "add_song", "group_id": 123, "song": {"title": "新歌", "song_id": "55"}})
    assert r["ok"] and svc.calls[-1][0] == "add"
    # window_key 缺省时回退到当前窗口
    assert svc.calls[-1][2] == "W1"

    r = await dispatch_action({"action": "edit_song", "group_id": 123, "index": 2, "fields": {"title": "改"}})
    assert r["ok"] and svc.calls[-1][0] == "edit" and svc.calls[-1][3] == 2

    r = await dispatch_action({"action": "match", "group_id": 123, "index": 1,
                               "link": "https://music.163.com/song?id=2692690431"})
    assert r["ok"] and svc.calls[-1][0] == "match" and svc.calls[-1][3] == 1

    r = await dispatch_action({"action": "reorder", "group_id": 123, "ordered_indices": [3, 1, 2]})
    assert r["ok"] and svc.calls[-1][3] == [3, 1, 2]

    r = await dispatch_action({"action": "sync", "group_id": 123})
    assert r["ok"] and r["added"] == 1 and svc.calls[-1] == ("sync", 123)

    print("OK test_webui_collection")


if __name__ == "__main__":
    asyncio.run(main())

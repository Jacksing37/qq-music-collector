"""总库在 WebUI 层的概览与操作路由（假 service 验证分支）。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_webui_master.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.webui import MASTER_KEY, build_overview, dispatch_action  # noqa: E402

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


class _Song:
    def __init__(self, title, artists="群星", sharer_name="李四", netease_id="9"):
        self.title = title
        self.artists = artists
        self.sharer_name = sharer_name
        self.sharer_id = 2
        self.platform = "netease"
        self.netease_id = netease_id
        self.matched = True
        self.url = ""


class _Playlist:
    sharer_aliases = {}


class _State:
    def __init__(self):
        self.key = "W1"
        self.label = "窗口1"
        self.collecting = True


class _Report:
    def __init__(self):
        self.ok = True
        self.added = 2
        self.message = ""
        self.playlist_url = "https://music.163.com/playlist/1"
        self.playlist_id = 1


class _FakeService:
    def __init__(self):
        self.agg_calls = []
        self.archive_calls = []
        self.sync_calls = []
        self.preview_name = "群总库 6001"
        self.preview_desc = "由 QQ 群 6001 跨窗口汇总收集，共 2 首。"
        self._master_songs = [_Song("孤勇者"), _Song("稻香")]
        self._archives = {
            (6001, MASTER_KEY): {"playlist_id": "1", "playlist_url": "https://music.163.com/playlist/1"}
        }

    class _Cfg:
        playlist = _Playlist()
        groups = []
        collect_override = "auto"

    @property
    def config(self):
        return self._Cfg()

    def current_window(self):
        return _State()

    @property
    def netease(self):
        class _N:
            logged_in = True

        return _N()

    class _Store:
        def __init__(self, parent):
            self._p = parent

        async def groups_in_window(self, wk):
            return [6001] if wk == MASTER_KEY else [6001, 6002]

        async def list_songs(self, gid, wk):
            return self._p._master_songs if wk == MASTER_KEY else []

        async def get_archive(self, gid, wk):
            return self._p._archives.get((gid, wk))

    @property
    def store(self):
        return self._Store(self)

    async def target_groups(self, wk):
        return [6001]

    async def windows_with_counts(self, gid=None):
        return [("W1", 2)]

    async def aggregate_to_master(self, gid):
        self.agg_calls.append(gid)
        return 2

    async def run_master_archive(self, gid, name_override=""):
        self.archive_calls.append((gid, name_override))
        return _Report()

    async def sync_master_playlist(self, gid):
        self.sync_calls.append(gid)
        return {
            "ok": True, "added": 2, "removed": 0, "playlist_id": 1,
            "playlist_url": "https://music.163.com/playlist/1", "message": "ok",
        }

    async def preview_master_name(self, gid):
        return self.preview_name

    async def preview_master_description(self, gid):
        return self.preview_desc

    async def run_archive(self, gid, window=None, name_override=""):
        self.archive_calls.append((gid, name_override))
        return _Report()

    async def sync_playlist(self, gid, window=None):
        return {"ok": True, "playlist_id": 2, "message": "ok"}

    async def preview_playlist_name(self, gid):
        return "群歌单"

    async def rebuild_description(self, gid):
        return "desc"


def _patch(svc):
    import music_collector.webui as w

    w.service = svc


async def test_build_overview_master_scope():
    print("\n[A] build_overview(scope=master)")
    svc = _FakeService()
    _patch(svc)
    ov = await build_overview(scope="master")
    check("selected_window 为总库键", ov["selected_window"] == MASTER_KEY, ov["selected_window"])
    check("scope 标记 master", ov["scope"] == "master")
    check("返回一个总库群", len(ov["groups"]) == 1)
    g = ov["groups"][0]
    check("群号正确", g["group_id"] == 6001)
    check("数量为 2", g["count"] == 2)
    check("带总库歌单链接", g["playlist_url"] == "https://music.163.com/playlist/1")
    check("歌曲列表正确", g["songs"][0]["title"] == "孤勇者")
    # 普通窗口视角下不应出现总库
    ov2 = await build_overview()
    keys = [w["key"] for w in ov2["windows"]]
    check("普通视角 windows 不含总库键", MASTER_KEY not in keys, str(keys))


async def test_dispatch_master_aggregate():
    print("\n[B] dispatch master_aggregate 路由")
    svc = _FakeService()
    _patch(svc)
    r = await dispatch_action({"action": "master_aggregate", "group_id": 6001})
    check("成功且调用聚合", r["ok"] and svc.agg_calls == [6001], str(svc.agg_calls))


async def test_dispatch_master_archive_sync_preview():
    print("\n[C] dispatch archive/sync/preview 走总库分支")
    svc = _FakeService()
    _patch(svc)
    r = await dispatch_action({"action": "archive", "group_id": 6001, "window_key": MASTER_KEY})
    check("archive 走总库", r["ok"] and svc.archive_calls == [(6001, "")], str(svc.archive_calls))
    r = await dispatch_action({"action": "sync", "group_id": 6001, "window_key": MASTER_KEY})
    check("sync 走总库", r["ok"] and svc.sync_calls == [6001], str(svc.sync_calls))
    r = await dispatch_action({"action": "preview", "group_id": 6001, "window_key": MASTER_KEY})
    check("preview 走总库", r["ok"] and r["data"]["name"] == "群总库 6001", str(r.get("data")))
    check("preview 歌曲正确", r["data"]["songs"][0]["title"] == "孤勇者")
    check("preview window_key 为总库", r["data"]["window_key"] == MASTER_KEY)


async def test_dispatch_normal_window_routes_to_normal():
    print("\n[D] 普通窗口仍走普通归档")
    svc = _FakeService()
    _patch(svc)
    r = await dispatch_action({"action": "archive", "group_id": 6001, "window_key": "W1"})
    check("普通窗口 archive 走 run_archive", r["ok"] and svc.archive_calls == [(6001, "")])


async def main() -> None:
    await test_build_overview_master_scope()
    await test_dispatch_master_aggregate()
    await test_dispatch_master_archive_sync_preview()
    await test_dispatch_normal_window_routes_to_normal()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

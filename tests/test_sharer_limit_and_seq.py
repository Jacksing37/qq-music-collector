"""三项改动的回归测试。

1. **期号自增**：线上「期号永远不自增、每期都得手动改」的根因是
   ``auto_archive_on_share`` 打开时，本期歌单在**第一次分享**时就被建出来了
   （走 ``auto_archive_songs``），而那条路径原先漏掉了期号消耗；等定时归档再跑时
   记录已存在、``created_new=False``，于是期号永远不动。这里锁死「自动归档建歌单
   同样要消耗期号，复用时不重复消耗」。
2. **无法匹配提示**：非网易云歌搜不到时，要在收录消息**同一条**里附带提示。
3. **同窗口同一用户只收录第一首**：判定基准是「首次分享」（首发那首即使因重复 /
   无法匹配没进榜，名额也算用掉）。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_sharer_limit_and_seq.py
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

from music_collector import build_accept_text, build_sharer_limit_text  # noqa: E402
from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.service import CollectorService, service  # noqa: E402
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
    """够用的网易云桩：``search_hits`` 控制跨平台搜索能否命中。"""

    logged_in = True

    def __init__(self) -> None:
        self.created: list[str] = []
        self.search_hits: list[dict] = []
        self._pid = 8000

    async def search_songs(self, keyword, limit=10):
        return list(self.search_hits)

    async def create_playlist(self, name, privacy=False):
        self._pid += 1
        self.created.append(name)
        return self._pid

    async def add_tracks(self, playlist_id, batch):
        return {"code": 200}

    async def remove_tracks(self, playlist_id, batch):
        return {"code": 200}

    async def update_description(self, playlist_id, desc, name=""):
        return True, "ok"

    def playlist_url(self, playlist_id):
        return f"https://music.163.com/#/playlist?id={playlist_id}"


def _song(sid: str, title: str, platform: str = "netease", sharer_id: int = 1) -> Song:
    return Song(
        platform=platform, song_id=sid, title=title, artists="测试歌手",
        sharer_id=sharer_id, sharer_name="张三",
        netease_id=sid if platform == "netease" else None,
    )


def _seg(*ids: str) -> list[dict]:
    """把若干网易云链接塞进一条消息（detector 能识别成多个 MusicLink）。"""
    text = " ".join(f"https://music.163.com/song?id={i}" for i in ids)
    return [{"type": "text", "data": {"text": text}}]


def _make_svc(tmp: Path):
    store = Store(tmp / "svc.db")
    api = StubNetease()
    svc = CollectorService()
    svc.store = store
    svc.netease = api
    svc.archiver = Archiver(api, store)
    # 关键：配置指到临时目录，绝不碰真实的 data/config.yaml
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.collect_override = "on"  # 强制收集期
    config_manager.config.master.enabled = False
    return svc, store, api


# ------------------------------------------------------- 1. 期号自增


async def test_auto_archive_consumes_seq():
    print("\n[A] 分享即归档新建歌单 → 必须消耗期号（线上不自增的根因）")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7001
    pl = config_manager.config.playlist
    pl.seq = 90
    pl.seq_auto_increment = True
    pl.pending_name = ""
    pl.name_template = "Wk.{seq}"

    state = svc.current_window()
    await store.add_song(gid, state.key, _song("1001", "歌一"))
    all_songs = await store.list_songs(gid, state.key)

    await svc.auto_archive_songs(gid, state, all_songs)
    check("分享即归档建了新歌单", len(api.created) == 1, str(api.created))
    check("歌单名用了当期期号 90", api.created[:1] == ["Wk.90"], str(api.created))
    check("期号自增到 91", config_manager.config.playlist.seq == 91,
          str(config_manager.config.playlist.seq))

    # 同一窗口再来一次：复用已有歌单，不该再动期号
    await svc.auto_archive_songs(gid, state, all_songs)
    check("复用歌单不再重复自增", config_manager.config.playlist.seq == 91,
          str(config_manager.config.playlist.seq))
    check("第二次没有重复建歌单", len(api.created) == 1, str(api.created))


async def test_master_auto_archive_consumes_seq():
    print("\n[B] 总库分享即归档新建歌单 → 同样消耗期号")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7002
    m = config_manager.config.master
    m.enabled = True
    m.seq = 5
    m.seq_auto_increment = True
    m.name_template = "总库{seq}"

    await store.add_song(gid, MASTER_KEY, _song("2001", "总库歌"))
    await svc.auto_archive_master(gid)
    check("总库建了歌单", len(api.created) == 1, str(api.created))
    check("总库期号自增到 6", config_manager.config.master.seq == 6,
          str(config_manager.config.master.seq))


# ------------------------------------------------------- 2. 无法匹配提示


async def test_unmatched_notice():
    print("\n[C] 无法匹配到网易云 → 在收录消息同一条里提示")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7003
    config_manager.config.playlist.notify_unmatched = True
    api.search_hits = []  # 网易云搜不到

    async def _resolve(link):
        return _song(link.song_id, "搜不到的歌", platform="qq")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r = await svc.handle_segments(gid, _seg("111"), 123, "张三")
        check("歌仍然被收录", len(r.accepted) == 1, str(r.accepted))
        check("同时记入 unmatched", len(r.unmatched) == 1, str(r.unmatched))
        check("unmatched 的歌没有 netease_id",
              r.accepted[0].netease_id is None, str(r.accepted[0].netease_id))
    finally:
        svc.providers.resolve = real

    real_store = service.store
    service.store = store
    try:
        song = r.accepted[0]
        hit = await build_accept_text(song, 1, gid, unmatched=True)
        ok = await build_accept_text(song, 1, gid, unmatched=False)
        check("unmatched=True 时消息里带提示", "没搜到" in hit, hit)
        check("提示与「已收录」在同一条消息", "已收录" in hit, hit)
        check("unmatched=False 时不出现提示", "没搜到" not in ok, ok)
    finally:
        service.store = real_store


async def test_matched_song_has_no_notice():
    print("\n[D] 能匹配到的非网易云歌 → 不出现提示")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7004
    config_manager.config.playlist.notify_unmatched = True
    api.search_hits = [{"id": 12345, "name": "能搜到的歌",
                        "ar": [{"name": "测试歌手"}]}]

    async def _resolve(link):
        return _song(link.song_id, "能搜到的歌", platform="qq")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r = await svc.handle_segments(gid, _seg("112"), 123, "张三")
        check("歌被收录", len(r.accepted) == 1, str(r.accepted))
        check("不记 unmatched", len(r.unmatched) == 0, str(r.unmatched))
        check("已回填 netease_id", r.accepted[0].netease_id == "12345",
              str(r.accepted[0].netease_id))
    finally:
        svc.providers.resolve = real


# ------------------------------------------------------- 3. 同用户只收第一首


async def test_one_per_sharer():
    print("\n[E] 同一窗口同一用户只收录第一首")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7005
    config_manager.config.playlist.one_per_sharer = True

    async def _resolve(link):
        return _song(link.song_id, f"歌{link.song_id}")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r1 = await svc.handle_segments(gid, _seg("901"), 123, "张三")
        check("用户首发被收录", len(r1.accepted) == 1, str(r1.accepted))
        check("首发不被拦", len(r1.sharer_limited) == 0, str(r1.sharer_limited))

        r2 = await svc.handle_segments(gid, _seg("902"), 123, "张三")
        check("同一用户第二首不收录", len(r2.accepted) == 0, str(r2.accepted))
        check("第二首记为 sharer_limited", len(r2.sharer_limited) == 1,
              str(r2.sharer_limited))
        check("提示里带的是首发歌名",
              r2.sharer_limited[0][1].get("title") == "歌901",
              str(r2.sharer_limited[0][1]))

        # 同一条消息里贴了两首：只有第一首能过
        r3 = await svc.handle_segments(gid, _seg("903", "904"), 456, "李四")
        check("同批两首只收第一首", len(r3.accepted) == 1, str(r3.accepted))
        check("同批第二首被拦", len(r3.sharer_limited) == 1, str(r3.sharer_limited))

        # 换个人发同一首：正常收录（不是「歌重复」被拦）
        r4 = await svc.handle_segments(gid, _seg("902"), 789, "王五")
        check("换人发同一首歌正常收录", len(r4.accepted) == 1, str(r4.accepted))

        # 判定基准 = 首次分享：首发那首因重复没进榜，名额同样算用掉
        r5 = await svc.handle_segments(gid, _seg("901"), 999, "赵六")
        check("首发就是别人已收录的歌 → 记 duplicated",
              len(r5.duplicated) == 1, str(r5.duplicated))
        r6 = await svc.handle_segments(gid, _seg("905"), 999, "赵六")
        check("首次分享即占位：后续新歌也被拦", len(r6.accepted) == 0,
              str(r6.accepted))
        check("被拦时用的仍是首发歌名",
              r6.sharer_limited[0][1].get("title") == "歌901",
              str(r6.sharer_limited[0][1]))
    finally:
        svc.providers.resolve = real

    # 关掉开关后应恢复原行为
    config_manager.config.playlist.one_per_sharer = False
    svc.providers.resolve = _resolve
    try:
        r7 = await svc.handle_segments(gid, _seg("906"), 123, "张三")
        check("关闭开关后同一用户可继续分享", len(r7.accepted) == 1, str(r7.accepted))
    finally:
        svc.providers.resolve = real


async def test_sharer_limit_text_placeholders():
    print("\n[F] 同用户提示文案可自定义且占位符可用")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7006
    config_manager.config.reply.sharer_limit_text = (
        "{nick}|{title}|{artists}|{platform}|{index}|{count}|{window}"
    )
    await store.add_song(gid, svc.current_window().key, _song("a1", "首发歌", sharer_id=123))

    real_store = service.store
    service.store = store
    try:
        blocked = Song(platform="qq", song_id="b1", title="想发的歌", artists="某人",
                       sharer_id=123, sharer_name="张三")
        first = {"title": "首发歌", "artists": "测试歌手", "platform": "qq"}
        text = await build_sharer_limit_text(blocked, first, gid)
        check("占位符全部被替换", "{" not in text, text)
        check("平台 key 转成中文名", "QQ音乐" in text, text)
        check("带上了首发歌名", "首发歌" in text, text)
        check("带上了分享者", "张三" in text, text)
        check("带上首发序号 1", "|1|" in text, text)
    finally:
        service.store = real_store

    # 未自定义时的默认文案（线上就是这么用的）
    config_manager.config.reply.sharer_limit_text = (
        " 本期你已经分享过《{title}》了，要更换的话请找管理员"
    )
    service.store = store
    try:
        text = await build_sharer_limit_text(
            blocked, {"title": "首发歌", "artists": "", "platform": "qq"}, gid
        )
        check("默认文案渲染正确",
              text == " 本期你已经分享过《首发歌》了，要更换的话请找管理员", text)
    finally:
        service.store = real_store


async def main() -> None:
    await test_auto_archive_consumes_seq()
    await test_master_auto_archive_consumes_seq()
    await test_unmatched_notice()
    await test_matched_song_has_no_notice()
    await test_one_per_sharer()
    await test_sharer_limit_text_placeholders()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

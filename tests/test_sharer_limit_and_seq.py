"""三项改动的回归测试。

1. **期号自增**：线上「期号永远不自增、每期都得手动改」的根因是
   ``auto_archive_on_share`` 打开时，本期歌单在**第一次分享**时就被建出来了
   （走 ``auto_archive_songs``），而那条路径原先漏掉了期号消耗；等定时归档再跑时
   记录已存在、``created_new=False``，于是期号永远不动。这里锁死「自动归档建歌单
   同样要消耗期号，复用时不重复消耗」。
2. **无法匹配提示**：非网易云歌搜不到时，要在收录消息**同一条**里附带提示。
3. **同窗口同一用户只收录第一首**：判定基准是「首次分享」，但若首发那首最终因
   **重复**（同窗口已有 / 总库已有）而没进榜，名额会还给本人（2026-10-10 线上更正：
   否则他既没收到歌、又被判「本期已经分享过」，换歌也被拦）。真正收录了才占住名额。

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

        # 2026-10-10 更正：首发那首若是因**重复**而没进榜（别人先收了 / 总库已有），
        # 名额要还给本人——否则他「什么都没收到，还被判本期已分享过」，换歌也被拦。
        wk = svc.current_window(gid).key
        r5 = await svc.handle_segments(gid, _seg("901"), 999, "赵六")
        check("首发就是别人已收录的歌 → 记 duplicated",
              len(r5.duplicated) == 1, str(r5.duplicated))
        check("没收录 → 名额已归还",
              await store.get_sharer_claim(gid, wk, 999) is None,
              str(await store.get_sharer_claim(gid, wk, 999)))
        r6 = await svc.handle_segments(gid, _seg("905"), 999, "赵六")
        check("重复不占名额：换个新歌能被收录", len(r6.accepted) == 1,
              str(r6.accepted))
        # 真正收录之后，名额才生效
        r6b = await svc.handle_segments(gid, _seg("907"), 999, "赵六")
        check("收录之后才占位：再发新歌被拦", len(r6b.sharer_limited) == 1,
              str(r6b.sharer_limited))
        check("被拦时用的是首发歌名",
              r6b.sharer_limited[0][1].get("title") == "歌905",
              str(r6b.sharer_limited[0][1]))
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


async def test_delete_releases_sharer_claim():
    print("\n[G] 管理员删掉本期的歌 → 该分享者的名额随之释放")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7007
    config_manager.config.playlist.one_per_sharer = True

    async def _resolve(link):
        return _song(link.song_id, f"歌{link.song_id}")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        r1 = await svc.handle_segments(gid, _seg("1101"), 123, "张三")
        check("张三五首发被收录", len(r1.accepted) == 1, str(r1.accepted))
        r2 = await svc.handle_segments(gid, _seg("1201"), 456, "李四")
        check("李四的首发也被收录", len(r2.accepted) == 1, str(r2.accepted))
        r3 = await svc.handle_segments(gid, _seg("1102"), 123, "张三")
        check("张三第二首被拦", len(r3.sharer_limited) == 1, str(r3.sharer_limited))

        wk = svc.current_window().key
        n = await svc.clear_indices(gid, wk, [1])        # 删掉本期第 1 首（张三的）
        check("按序号删除成功", n == 1, str(n))
        check("张三的占位记录已释放",
              await store.get_sharer_claim(gid, wk, 123) is None)
        check("李四的占位不受影响",
              await store.get_sharer_claim(gid, wk, 456) is not None)

        r4 = await svc.handle_segments(gid, _seg("1103"), 123, "张三")
        check("删掉后张三可以再分享", len(r4.accepted) == 1, str(r4.accepted))
        check("再分享不再被拦", len(r4.sharer_limited) == 0, str(r4.sharer_limited))

        # 清空整个窗口同样要释放全部占位
        await svc.clear_window(gid, wk)
        check("清空窗口后占位也没了",
              await store.get_sharer_claim(gid, wk, 123) is None
              and await store.get_sharer_claim(gid, wk, 456) is None)

        # 总库是跨窗口聚合视图：在里面删歌不该误放某个具体窗口的名额
        await store.add_sharer_claim(gid, wk, 123, "歌1105")
        released = await store.release_claims_for(gid, MASTER_KEY, [123])
        check("总库视图删除不释放窗口占位", released == 0, str(released))
        check("那个占位仍然在",
              await store.get_sharer_claim(gid, wk, 123) is not None)
    finally:
        svc.providers.resolve = real


async def test_stale_claim_auto_released():
    print("\n[H] 占位所指的歌已被删掉 → 下次分享自动把名额还回去（幽灵名额）")
    tmp = Path(tempfile.mkdtemp())
    svc, store, api = _make_svc(tmp)
    await store.init()
    gid = 7008
    config_manager.config.playlist.one_per_sharer = True
    config_manager.config.master.enabled = True

    async def _resolve(link):
        return _song(link.song_id, f"歌{link.song_id}")

    real = svc.providers.resolve
    svc.providers.resolve = _resolve
    try:
        wk = svc.current_window().key
        # 复刻线上 543486099 的状态：歌早就在总库里，占位是本窗口分享时留下的，
        # 但本窗口并没有它的行 —— 这时删掉总库那行，歌就哪儿都不在了。
        await store.add_song(gid, MASTER_KEY, _song("3001", "歌3001"))
        await store.add_sharer_claim(
            gid, wk, 123, "歌3001", artists="测试歌手", platform="netease",
            sharer_name="张三", song_id="3001",
        )

        r1 = await svc.handle_segments(gid, _seg("3001"), 123, "张三")
        check("歌还在库里时照旧拦下", len(r1.sharer_limited) == 1, str(r1.sharer_limited))

        # 管理员在网页端「总库」页把这首歌删掉（这条路径本就刻意不释放窗口占位，
        # 占位就成了幽灵名额 —— 线上用户遇到的正是它）
        n = await svc.clear_indices(gid, MASTER_KEY, [1])
        check("总库里的那行已删除", n == 1, str(n))
        check("窗口占位仍留着（这就是幽灵名额的成因）",
              await store.get_sharer_claim(gid, wk, 123) is not None)

        r2 = await svc.handle_segments(gid, _seg("3001"), 123, "张三")
        check("幽灵名额不再拦人", len(r2.sharer_limited) == 0, str(r2.sharer_limited))
        check("同一首被重新收录", len(r2.accepted) == 1, str(r2.accepted))
        claim = await store.get_sharer_claim(gid, wk, 123)
        check("占位已重建成这首新歌", (claim or {}).get("song_id") == "3001", str(claim))
        check("重收的歌也进了总库", await store.find_in_window(
            gid, MASTER_KEY, _song("3001", "歌3001")) is not None)

        # 重建后的占位依旧是「本期一首」：再发第二首还是会被拦
        r3 = await svc.handle_segments(gid, _seg("3002"), 123, "张三")
        check("重收之后名额照样只有一个", len(r3.sharer_limited) == 1, str(r3.sharer_limited))

        # 老占位（升级前登记、没有 song_id）也要能靠歌名判失效
        await store.add_sharer_claim(gid, wk, 456, "早就没了的歌", sharer_name="李四")
        r4 = await svc.handle_segments(gid, _seg("3003"), 456, "李四")
        check("老占位按歌名判失效后放行", len(r4.accepted) == 1, str(r4.accepted))
        check("老占位已释放", len(r4.sharer_limited) == 0, str(r4.sharer_limited))

        # 歌还在窗口里 → 占位有效，必须继续拦（别把正常名额误放了）
        await store.add_sharer_claim(gid, wk, 789, "歌3003", platform="netease",
                                     sharer_name="王五", song_id="3003")
        r5 = await svc.handle_segments(gid, _seg("3003"), 789, "王五")
        check("歌还在窗口里则继续拦", len(r5.sharer_limited) == 1, str(r5.sharer_limited))
    finally:
        svc.providers.resolve = real


async def main() -> None:
    await test_auto_archive_consumes_seq()
    await test_master_auto_archive_consumes_seq()
    await test_unmatched_notice()
    await test_matched_song_has_no_notice()
    await test_one_per_sharer()
    await test_sharer_limit_text_placeholders()
    await test_delete_releases_sharer_claim()
    await test_stale_claim_auto_released()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

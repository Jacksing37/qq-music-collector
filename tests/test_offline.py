"""离线冒烟测试：不依赖 nonebot 运行时和网络（除标注的联网用例）。

运行: python tests/test_offline.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

# 插件包在导入时会 require("nonebot_plugin_apscheduler")，先把 NoneBot 初始化起来。
# 这同时也验证了插件本身能在真实运行时里被加载。
import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import archiver as arch  # noqa: E402
from music_collector.config import RenderConfig, WindowConfig  # noqa: E402
from music_collector.detector import extract_from_card, extract_from_text, match_url  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.netease_api import weapi_encrypt  # noqa: E402
from music_collector.render import build_text_list, render_song_list  # noqa: E402
from music_collector.store import Store  # noqa: E402
from music_collector.window import WindowResolver  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


# ---------------------------------------------------------------- 链接识别


def test_detector() -> None:
    print("\n[1] 链接识别")
    cases = [
        ("https://music.163.com/song?id=1901371647", "netease", "1901371647"),
        ("https://music.163.com/#/song?id=347230", "netease", "347230"),
        ("https://y.music.163.com/m/song?id=5257138&userid=1", "netease", "5257138"),
        ("https://music.163.com/m/song/1901371647", "netease", "1901371647"),
        ("https://y.qq.com/n/ryqq/songDetail/003OUlho2HcRHC", "qq", "003OUlho2HcRHC"),
        ("https://y.qq.com/n/yqq/song/001Qu4I30eVFYb.html", "qq", "001Qu4I30eVFYb"),
        ("https://i.y.qq.com/v8/playsong.html?songmid=003OUlho2HcRHC", "qq", "003OUlho2HcRHC"),
        ("https://www.kuwo.cn/play_detail/158605450", "kuwo", "158605450"),
        ("https://qishui.douyin.com/s/iL9kbQ7M/", "qishui", "iL9kbQ7M"),
        ("https://music.apple.com/cn/song/%E8%B5%B7%E9%A3%8E%E4%BA%86/1751602451", "apple", "1751602451"),
        ("https://music.apple.com/us/album/albums/1751602450?i=1751602451", "apple", "1751602451"),
    ]
    for url, platform, key in cases:
        link = match_url(url)
        ok = link is not None and link.platform == platform and link.key == key
        check(f"{platform:8s} {url[:52]}", ok, "" if ok else f"-> {link}")

    # 纯 Apple Music 专辑链接（无 ?i=）不是单曲，不应误伤
    check("忽略 Apple 纯专辑链接", match_url(
        "https://music.apple.com/cn/album/albums/1751602450") is None)

    # 非音乐链接不应误伤
    check("忽略普通链接", match_url("https://www.example.com/news/123") is None)
    check("忽略 GitHub", match_url("https://github.com/nonebot/nonebot2") is None)

    # 混在文本中
    text = "推荐这首 https://music.163.com/song?id=347230 很好听！还有 https://baidu.com"
    found = extract_from_text(text)
    check("文本中提取", len(found) == 1 and found[0].song_id == "347230", f"-> {found}")

    # 中文标点粘连
    text2 = "听听这个https://music.163.com/song?id=347230。"
    found2 = extract_from_text(text2)
    check("剥离中文句号", len(found2) == 1 and found2[0].song_id == "347230", f"-> {found2}")


def test_card() -> None:
    print("\n[2] 分享卡片解析")
    qq_card = {
        "app": "com.tencent.structmsg",
        "view": "music",
        "meta": {
            "music": {
                "title": "晴天",
                "desc": "周杰伦",
                "preview": "https://y.gtimg.cn/music/photo_new/T002R300x300M000002fRUtu4Z0TAb.jpg",
                "jumpUrl": "https:\\/\\/i.y.qq.com\\/v8\\/playsong.html?songmid=0039MnYb0qxYhV",
                "tag": "QQ音乐",
            }
        },
    }
    links = extract_from_card(qq_card)
    ok = len(links) == 1 and links[0].platform == "qq" and links[0].song_mid == "0039MnYb0qxYhV"
    check("QQ音乐结构化卡片", ok, f"-> {links}")
    check("卡片带出歌名", bool(links) and links[0].hint_title == "晴天")
    check("卡片带出歌手", bool(links) and links[0].hint_artist == "周杰伦")

    netease_card = {
        "app": "com.tencent.miniapp_01",
        "meta": {
            "detail_1": {
                "desc": "网易云音乐",
                "title": "网易云音乐",
                "qqdocurl": "https://163cn.tv/xxxxxx",
                "preview": "http://example.com/a.jpg",
            }
        },
    }
    links2 = extract_from_card(netease_card)
    check("网易云小程序卡片", len(links2) == 1 and links2[0].platform == "netease", f"-> {links2}")

    # 字符串形式的 JSON 也要能吃
    links3 = extract_from_card(json.dumps(qq_card))
    check("字符串形式卡片", len(links3) == 1)

    # 非音乐卡片不应误报
    other = {"app": "com.tencent.structmsg", "meta": {"news": {"jumpUrl": "https://news.qq.com/a/1"}}}
    check("忽略新闻卡片", extract_from_card(other) == [])


# ---------------------------------------------------------------- 时间窗口


def test_window() -> None:
    print("\n[3] 时间窗口")
    tz = ZoneInfo("Asia/Shanghai")

    cfg = WindowConfig(mode="weekly")
    cfg.weekly.start = "MON 00:00"
    cfg.weekly.summary = "SUN 22:00"
    cfg.weekly.archive = "SUN 22:30"
    r = WindowResolver(cfg)

    # 2026-08-05 是周三
    wed = datetime(2026, 8, 5, 12, 0, tzinfo=tz)
    state = r.resolve(wed)
    check("周三处于收集期", state.collecting, state.describe())
    check("窗口起点是本周一", state.start_at == datetime(2026, 8, 3, 0, 0, tzinfo=tz), state.describe())
    check("窗口终点是周日22:30", state.archive_at == datetime(2026, 8, 9, 22, 30, tzinfo=tz), state.describe())

    # 归档时刻整点触发，应仍归属本窗口且已停止收集
    at_archive = datetime(2026, 8, 9, 22, 30, tzinfo=tz)
    s2 = r.resolve(at_archive)
    check("归档时刻窗口 key 不变", s2.key == state.key, f"{s2.key} vs {state.key}")
    check("归档时刻停止收集", not s2.collecting, s2.describe())

    # 归档后到下周一之间是空窗期
    s3 = r.resolve(datetime(2026, 8, 9, 23, 0, tzinfo=tz))
    check("空窗期不收集", not s3.collecting, s3.describe())

    # 下周一重新开始，key 变化
    s4 = r.resolve(datetime(2026, 8, 10, 0, 0, tzinfo=tz))
    check("新窗口开始收集", s4.collecting and s4.key != state.key, s4.describe())

    # 中文星期
    cfg.weekly.start = "周一 20:00"
    r2 = WindowResolver(cfg)
    check("中文星期可解析", r2.weekly_points()[0].dow == 0)

    # daily 模式
    dcfg = WindowConfig(mode="daily")
    dcfg.daily.start = "08:00"
    dcfg.daily.archive = "23:30"
    dr = WindowResolver(dcfg)
    check("每日 12:00 收集中", dr.resolve(datetime(2026, 8, 5, 12, 0, tzinfo=tz)).collecting)
    check("每日 02:00 空窗", not dr.resolve(datetime(2026, 8, 5, 2, 0, tzinfo=tz)).collecting)

    # once 模式
    ocfg = WindowConfig(mode="once")
    ocfg.once.start = "2026-08-10 00:00"
    ocfg.once.archive = "2026-08-20 22:00"
    orr = WindowResolver(ocfg)
    check("单次区间内", orr.resolve(datetime(2026, 8, 15, 0, 0, tzinfo=tz)).collecting)
    check("单次区间前", not orr.resolve(datetime(2026, 8, 1, 0, 0, tzinfo=tz)).collecting)
    check("单次区间后", not orr.resolve(datetime(2026, 8, 21, 0, 0, tzinfo=tz)).collecting)

    # 调度参数
    specs = r.schedule_specs()
    check("生成 3 个调度任务", len(specs) == 3, f"-> {specs}")
    check("cron 参数含星期", specs[0][2].get("day_of_week") == "mon", f"-> {specs[0]}")

    # 非法格式要报错
    bad = WindowConfig(mode="weekly")
    bad.weekly.start = "星期八 25:00"
    try:
        WindowResolver(bad).weekly_points()
        check("非法时间点抛异常", False)
    except Exception:
        check("非法时间点抛异常", True)


# ---------------------------------------------------------------- 匹配算法


def test_match() -> None:
    print("\n[4] 网易云匹配算法")
    song = Song(platform="qq", song_id="1", title="晴天", artists="周杰伦", duration=269)
    good = {"name": "晴天", "ar": [{"name": "周杰伦"}], "dt": 269000}
    cover = {"name": "晴天", "ar": [{"name": "群星"}], "dt": 200000}
    remix = {"name": "晴天 (Live)", "ar": [{"name": "周杰伦"}], "dt": 300000}

    check("严格模式命中原版", arch.is_acceptable(song, good, True))
    check("严格模式拒绝翻唱", not arch.is_acceptable(song, cover, True))
    check("Live 版括号被忽略后可命中", arch.is_acceptable(song, remix, True))
    check("原版分数最高", arch.score_candidate(song, good) > arch.score_candidate(song, remix))

    s2 = Song(platform="kugou", song_id="x", title="Shape of You", artists="Ed Sheeran")
    c2 = {"name": "Shape of You", "ar": [{"name": "Ed Sheeran"}]}
    check("英文歌大小写归一", arch.is_acceptable(s2, c2, True))

    s3 = Song(platform="kuwo", song_id="y", title="七里香", artists="")
    c3 = {"name": "七里香", "ar": [{"name": "周杰伦"}]}
    check("歌手未知时不惩罚", arch.is_acceptable(s3, c3, True))


# ---------------------------------------------------------------- 存储


async def test_store() -> None:
    print("\n[5] SQLite 收集池")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        await store.init()

        s1 = Song(platform="netease", song_id="1", title="A", artists="X", sharer_name="张三")
        s2 = Song(platform="qq", song_id="2", title="B", artists="Y", sharer_name="李四")

        ins1, row1 = await store.add_song(10, "W1", s1)
        ins2, _ = await store.add_song(10, "W1", s2)
        ins3, row3 = await store.add_song(10, "W1", s1)  # 重复

        check("首次插入成功", ins1 and ins2)
        check("重复插入被拦截", not ins3)
        check("重复返回原记录的分享者", row3.sharer_name == "张三", f"-> {row3.sharer_name}")
        check("计数正确", await store.count(10, "W1") == 2)

        songs = await store.list_songs(10, "W1")
        check("按分享顺序返回", [s.title for s in songs] == ["A", "B"], f"-> {[s.title for s in songs]}")
        check("序号定位正确", await store.position_of(10, "W1", row1.row_id) == 1)

        # 不同窗口互不干扰
        await store.add_song(10, "W2", s1)
        check("窗口隔离", await store.count(10, "W2") == 1 and await store.count(10, "W1") == 2)
        check("群列表正确", await store.groups_in_window("W1") == [10])

        await store.mark_matched(row1.row_id, "999")
        again = await store.list_songs(10, "W1")
        check("匹配结果已写回", again[0].netease_id == "999" and again[0].matched)

        removed = await store.remove_song(10, "W1", 1)
        check("按序号删除", removed is not None and removed.title == "A")
        check("删除后计数", await store.count(10, "W1") == 1)

        await store.record_archive(10, "W1", "888", "http://x", 2, 1, 1)
        rec = await store.get_archive(10, "W1")
        check("归档记录落库", rec is not None and rec["playlist_id"] == "888")


# ---------------------------------------------------------------- 渲染


async def test_render() -> None:
    print("\n[6] 榜单渲染")
    songs = [
        Song(platform="netease", song_id=str(i), title=f"测试歌曲名称第{i}首 Very Long Title Here",
             artists="周杰伦、林俊杰", album="叶惠美", sharer_name=f"用户{i}", duration=200 + i)
        for i in range(1, 13)
    ]
    text = build_text_list(songs, "群音乐收藏榜")
    check("文字列表含全部条目", text.count("\n") >= 12, f"lines={text.count(chr(10))}")
    check("文字列表带序号", "1. 测试歌曲名称第1首" in text)
    check("空列表有提示", "还没有收集到" in build_text_list([], "榜单"))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = RenderConfig(show_cover=False, max_items_per_image=8, theme="dark")
        paths = await render_song_list(songs, "群音乐收藏榜", "共 12 首", cfg, Path(tmp))
        check("分页生成 2 张图", len(paths) == 2, f"-> {paths}")
        check("图片文件非空", all(p.exists() and p.stat().st_size > 1000 for p in paths))

        # 生成一张给人看的样例图，留在项目里
        sample_dir = ROOT / "data" / "sample"
        sample_dir.mkdir(parents=True, exist_ok=True)
        light = RenderConfig(show_cover=False, max_items_per_image=40, theme="dark")
        out = await render_song_list(songs, "群音乐收藏榜 · 2026-08-03 ~ 2026-08-09",
                                     "共 12 首 · 每周一 00:00 开始收集", light, sample_dir)
        check("样例图已生成", bool(out) and out[0].exists())
        print(f"       样例图: {out[0]}")


# ---------------------------------------------------------------- 加密


def test_crypto() -> None:
    print("\n[7] weapi 加密")
    enc = weapi_encrypt({"hello": "world"})
    check("产出 params", isinstance(enc.get("params"), str) and len(enc["params"]) > 0)
    check("encSecKey 长度 256", len(enc.get("encSecKey", "")) == 256, f"-> {len(enc.get('encSecKey',''))}")
    enc2 = weapi_encrypt({"hello": "world"})
    check("每次密钥随机", enc["encSecKey"] != enc2["encSecKey"])


# ---------------------------------------------------------------- main


async def main() -> int:
    test_detector()
    test_card()
    test_window()
    test_match()
    await test_store()
    await test_render()
    test_crypto()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

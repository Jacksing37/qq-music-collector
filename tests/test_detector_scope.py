"""识别器边界回归：泛化参数不外溢、集合页不当单曲。

2026-11-13 线上两个现象：
  1) 网易云「分享歌单」卡（``y.music.163.com/m/playlist?id=…``）被域名兜底成一个
     没有 song_id 的 netease link，``fallback_song`` 拿卡片标题当歌名 → 入库；
  2) ``?mid=`` / ``?songid=`` 这类泛化参数原本**对任何域名都生效**（非音乐域名时
     ``scope is None``，所有平台规则都跑一遍），微博 / B 站链接只要带 ``mid=``
     就会被当成 QQ 音乐，再用卡片标题反查出某首歌入库。

本文件钉住：
  [A] 泛化参数只在 QQ 音乐域名下参与匹配；
  [B] 歌单 / 专辑 / 电台 / 榜单 / 歌手页不做域名兜底；
  [C] 单曲页（含 Apple ``album?i=``）识别不受影响；
  [D] ``fallback_song`` 不把「分享歌单: …」这类集合文案当歌名；
  [E] 真实歌单分享卡抽不出任何音乐链接。

运行: ./.venv/Scripts/python.exe tests/test_detector_scope.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.detector import extract_from_card, match_url  # noqa: E402
from music_collector.models import MusicLink  # noqa: E402
from music_collector.providers.base import fallback_song  # noqa: E402

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


def _platform(url: str) -> str | None:
    link = match_url(url)
    return link.platform if link else None


def test_generic_query_only_on_qq() -> None:
    print("\n[A] 泛化参数（mid= / songid=）只在 QQ 域名生效")
    check("微博 mid= 不再识别为音乐",
          _platform("https://weibo.com/1642904381/NkLmNoP?mid=5012345678901234") is None)
    check("B 站 BV + mid= 不识别",
          _platform("https://www.bilibili.com/video/BV1GJ411x7h7?mid=1234567890") is None)
    check("任意站点 mid= 不识别",
          _platform("https://example.com/post?mid=abcdefghij12") is None)
    check("任意站点 songid= 不识别",
          _platform("https://example.com/post?songid=123456") is None)

    link = match_url("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV?mid=0039MnYb0qxYhV")
    check("QQ 域名下仍取到 mid", bool(link) and link.song_mid == "0039MnYb0qxYhV",
          str(link))
    link2 = match_url("https://y.qq.com/n/ryqq/player?songid=1234567")
    check("QQ 域名下仍取到 songid", bool(link2) and link2.song_id == "1234567", str(link2))


def test_collection_pages_ignored() -> None:
    print("\n[B] 歌单 / 专辑 / 电台 / 榜单 / 歌手页不当单曲")
    bad = {
        "网易云移动歌单卡": "https://y.music.163.com/m/playlist?id=9089474436&userid=1",
        "网易云 hash 歌单": "https://music.163.com/#/playlist?id=123456",
        "网易云电台节目": "https://music.163.com/program?id=2499",
        "网易云榜单": "https://music.163.com/discover/toplist?id=3778678",
        "QQ 歌单": "https://y.qq.com/n/ryqq/playlist/8586675416",
        "QQ 专辑": "https://y.qq.com/n/ryqq/albumDetail/002fRO0N4FftzY",
        "QQ 榜单": "https://y.qq.com/n/ryqq/toplist/26",
        "QQ 歌手页": "https://y.qq.com/n/ryqq/singer/001BLpXF2DyJe2",
        "Apple 歌单": "https://music.apple.com/cn/playlist/abc/pl.123",
    }
    for name, url in bad.items():
        check(f"{name} 不产出链接", _platform(url) is None, url)


def test_songs_still_work() -> None:
    print("\n[C] 单曲页识别不受影响")
    cases = {
        "https://music.163.com/song?id=1234": "netease",
        "https://music.163.com/#/song?id=1930772529": "netease",
        "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV": "qq",
        "https://www.kugou.com/mixsong/abc123def.html": "kugou",
        "https://music.apple.com/cn/album/folklore/1526569982?i=1526570000": "apple",
    }
    for url, want in cases.items():
        check(f"{want} 单曲仍识别", _platform(url) == want, url)
    link = match_url("https://music.apple.com/cn/album/folklore/1526569982?i=1526570000")
    check("Apple album?i= 取到 track id", bool(link) and link.song_id == "1526570000", str(link))


def test_fallback_title() -> None:
    print("\n[D] 兜底不用「分享歌单」类文案当歌名")
    for title in ("分享歌单: Wk.？线上学习24/12/27至今", "歌单：青春回忆", "专辑", ""):
        link = MusicLink(platform="netease", raw="https://x", song_id="1", hint_title=title)
        check(f"{title!r} -> 未识别", fallback_song(link).title == "未识别歌曲",
              fallback_song(link).title)
    normal = MusicLink(platform="kugou", raw="https://x", song_id="1", hint_title="孤勇者")
    check("正常歌名保留", fallback_song(normal).title == "孤勇者", fallback_song(normal).title)


def test_real_playlist_card() -> None:
    print("\n[E] 真实「分享歌单」卡片抽不出音乐链接")
    card = {
        "app": "com.tencent.tuwen.lua",
        "meta": {
            "detail_1": {
                "title": "分享歌单: Wk.？线上学习24/12/27至今",
                "desc": "雪見桜",
                "qqdocurl": "https://y.music.163.com/m/playlist?id=9089474436&userid=1",
                "preview": "https://p1.music.126.net/cover.jpg",
            }
        },
    }
    links = extract_from_card(json.dumps(card))
    check("歌单卡不产出链接", links == [], str([(l.platform, l.key) for l in links]))


def main() -> None:
    test_generic_query_only_on_qq()
    test_collection_pages_ignored()
    test_songs_still_work()
    test_fallback_title()
    test_real_playlist_card()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

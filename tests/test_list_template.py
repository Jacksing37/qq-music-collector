"""`/music list` 文字榜单模板回归。

2026-11-13：把榜单文字从硬编码改成「收录回复模板」页里的 list_* 模板。
关键约束是**关着模板时输出必须和改之前一模一样**，所以这里把旧格式逐行钉死。

  [A] 默认（list_enabled=False）与旧内置格式逐行一致
  [B] 打开模板后 标题 / 条目 / 骨架 全部按模板渲染
  [C] 空榜走 list_empty_text
  [D] 超过 list_limit 时补一行 {more}，{left} 数字正确
  [E] 分享者为空时 {nick_line} 整段消失（不能留空括号）
  [F] 模板里写了未知占位符不崩，原样保留

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_list_template.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.config import ReplyConfig  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.render import LIST_DIVIDER, build_text_list  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}")
        if detail:
            print("       " + detail.replace("\n", "\n       "))


def make_songs(n: int, sharer: bool = True) -> list[Song]:
    return [
        Song(
            platform="netease",
            song_id=str(1000 + i),
            title=f"歌{i}",
            artists=f"人{i}",
            sharer_id=(2000 + i) if sharer else 0,
            sharer_name=f"群友{i}" if sharer else "",
            duration=185,
        )
        for i in range(1, n + 1)
    ]


TITLE = "群音乐收藏榜 · 测试窗口"

print("[A] 默认关闭模板 = 旧格式")
songs = make_songs(3)
text = build_text_list(songs, TITLE)
old = "\n".join(
    [TITLE, "共 3 首", "─" * 16,
     "1. 歌1 - 人1（群友1）", "2. 歌2 - 人2（群友2）", "3. 歌3 - 人3（群友3）"]
)
check("逐行等于旧格式", text == old, f"got:\n{text}\n\nwant:\n{old}")
check("分隔线长度 16", LIST_DIVIDER == "─" * 16)
check("list_enabled 默认 False", ReplyConfig().list_enabled is False)

print("\n[B] 打开模板后按模板渲染")
cfg = ReplyConfig(
    list_enabled=True,
    list_title="本周 {count} 首 · {window}",
    list_item="{index}. {title} — {artists}{nick_line}",
    list_template="🎵 {header}\n{divider}\n{songs}{more}",
)
got = build_text_list(songs, TITLE, tmpl=cfg, meta={"window": "周四 12:00"})
want = "\n".join(
    ["🎵 本周 3 首 · 周四 12:00", "─" * 16,
     "1. 歌1 — 人1（群友1）", "2. 歌2 — 人2（群友2）", "3. 歌3 — 人3（群友3）"]
)
check("标题/条目/骨架都生效", got == want, f"got:\n{got}\n\nwant:\n{want}")

print("\n[C] 空榜用 list_empty_text")
cfg_empty = ReplyConfig(list_enabled=True, list_empty_text="本期还没人推歌呢")
check("自定义空榜文案",
      build_text_list([], TITLE, tmpl=cfg_empty) == f"{TITLE}\n本期还没人推歌呢")
check("关闭模板时仍是旧文案",
      build_text_list([], TITLE) == f"{TITLE}\n当前还没有收集到任何歌曲。")

print("\n[D] 超过 list_limit 补 more 行")
cfg_limit = ReplyConfig(
    list_enabled=True,
    list_template="{header}\n共 {count} 首\n{divider}\n{songs}{more}",
    list_limit=2,
)
big = build_text_list(make_songs(5), TITLE, tmpl=cfg_limit, meta={"window": "测试窗口"})
check("只列 2 条 + 还有 3 首",
      big.count(". 歌") == 2 and big.endswith("…… 还有 3 首，详见图片"), big)
check("count 仍是总数 5", "\n共 5 首\n" in big, big)

print("\n[E] 分享者为空时 nick_line 消失")
nobody = build_text_list(make_songs(1, sharer=False), TITLE, tmpl=ReplyConfig(
    list_enabled=True, list_item="{index}. {title} - {artists}{nick_line}"))
check("不留空括号", nobody.strip().endswith("1. 歌1 - 人1"), nobody)

print("\n[F] 未知占位符原样保留")
weird = build_text_list(make_songs(1), TITLE, tmpl=ReplyConfig(
    list_enabled=True, list_template="{header}\n{不存在的占位符}"))
check("不崩且保留原文", "{不存在的占位符}" in weird, weird)

print("\n[G] {playlist} 由 service 塞进 meta，透传到模板")
cfg_pl = ReplyConfig(
    list_enabled=True,
    list_template="{header}\n共 {count} 首\n{playlist}\n{songs}",
)
got_pl = build_text_list(
    make_songs(2), TITLE, tmpl=cfg_pl,
    meta={"window": "测试窗口", "playlist": "https://music.163.com/playlist?id=18590586694"},
)
check("歌单链接出现在榜单里",
      "https://music.163.com/playlist?id=18590586694" in got_pl, got_pl)
check("没传 playlist 时原样保留占位符",
      "{playlist}" in build_text_list(make_songs(1), TITLE, tmpl=cfg_pl))

print(f"\n通过 {PASSED} / {PASSED + FAILED}")
if FAILED:
    sys.exit(1)

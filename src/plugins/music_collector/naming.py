"""歌单命名 / 简介模板引擎。

用一套统一的占位符渲染歌单名和简介，例如：

    name_template: "Wk.{seq}线上学习{yy}/{m}/{d}"
    -> Wk.86线上学习26/8/7

可用占位符
----------
时间类（取窗口结束日；没有结束日就取当前时间）
    {y} 2026   {yy} 26   {m} 8    {mm} 08   {d} 7   {dd} 07
    {ymd} 2026-08-07      {date} 2026-08-07       {slash} 26/8/7
    {week} ISO 周数       {weekday} 周五
    {start} 窗口起始日     {end} 窗口结束日        {window} 窗口区间文案
统计类
    {count} 收录首数      {total} 分享总数        {sharers} 参与人数
其它
    {group} 群号          {seq} 自增期号（每归档一次 +1）
    {songlist} 歌曲清单（仅简介可用）
    {sharerlist} 按人聚合的分享清单（仅简介可用）
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Sequence

from .models import Song
from .textutil import sanitize, sanitize_name

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 允许用户写 {不认识的东西} 而不炸掉
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def build_context(
    *,
    group_id: int,
    window_label: str,
    start_at: Optional[datetime],
    end_at: Optional[datetime],
    count: int,
    total: int,
    seq: int,
    songs: Sequence[Song] = (),
    emoji_style: str = "text",
) -> dict[str, str]:
    """组装占位符表。"""
    ref = end_at or start_at or datetime.now()
    sharer_names = []
    for song in songs:
        name = sharer_of(song, emoji_style)
        if name and name not in sharer_names:
            sharer_names.append(name)

    return {
        "y": f"{ref.year}",
        "yy": f"{ref.year % 100:02d}",
        "m": f"{ref.month}",
        "mm": f"{ref.month:02d}",
        "d": f"{ref.day}",
        "dd": f"{ref.day:02d}",
        "ymd": f"{ref:%Y-%m-%d}",
        "date": f"{ref:%Y-%m-%d}",
        "slash": f"{ref.year % 100:02d}/{ref.month}/{ref.day}",
        "dot": f"{ref.year % 100:02d}.{ref.month}.{ref.day}",
        "time": f"{ref:%H:%M}",
        "week": f"{ref.isocalendar().week}",
        "weekday": _WEEKDAY_CN[ref.weekday()],
        "start": f"{start_at:%Y-%m-%d}" if start_at else "",
        "end": f"{end_at:%Y-%m-%d}" if end_at else "",
        "window": window_label,
        "group": str(group_id),
        "count": str(count),
        "total": str(total),
        "sharers": str(len(sharer_names)),
        "seq": str(seq),
    }


def render_template(template: str, context: dict[str, str]) -> str:
    """按占位符表渲染。未知占位符原样保留，不抛异常。"""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(repl, template or "")


def unknown_placeholders(template: str, context: dict[str, str]) -> list[str]:
    return [
        m.group(1)
        for m in _PLACEHOLDER_RE.finditer(template or "")
        if m.group(1) not in context
    ]


# ---------------------------------------------------------------- 清单文案


def sharer_of(song: Song, emoji_style: str = "text") -> str:
    """取分享者展示名，顺手把昵称里的表情转成文字。

    昵称里的 emoji 是 4 字节字符，网易云简介写不进去，必须先转掉。
    """
    raw = song.sharer_name or ""
    fallback = str(song.sharer_id) if song.sharer_id else "匿名"
    return sanitize_name(raw, emoji_style, fallback)


def build_song_lines(
    songs: Sequence[Song],
    with_platform: bool = False,
    emoji_style: str = "text",
    show_artist: bool = True,
) -> list[str]:
    """「1. 张三 分享《歌名》- 歌手」逐首一行的清单。"""
    lines: list[str] = []
    for idx, song in enumerate(songs, start=1):
        sharer = sharer_of(song, emoji_style)
        title = sanitize(song.title, emoji_style) or song.title
        line = f"{idx}. {sharer} 分享《{title}》"
        if show_artist:
            artist = sanitize(song.artists, emoji_style) or "未知歌手"
            line += f" - {artist}"
        if with_platform:
            line += f"（{song.platform_name}）"
        lines.append(line)
    return lines


def build_sharer_lines(
    songs: Sequence[Song],
    emoji_style: str = "text",
    show_artist: bool = True,
    blank_line: bool = False,
) -> list[str]:
    """按人聚合，但**每首歌独占一行**，看起来整齐：

    ::

        张三（2首）
          · 晴天 - 周杰伦
          · 稻香 - 周杰伦
    """
    grouped: dict[str, list[Song]] = {}
    for song in songs:
        grouped.setdefault(sharer_of(song, emoji_style), []).append(song)

    lines: list[str] = []
    for i, (name, items) in enumerate(grouped.items()):
        if blank_line and i:
            lines.append("")
        lines.append(f"{name}（{len(items)}首）")
        for song in items:
            title = sanitize(song.title, emoji_style) or song.title
            line = f"  · {title}"
            if show_artist:
                artist = sanitize(song.artists, emoji_style) or "未知歌手"
                line += f" - {artist}"
            lines.append(line)
    return lines


def build_name_lines(
    songs: Sequence[Song],
    emoji_style: str = "text",
) -> list[str]:
    """极简清单：每行只列分享者名字，按歌曲顺序编号。

    ::

        1.张三
        2.李四

    用于只关心「谁分享了」而不需要歌名/歌手的场景。
    """
    return [f"{idx}.{sharer_of(song, emoji_style)}" for idx, song in enumerate(songs, start=1)]


def fit_description(
    header: str,
    body_lines: Sequence[str],
    limit: int = 990,
    blank_line_after_header: bool = True,
) -> str:
    """拼装简介：每条独占一行；网易云上限 1000 字，超了截断并注明省略数。"""
    text = (header or "").rstrip()
    if not body_lines:
        return text[:limit]

    parts: list[str] = []
    used = 0
    if text:
        parts.append(text)
        used += len(text) + 1
        if blank_line_after_header:
            parts.append("")
            used += 1

    written = 0
    # 空行不计入「首数」，统计时要排除
    countable = sum(1 for line in body_lines if line.strip())
    for line in body_lines:
        need = len(line) + 1
        if used + need > limit - 24:  # 预留省略提示的空间
            break
        parts.append(line)
        used += need
        if line.strip():
            written += 1
    remain = countable - written
    if remain > 0:
        parts.append(f"…… 还有 {remain} 条未列出")
    return "\n".join(parts)[:limit]

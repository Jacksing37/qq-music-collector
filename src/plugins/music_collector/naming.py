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
    aliases: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """组装占位符表。"""
    ref = end_at or start_at or datetime.now()
    sharer_names = []
    for song in songs:
        name = sharer_of(song, emoji_style, aliases)
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


def resolve_alias(name: str, aliases: Optional[dict[str, str]] = None) -> str:
    """显示层昵称映射：命中映射表返回映射名，否则原样返回。

    兼容昵称里带表情符号的情况：先按原样匹配，再去掉表情后匹配一次，
    这样 ``菜老名`` 这种 key 也能命中 QQ 里实际带 emoji 的昵称。
    """
    if not aliases or not name:
        return name
    if name in aliases:
        return aliases[name]
    plain = sanitize_name(name, "strip")
    return aliases.get(plain, name)


def sharer_of(song: Song, emoji_style: str = "text", aliases: Optional[dict[str, str]] = None) -> str:
    """取分享者展示名，先套用昵称映射，再把昵称里的表情转成文字。

    先映射再清洗的原因：QQ 昵称可能带 emoji（如 ``菜老名🎵``），映射 key 是
    ``菜老名`` 这种纯文本。若先转成 ``菜老名[音符]`` 就匹配不上了，所以先在
    原始昵称上做映射，映射命中后再过一遍 emoji 清洗。
    ``aliases`` 为 ``{原昵称: 显示名}``，仅影响展示，不改变入库数据。
    """
    raw = song.sharer_name or ""
    fallback = str(song.sharer_id) if song.sharer_id else "匿名"
    mapped = resolve_alias(raw, aliases)
    return sanitize_name(mapped, emoji_style, fallback)


def build_song_lines(
    songs: Sequence[Song],
    with_platform: bool = False,
    emoji_style: str = "text",
    show_artist: bool = True,
    aliases: Optional[dict[str, str]] = None,
) -> list[str]:
    """「1. 张三 分享《歌名》- 歌手」逐首一行的清单。"""
    lines: list[str] = []
    for idx, song in enumerate(songs, start=1):
        sharer = sharer_of(song, emoji_style, aliases)
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
    aliases: Optional[dict[str, str]] = None,
) -> list[str]:
    """按人聚合，但**每首歌独占一行**，看起来整齐：

    ::

        张三（2首）
          · 晴天 - 周杰伦
          · 稻香 - 周杰伦
    """
    grouped: dict[str, list[Song]] = {}
    for song in songs:
        grouped.setdefault(sharer_of(song, emoji_style, aliases), []).append(song)

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
    aliases: Optional[dict[str, str]] = None,
) -> list[str]:
    """极简清单：每行只列分享者名字，按歌曲顺序编号。

    ::

        1.张三
        2.李四

    用于只关心「谁分享了」而不需要歌名/歌手的场景。
    """
    return [f"{idx}.{sharer_of(song, emoji_style, aliases)}" for idx, song in enumerate(songs, start=1)]


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

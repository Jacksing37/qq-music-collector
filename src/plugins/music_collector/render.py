"""榜单渲染：文字列表 + Pillow 长图。"""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import CACHE_DIR, RenderConfig
from .models import Song
from .naming import resolve_alias

# ---------------------------------------------------------------- 字体

_REGULAR_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    # Debian / Ubuntu：fonts-noto-cjk 在不同版本里落盘路径不一致，全列上
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]
_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

# 泰文字体候选（Linux 上通常由 fonts-noto-sans-thai / google-noto-sans-thai-fonts 提供）
_THAI_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai.ttf",
    "/usr/share/fonts/truetype/thai/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/google-noto-cjk/NotoSansThai-Regular.ttf",
]

# 系统字体目录（兜底扫描用）
_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/X11/fonts"]
# 兜底扫描时认可的文件/目录名关键字（小写匹配）。
# 同时匹配「文件名」和「所在目录名」——dnf 系字体常落在 google-noto-cjk 目录里。
_CJK_HINTS = ("cjk", "wqy", "droidsansfallback", "sourcehansans", "notosanssc", "noto-sans-cjk", "han")
_THAI_HINTS = ("thai", "notosansthai")


@lru_cache(maxsize=4)
def _scan_font(prefer_bold: bool, hints: tuple[str, ...]) -> Optional[str]:
    """候选路径全落空时，遍历系统字体目录找一个匹配关键字的字体。

    容器/发行版之间字体的落盘路径差异很大，硬编码列表容易漏，
    扫一遍比让用户看到一屏方块字强。``hints`` 为文件名/父目录名要包含的小写关键字。
    """
    found: list[str] = []
    for base in _FONT_DIRS:
        root = Path(base)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in (".ttc", ".otf", ".ttf"):
                continue
            haystack = path.name.lower() + "/" + path.parent.name.lower()
            if any(hint in haystack for hint in hints):
                found.append(str(path))
    if not found:
        return None
    if prefer_bold:
        for path in found:
            if "bold" in Path(path).name.lower():
                return path
    for path in found:
        if "bold" not in Path(path).name.lower():
            return path
    return found[0]


def _scan_cjk_font(prefer_bold: bool) -> Optional[str]:
    return _scan_font(prefer_bold, _CJK_HINTS)


def _scan_thai_font() -> Optional[str]:
    return _scan_font(False, _THAI_HINTS)


def _find_font(candidates: Sequence[str], override: Optional[str] = None) -> Optional[str]:
    if override and Path(override).exists():
        return override
    for path in candidates:
        if Path(path).exists():
            return path
    return _scan_cjk_font(candidates is _BOLD_CANDIDATES)


def _find_thai_font(override: Optional[str] = None) -> Optional[str]:
    if override and Path(override).exists():
        return override
    for path in _THAI_CANDIDATES:
        if Path(path).exists():
            return path
    return _scan_thai_font()


def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- 多脚本回退
# Pillow 的 draw.text 不会跨字体自动回退：主字体（CJK）不含泰文等字形时会画成方块。
# 这里按字符码点把「泰文」等主字体缺失的脚本路由到对应回退字体，逐段绘制。

# (字体, 回退字体) 二元组，回退字体为 None 时全走主字体（无泰文字体时等价旧行为）。
FontPair = tuple[ImageFont.FreeTypeFont, Optional[ImageFont.FreeTypeFont]]


def _script_font(ch: str, thai: Optional[ImageFont.FreeTypeFont],
                 primary: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    o = ord(ch)
    # 泰文 Unicode 区块 U+0E00–U+0E7F
    if 0x0E00 <= o <= 0x0E7F and thai is not None:
        return thai
    return primary


def _split_runs(text: str, pair: FontPair):
    """把文本按「用哪个字体画」切成连续段，返回 [(font, run), ...]。"""
    primary, thai = pair
    runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
    cur: Optional[ImageFont.FreeTypeFont] = None
    buf = ""
    for ch in text:
        f = _script_font(ch, thai, primary)
        if f is cur:
            buf += ch
        else:
            if buf:
                runs.append((cur, buf))
            cur = f
            buf = ch
    if buf:
        runs.append((cur, buf))
    return runs


def _text_width(draw: ImageDraw.ImageDraw, text: str, pair: FontPair) -> float:
    return sum(draw.textlength(run, font=f) for f, run in _split_runs(text, pair))


def _draw_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
               pair: FontPair, fill) -> float:
    """带脚本回退的绘制，返回绘制结束的 x 坐标。"""
    x, y = xy
    for f, run in _split_runs(text, pair):
        draw.text((x, y), run, font=f, fill=fill)
        x += draw.textlength(run, font=f)
    return x


def _ellipsize_fb(draw: ImageDraw.ImageDraw, text: str, pair: FontPair,
                  max_w: int) -> str:
    if _text_width(draw, text, pair) <= max_w:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_width(draw, text[:mid] + ellipsis, pair) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


# ---------------------------------------------------------------- 主题

@dataclass(frozen=True)
class Theme:
    bg: str
    card: str
    text: str
    subtext: str
    accent: str
    divider: str
    placeholder: str


THEMES = {
    "dark": Theme(
        bg="#17181c", card="#212329", text="#f2f3f5", subtext="#9a9da6",
        accent="#5dcaa5", divider="#2c2f36", placeholder="#31343c",
    ),
    "light": Theme(
        bg="#f4f4f6", card="#ffffff", text="#1c1d21", subtext="#71747d",
        accent="#1d9e75", divider="#e6e7ea", placeholder="#dcdde1",
    ),
}

# ---------------------------------------------------------------- 布局常量

WIDTH = 920
PADDING = 32
HEADER_H = 132
ROW_H = 92
COVER = 64
FOOTER_H = 56


# ---------------------------------------------------------------- 文字列表


def build_text_list(
    songs: Sequence[Song], title: str, limit: int = 60,
    aliases: Optional[dict[str, str]] = None,
) -> str:
    """生成纯文字榜单。超过 limit 条只列前 limit 条，避免消息被风控截断。

    ``aliases`` 为分享者昵称映射，命中后展示映射名（仅展示层，不改数据）。
    """
    if not songs:
        return f"{title}\n当前还没有收集到任何歌曲。"
    lines = [title, f"共 {len(songs)} 首", "─" * 16]
    for idx, song in enumerate(songs[:limit], start=1):
        artist = song.artists or "未知歌手"
        raw = song.sharer_name or str(song.sharer_id or "")
        sharer = resolve_alias(raw, song.sharer_id, aliases)
        line = f"{idx}. {song.title} - {artist}"
        if sharer:
            line += f"（{sharer}）"
        lines.append(line)
    if len(songs) > limit:
        lines.append(f"…… 还有 {len(songs) - limit} 首，详见图片")
    return "\n".join(lines)


# ---------------------------------------------------------------- 封面缓存


def _cache_path(url: str) -> Path:
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.img"


async def _download_cover(url: str, client: httpx.AsyncClient) -> Optional[bytes]:
    if not url or not url.startswith("http"):
        return None
    path = _cache_path(url)
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
    except Exception:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


async def fetch_covers(songs: Sequence[Song]) -> dict[str, bytes]:
    """并发拉取封面，失败的直接跳过。"""
    urls = {s.cover for s in songs if s.cover}
    if not urls:
        return {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"}
    results: dict[str, bytes] = {}
    limiter = asyncio.Semaphore(8)

    async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
        async def worker(url: str) -> None:
            async with limiter:
                data = await _download_cover(url, client)
                if data:
                    results[url] = data

        await asyncio.gather(*(worker(u) for u in urls), return_exceptions=True)
    return results


# ---------------------------------------------------------------- 绘制


def _rounded_cover(data: bytes, size: int, radius: int = 10) -> Optional[Image.Image]:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _render_page(
    songs: Sequence[Song],
    covers: dict[str, bytes],
    title: str,
    subtitle: str,
    theme: Theme,
    cfg: RenderConfig,
    page_info: str,
    start_index: int,
    aliases: Optional[dict[str, str]] = None,
) -> Image.Image:
    regular = _find_font(_REGULAR_CANDIDATES, cfg.font_path)
    bold = _find_font(_BOLD_CANDIDATES, cfg.font_path)
    thai_path = _find_thai_font(cfg.thai_font_path)

    def _tf(size: int) -> Optional[ImageFont.FreeTypeFont]:
        return _load_font(thai_path, size) if thai_path else None

    # (主字体, 泰文回退字体) 二元组；无泰文字体时回退为 None（等价旧行为）
    f_title = (_load_font(bold, 34), _tf(34))
    f_sub = (_load_font(regular, 17), _tf(17))
    f_name = (_load_font(bold, 20), _tf(20))
    f_meta = (_load_font(regular, 15), _tf(15))
    f_index = (_load_font(bold, 19), _tf(19))
    f_small = (_load_font(regular, 14), _tf(14))

    height = HEADER_H + ROW_H * len(songs) + FOOTER_H
    img = Image.new("RGB", (WIDTH, height), theme.bg)
    draw = ImageDraw.Draw(img)

    # 头部
    _draw_text(draw, (PADDING, 34), title, f_title, theme.text)
    _draw_text(draw, (PADDING, 84), subtitle, f_sub, theme.subtext)
    if page_info:
        w = _text_width(draw, page_info, f_sub)
        _draw_text(draw, (WIDTH - PADDING - w, 88), page_info, f_sub, theme.subtext)
    draw.line([(PADDING, HEADER_H - 14), (WIDTH - PADDING, HEADER_H - 14)], fill=theme.divider, width=1)

    # 条目
    for i, song in enumerate(songs):
        top = HEADER_H + i * ROW_H
        draw.rounded_rectangle(
            (PADDING, top, WIDTH - PADDING, top + ROW_H - 12), radius=12, fill=theme.card
        )

        num = str(start_index + i)
        num_w = _text_width(draw, num, f_index)
        _draw_text(draw, (PADDING + 40 - num_w, top + 32), num, f_index, theme.accent)

        cover_x = PADDING + 56
        cover_y = top + 8
        if cfg.show_cover:
            data = covers.get(song.cover) if song.cover else None
            pic = _rounded_cover(data, COVER) if data else None
            if pic is not None:
                img.paste(pic, (cover_x, cover_y), pic)
            else:
                draw.rounded_rectangle(
                    (cover_x, cover_y, cover_x + COVER, cover_y + COVER),
                    radius=10, fill=theme.placeholder,
                )
            text_x = cover_x + COVER + 18
        else:
            text_x = cover_x

        right_limit = WIDTH - PADDING - 190
        name = _ellipsize_fb(draw, song.title or "未识别歌曲", f_name, right_limit - text_x)
        _draw_text(draw, (text_x, top + 16), name, f_name, theme.text)

        meta_parts = [song.artists or "未知歌手"]
        if song.album:
            meta_parts.append(song.album)
        meta = _ellipsize_fb(draw, " · ".join(meta_parts), f_meta, right_limit - text_x)
        _draw_text(draw, (text_x, top + 46), meta, f_meta, theme.subtext)

        # 右侧信息
        tag = song.platform_name
        tag_w = _text_width(draw, tag, f_small)
        draw.rounded_rectangle(
            (WIDTH - PADDING - 24 - tag_w - 16, top + 16,
             WIDTH - PADDING - 24, top + 40),
            radius=6, outline=theme.accent, width=1,
        )
        _draw_text(draw, (WIDTH - PADDING - 32 - tag_w, top + 21), tag, f_small, theme.accent)

        sharer = resolve_alias(song.sharer_name or "", song.sharer_id, aliases)
        if sharer:
            sharer_text = _ellipsize_fb(draw, f"by {sharer}", f_small, 168)
            sw = _text_width(draw, sharer_text, f_small)
            _draw_text(draw, (WIDTH - PADDING - 24 - sw, top + 48), sharer_text,
                       f_small, theme.subtext)

    footer = f"生成于 {datetime.now():%Y-%m-%d %H:%M}"
    _draw_text(draw, (PADDING, height - FOOTER_H + 18), footer, f_small, theme.subtext)
    return img


def _render_sync(
    songs: Sequence[Song],
    covers: dict[str, bytes],
    title: str,
    subtitle: str,
    cfg: RenderConfig,
    out_dir: Path,
    aliases: Optional[dict[str, str]] = None,
) -> list[Path]:
    theme = THEMES.get(cfg.theme, THEMES["dark"])
    per_page = max(5, cfg.max_items_per_image)
    pages = [songs[i:i + per_page] for i in range(0, len(songs), per_page)] or [[]]
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for idx, chunk in enumerate(pages):
        page_info = f"{idx + 1}/{len(pages)}" if len(pages) > 1 else ""
        image = _render_page(
            chunk, covers, title, subtitle, theme, cfg, page_info,
            idx * per_page + 1, aliases,
        )
        path = out_dir / f"list_{stamp}_{idx + 1}.png"
        image.save(path, format="PNG", optimize=True)
        paths.append(path)
    return paths


async def render_song_list(
    songs: Sequence[Song],
    title: str,
    subtitle: str,
    cfg: RenderConfig,
    out_dir: Optional[Path] = None,
    aliases: Optional[dict[str, str]] = None,
) -> list[Path]:
    """渲染榜单长图，返回图片路径列表（可能分页）。``aliases`` 用于映射分享者名。"""
    out_dir = out_dir or (CACHE_DIR / "render")
    covers = await fetch_covers(songs) if cfg.show_cover else {}
    return await asyncio.to_thread(
        _render_sync, list(songs), covers, title, subtitle, cfg, out_dir, aliases
    )

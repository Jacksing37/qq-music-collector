"""验证 textutil 的表情清洗：满足网易云简介 utf8(3字节) 存储限制。

关键结论：
- text  模式把认识 emoji 转成中文词（如 🎵→[音符]），不认识的 4 字节字符丢弃
- strip 模式直接删掉表情
- keep  模式原样返回（带 emoji 的昵称简介大概率写不进网易云）
无论哪种「安全」模式（text/strip），输出都不得再含 4 字节字符。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")  # 触发插件加载（__init__ 会 require apscheduler）

from music_collector.textutil import (  # noqa: E402
    EMOJI_STYLES,
    has_wide_char,
    sanitize,
    sanitize_name,
)


def test_text_style_converts_known_emoji_to_brackets() -> None:
    out = sanitize("🌸小仙女🌸", "text")
    assert "🌸" not in out, "text 模式应去掉原始 emoji"
    assert "[樱花]" in out
    assert "小仙女" in out
    assert not has_wide_char(out)


def test_strip_style_removes_emoji() -> None:
    out = sanitize("🌸小仙女🌸", "strip")
    assert "🌸" not in out
    assert out == "小仙女"
    assert not has_wide_char(out)


def test_keep_style_preserves_emoji() -> None:
    out = sanitize("🌸小仙女🌸", "keep")
    assert "🌸" in out
    assert out == "🌸小仙女🌸"


def test_unknown_emoji_yields_no_wide_char() -> None:
    """表外 4 字节字符（无论是否装 emoji 库）处理后不得残留宽字符。"""
    out = sanitize("\U0001FAC2星", "text")
    assert not has_wide_char(out)
    assert "星" in out


def test_zwj_sequence_collapses() -> None:
    out = sanitize("👨‍👩‍👧", "text")
    assert "\u200d" not in out
    assert not has_wide_char(out)


def test_flag_emoji_collapses() -> None:
    out = sanitize("🇨🇳中国", "text")
    assert "🇨🇳" not in out
    assert "中国" in out


def test_sanitize_name_uses_fallback_when_empty() -> None:
    assert sanitize_name("", "text", "10001") == "10001"
    assert sanitize_name("", "strip", "10001") == "10001"


def test_sanitize_name_cleans_emoji() -> None:
    out = sanitize_name("🌟星", "text", "10001")
    assert "🌟" not in out
    assert "星" in out
    assert not has_wide_char(out)


def test_emoji_styles_constant() -> None:
    assert EMOJI_STYLES == ("text", "strip", "keep")


if __name__ == "__main__":
    test_text_style_converts_known_emoji_to_brackets()
    test_strip_style_removes_emoji()
    test_keep_style_preserves_emoji()
    test_unknown_emoji_yields_no_wide_char()
    test_zwj_sequence_collapses()
    test_flag_emoji_collapses()
    test_sanitize_name_uses_fallback_when_empty()
    test_sanitize_name_cleans_emoji()
    test_emoji_styles_constant()
    print("textutil tests OK")

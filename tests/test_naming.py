"""验证命名清单渲染：一首歌占一行 + emoji 清洗参数贯通 + 空白行分段。

对应需求：
1. 简介中一首歌换一行（build_song_lines 逐首一行 / build_sharer_lines 每首歌独占一行）
2. 带表情的昵称需转成文字才能写入（emoji_style 贯通到清单渲染）
3. by_person 样式下条目间可插空行（blank_line）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot

nonebot.init(driver="~fastapi")

from music_collector.models import Song  # noqa: E402
from music_collector.naming import (  # noqa: E402
    build_name_lines,
    build_sharer_lines,
    build_song_lines,
    fit_description,
    resolve_alias,
    sharer_of,
)


def _song(title: str, artist: str, sharer: str, sid: int) -> Song:
    return Song(
        platform="netease",
        song_id=str(sid),
        title=title,
        artists=artist,
        sharer_id=1000 + sid,
        sharer_name=sharer,
        netease_id=str(sid),
        matched=True,
    )


def test_build_song_lines_one_per_line() -> None:
    songs = [
        _song("孤勇者", "陈奕迅", "张三", 1),
        _song("晴天", "周杰伦", "李四", 2),
    ]
    lines = build_song_lines(songs, emoji_style="text", show_artist=True)
    assert len(lines) == 2
    assert lines[0] == "1. 张三 分享《孤勇者》 - 陈奕迅"
    assert lines[1] == "2. 李四 分享《晴天》 - 周杰伦"


def test_build_song_lines_hide_artist() -> None:
    songs = [_song("孤勇者", "陈奕迅", "张三", 1)]
    lines = build_song_lines(songs, emoji_style="text", show_artist=False)
    assert lines[0] == "1. 张三 分享《孤勇者》"


def test_build_song_lines_emoji_cleaned() -> None:
    songs = [_song("歌A", "歌手A", "🌟星", 1)]
    lines = build_song_lines(songs, emoji_style="text", show_artist=False)
    assert "🌟" not in lines[0], "emoji 应被转成文字"
    assert "星" in lines[0]


def test_build_name_lines_only_sharer() -> None:
    songs = [
        _song("孤勇者", "陈奕迅", "Jacksing", 1),
        _song("晴天", "周杰伦", "自巾", 2),
    ]
    lines = build_name_lines(songs, emoji_style="text")
    assert lines == ["1.Jacksing", "2.自巾"]
    # 只列分享者名字，不应出现歌名/歌手
    assert "孤勇者" not in "\n".join(lines)
    assert "周杰伦" not in "\n".join(lines)


def test_build_name_lines_emoji_cleaned() -> None:
    songs = [_song("歌A", "歌手A", "🌟星", 1)]
    lines = build_name_lines(songs, emoji_style="text")
    assert "🌟" not in lines[0], "emoji 应被转成文字"
    assert "星" in lines[0]


def test_build_sharer_lines_one_song_per_line() -> None:
    songs = [
        _song("孤勇者", "陈奕迅", "张三", 1),
        _song("晴天", "周杰伦", "张三", 2),
    ]
    lines = build_sharer_lines(songs, emoji_style="text", show_artist=True, blank_line=False)
    assert lines[0] == "张三（2首）"
    assert "· 孤勇者 - 陈奕迅" in lines[1]
    assert "· 晴天 - 周杰伦" in lines[2]
    assert len(lines) == 3


def test_build_sharer_lines_blank_between_people() -> None:
    songs = [
        _song("A", "a", "张三", 1),
        _song("B", "b", "李四", 2),
    ]
    lines = build_sharer_lines(songs, emoji_style="text", show_artist=True, blank_line=True)
    # 张三（1首） / · A - a / （空行）/ 李四（1首） / · B - b
    assert lines[0] == "张三（1首）"
    assert "" in lines
    assert lines.index("") == 2
    assert lines[3] == "李四（1首）"


def test_build_sharer_lines_no_blank_when_disabled() -> None:
    songs = [
        _song("A", "a", "张三", 1),
        _song("B", "b", "李四", 2),
    ]
    lines = build_sharer_lines(songs, emoji_style="text", show_artist=True, blank_line=False)
    assert "" not in lines


def test_fit_description_blank_after_header() -> None:
    out = fit_description("头部说明", ["a", "b"], blank_line_after_header=True)
    assert out == "头部说明\n\na\nb"


def test_fit_description_no_blank_when_disabled() -> None:
    out = fit_description("头部说明", ["a", "b"], blank_line_after_header=False)
    assert out == "头部说明\na\nb"


# ----------------------------------------------------------------- 昵称映射

def test_resolve_alias_basic() -> None:
    aliases = {"菜老名": "Jacksing"}
    assert resolve_alias("菜老名", aliases) == "Jacksing"
    assert resolve_alias("别人", aliases) == "别人"
    assert resolve_alias("菜老名", None) == "菜老名"


def test_resolve_alias_emoji_nickname() -> None:
    # QQ 昵称带 emoji 时，映射 key 用纯文本也能命中
    aliases = {"菜老名": "Jacksing"}
    assert resolve_alias("菜老名🎵", aliases) == "Jacksing"
    assert resolve_alias("菜老名[音符]", aliases) == "菜老名[音符]"  # 已是文本则不再强匹配


def test_sharer_of_applies_alias() -> None:
    aliases = {"菜老名": "Jacksing"}
    s = _song("晴天", "周杰伦", "菜老名", 1)
    assert sharer_of(s, "text", aliases) == "Jacksing"
    # emoji 昵称同样命中
    s2 = _song("晴天", "周杰伦", "菜老名🎵", 1)
    assert sharer_of(s2, "text", aliases) == "Jacksing"


def test_build_song_lines_applies_alias() -> None:
    aliases = {"菜老名": "Jacksing"}
    songs = [_song("晴天", "周杰伦", "菜老名", 1)]
    lines = build_song_lines(songs, emoji_style="text", show_artist=True, aliases=aliases)
    assert lines[0] == "1. Jacksing 分享《晴天》 - 周杰伦"


def test_build_name_lines_applies_alias() -> None:
    aliases = {"菜老名": "Jacksing", "李四": "Lee"}
    songs = [_song("孤勇者", "陈奕迅", "菜老名", 1), _song("晴天", "周杰伦", "李四", 2)]
    lines = build_name_lines(songs, emoji_style="text", aliases=aliases)
    assert lines == ["1.Jacksing", "2.Lee"]


def test_build_sharer_lines_groups_by_alias() -> None:
    aliases = {"菜老名": "Jacksing"}
    songs = [
        _song("A", "a", "菜老名", 1),
        _song("B", "b", "菜老名🎵", 2),
    ]
    # 两个原始昵称（一个带 emoji）都映射到 Jacksing，应聚合到同一人
    lines = build_sharer_lines(songs, emoji_style="text", show_artist=True, blank_line=False, aliases=aliases)
    assert lines[0] == "Jacksing（2首）"


if __name__ == "__main__":
    test_build_song_lines_one_per_line()
    test_build_song_lines_hide_artist()
    test_build_song_lines_emoji_cleaned()
    test_build_name_lines_only_sharer()
    test_build_name_lines_emoji_cleaned()
    test_build_sharer_lines_one_song_per_line()
    test_build_sharer_lines_blank_between_people()
    test_build_sharer_lines_no_blank_when_disabled()
    test_fit_description_blank_after_header()
    test_fit_description_no_blank_when_disabled()
    test_resolve_alias_basic()
    test_resolve_alias_emoji_nickname()
    test_sharer_of_applies_alias()
    test_build_song_lines_applies_alias()
    test_build_name_lines_applies_alias()
    test_build_sharer_lines_groups_by_alias()
    print("naming tests OK")

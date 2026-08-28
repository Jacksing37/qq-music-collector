"""收录回复自定义模板与占位符验证。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_reply_template.py
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

from music_collector import build_accept_text, _format_accept, _song_detail_block  # noqa: E402
from music_collector.config import PlaylistConfig, ReplyConfig, config_manager  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.service import service as gsvc  # noqa: E402
from music_collector.store import Store  # noqa: E402

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


class _StubNetease:
    logged_in = True

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


def _song(sid: str = "1", title: str = "晴天", sharer: str = "张三") -> Song:
    return Song(
        platform="netease", song_id=sid, title=title, artists="周杰伦",
        album="叶惠美", url=f"https://music.163.com/song?id={sid}",
        duration=269, sharer_id=111, sharer_name=sharer,
    )


async def _fresh_service(tmp: Path):
    """build_accept_text 走的是全局 service 单例，这里替换它的依赖。"""
    store = Store(tmp / "c.db")
    await store.init()
    gsvc.store = store
    gsvc.netease = _StubNetease()
    config_manager.path = tmp / "config.yaml"
    config_manager.load()
    config_manager.config.playlist = PlaylistConfig()
    return gsvc, store


async def test_builtin_when_disabled() -> None:
    print("\n[A] 关闭自定义时用内置格式")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    config_manager.config.reply = ReplyConfig(enabled=False)
    song = _song()

    text = await build_accept_text(song, 3, 9001)
    check("与内置格式一致", text == _format_accept(song, 3), f"-> {text!r}")
    check("含序号", "第 3 首" in text)
    check("含歌名", "晴天" in text)
    check("含平台", "网易云音乐" in text)


async def test_template_placeholders() -> None:
    print("\n[B] 占位符渲染")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    config_manager.config.reply = ReplyConfig(
        enabled=True,
        accept_text=(
            "第{index}首《{title}》-{artists}\n"
            "分享者:{nick} 专辑:{album} 平台:{platform}\n"
            "时长:{duration} 链接:{url}\n"
            "本期共{count}首 窗口:{window}\n"
            "歌单:{playlist}"
        ),
    )
    gid = 9002
    state = svc.current_window()
    await store.add_song(gid, state.key, _song())

    text = await build_accept_text(_song(), 1, gid)
    check("序号", "第1首" in text, f"-> {text}")
    check("歌名歌手", "《晴天》-周杰伦" in text)
    check("分享者", "分享者:张三" in text)
    check("专辑", "专辑:叶惠美" in text)
    check("平台", "平台:网易云音乐" in text)
    check("时长", "时长:04:29" in text, f"-> {text}")
    check("链接", f"链接:https://music.163.com/song?id=1" in text)
    check("本期数", "本期共1首" in text)
    check("未归档占位", "歌单:（本期歌单还没生成）" in text)


async def test_playlist_placeholder_after_archive() -> None:
    print("\n[C] {playlist} 在归档后输出歌单链接")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    config_manager.config.reply = ReplyConfig(enabled=True, accept_text="歌单:{playlist}")
    gid = 9003
    state = svc.current_window()
    await store.add_song(gid, state.key, _song())
    await store.record_archive(
        gid, state.key, "8888", "https://music.163.com/playlist/8888", 1, 1, 0,
        added_ids=["1"],
    )
    text = await build_accept_text(_song(), 1, gid)
    check("输出歌单链接", "https://music.163.com/playlist/8888" in text, f"-> {text}")

    # 只存 playlist_id 没存 url 时用 api 拼
    await store.record_archive(
        gid, state.key, "9999", None, 1, 1, 0, added_ids=["1"],
    )
    text2 = await build_accept_text(_song(), 1, gid)
    check("无 url 时按 id 拼链接", "playlist/9999" in text2, f"-> {text2}")


async def test_song_block_and_conditional_lines() -> None:
    print("\n[D] {song} 详情块与条件整行")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    config_manager.config.reply = ReplyConfig(
        enabled=True, accept_text="{title}\n{artists_line}{album_line}来源: {platform}\n---\n{song}"
    )
    gid = 9004
    state = svc.current_window()
    await store.add_song(gid, state.key, _song())

    text = await build_accept_text(_song(), 1, gid)
    check("歌手行", "歌手: 周杰伦" in text, f"-> {text}")
    check("专辑行", "专辑: 叶惠美" in text)
    check("详情块含时长", "时长: 04:29" in text)

    # 歌手/专辑为空时整行消失，不留空行
    bare = Song(platform="qq", song_id="x", title="无信息歌曲", sharer_id=1, sharer_name="李四")
    text2 = await build_accept_text(bare, 1, gid)
    check("无歌手时不出现歌手行", "歌手:" not in text2, f"-> {text2}")
    check("无专辑时不出现专辑行", "专辑:" not in text2)
    check("不留多余空行", "\n\n" not in text2, f"-> {text2!r}")


async def test_nickname_alias_in_reply() -> None:
    print("\n[E] {nick} 套用昵称映射")
    tmp = Path(tempfile.mkdtemp())
    svc, store = await _fresh_service(tmp)
    config_manager.config.reply = ReplyConfig(enabled=True, accept_text="来自 {nick}")
    config_manager.config.playlist.sharer_aliases = {"张三": "Jacksing"}
    text = await build_accept_text(_song(), 1, 9005)
    check("昵称已映射", text == "来自 Jacksing", f"-> {text!r}")


async def test_song_detail_block_helper() -> None:
    print("\n[F] 详情块辅助函数")
    block = _song_detail_block(_song())
    lines = block.splitlines()
    check("首行是歌名", lines[0] == "晴天", f"-> {lines}")
    check("含来源", any("来源:" in x for x in lines))
    check("含时长", any("时长: 04:29" in x for x in lines))


async def main() -> None:
    await test_builtin_when_disabled()
    await test_template_placeholders()
    await test_playlist_placeholder_after_archive()
    await test_song_block_and_conditional_lines()
    await test_nickname_alias_in_reply()
    await test_song_detail_block_helper()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

"""跨平台匹配回归：日文组合字符 / feat 变体 等边界。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_crossplatform_match.py
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

from music_collector.archiver import Archiver  # noqa: E402
from music_collector.config import PlaylistConfig  # noqa: E402
from music_collector.models import Song  # noqa: E402
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


class _StubAPI:
    """返回带组合字符/feat 变体的候选，用于验证匹配兜底是否生效。"""

    logged_in = True

    async def search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        k = keyword or ""
        out: list[dict] = []
        if "ビッグマウス" in k or "ビッグマウス" in k:
            out.append({"id": 2131010643, "name": "ビッグマウス",
                        "artists": [{"name": "夢限大みゅーたいぷ"}]})
        if "シガレット" in k:
            out.append({"id": 2087632742, "name": "シガレット feat. xea",
                        "artists": [{"name": "Anonymouz、xea"}]})
        return out


async def test_jp_combining_vs_precomposed():
    """组合字符(ビッグマウス) 应匹配到预组合(ビッグマウス)。"""
    print("\n[A] 日文组合字符 -> 预组合")
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    await store.init()
    arch = Archiver(_StubAPI(), store)
    cfg = PlaylistConfig()  # cross_platform_match=True, strict_match=True
    song = Song(platform="qq", song_id="x", title="ビッグマウス", artists="夢限大みゅーたいぷ")
    nid = await arch.match_netease_id(song, cfg)
    check("组合字符歌名匹配到预组合候选", nid == "2131010643", str(nid))


async def test_feat_variant_prefix():
    """歌名是候选歌名前缀且歌手一致时，应接受 feat 变体。"""
    print("\n[B] 歌名前缀 + 歌手一致 -> 接受 feat 变体")
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    await store.init()
    arch = Archiver(_StubAPI(), store)
    cfg = PlaylistConfig()
    song = Song(platform="qq", song_id="y", title="シガレット", artists="Anonymouz、xea")
    nid = await arch.match_netease_id(song, cfg)
    check("feat 变体被匹配(前缀+歌手一致)", nid == "2087632742", str(nid))


async def test_no_false_prefix_without_artist():
    """歌名前缀一致但歌手完全无关时，不应误匹配。"""
    print("\n[C] 歌名前缀但歌手无关 -> 不误匹配")
    store = Store(Path(tempfile.mktemp(suffix=".db")))
    await store.init()
    arch = Archiver(_StubAPI(), store)
    cfg = PlaylistConfig()
    # 歌手与候选(Anonymouz、xea)无关，仅歌名前缀一致 -> 必须 None
    song = Song(platform="qq", song_id="z", title="シガレット", artists="某 unrelated 歌手")
    nid = await arch.match_netease_id(song, cfg)
    check("歌手无关时不误匹配", nid is None, str(nid))


async def main() -> None:
    await test_jp_combining_vs_precomposed()
    await test_feat_variant_prefix()
    await test_no_false_prefix_without_artist()
    print("\n====================================================")
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    if FAILED:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

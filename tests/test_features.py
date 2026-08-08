"""针对三个新功能点的冒烟测试：自定义歌单命名、分享者简介、缓存清理。

运行: python tests/test_features.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import archiver as arch  # noqa: E402
from music_collector.cache import clean_caches, clean_dir  # noqa: E402
from music_collector.models import Song  # noqa: E402
from music_collector.naming import (  # noqa: E402
    build_context,
    build_sharer_lines,
    build_song_lines,
    fit_description,
    render_template,
    unknown_placeholders,
)
from music_collector.store import Store  # noqa: E402

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


# ---------------------------------------------------------------- 命名模板


def test_naming() -> None:
    print("\n[A] 自定义歌单命名")
    end = datetime(2026, 8, 7, 22, 30)
    ctx = build_context(
        group_id=123456,
        window_label="2026-08-03 ~ 2026-08-09",
        start_at=datetime(2026, 8, 3),
        end_at=end,
        count=12,
        total=15,
        seq=86,
    )
    # 用户给出的示例：Wk.86线上学习26/8/7
    name = render_template("Wk.{seq}线上学习{slash}", ctx)
    check("用户示例渲染", name == "Wk.86线上学习26/8/7", f"-> {name!r}")

    # 其它占位符
    name2 = render_template("Wk.{seq}学习{yy}/{m}/{d} 共{count}首", ctx)
    check("yy/m/d/count", name2 == "Wk.86学习26/8/7 共12首", f"-> {name2!r}")

    # 未知占位符原样保留
    name3 = render_template("歌单{seq}{bogus}", ctx)
    check("未知占位符保留", name3 == "歌单86{bogus}", f"-> {name3!r}")
    check("unknown_placeholders 识别", unknown_placeholders("x{seq}{bogus}", ctx) == ["bogus"])

    # weekday / window
    check("weekday=周五", ctx["weekday"] == "周五", f"-> {ctx['weekday']}")
    check("window 透传", ctx["window"] == "2026-08-03 ~ 2026-08-09")


def test_description() -> None:
    print("\n[B] 分享者简介")
    songs = [
        Song(platform="netease", song_id="1", title="晴天", artists="周杰伦", sharer_name="张三"),
        Song(platform="qq", song_id="2", title="告白气球", artists="周杰伦", sharer_name="李四"),
        Song(platform="netease", song_id="3", title="七里香", artists="周杰伦", sharer_name="张三"),
    ]
    lines = build_song_lines(songs)
    check("逐首清单含分享者", lines[0] == "1. 张三 分享《晴天》 - 周杰伦", f"-> {lines[0]!r}")
    check("逐首清单条数", len(lines) == 3)

    by_person = build_sharer_lines(songs)
    check("按人聚合", by_person[0].startswith("张三（2首）"), f"-> {by_person[0]!r}")

    header = "由 QQ 群 123456 收集，共 3 首。"
    desc = fit_description(header, lines)
    check("简介含表头", header in desc)
    check("简介含分享清单", "李四 分享《告白气球》" in desc)

    # 截断：塞超长清单，验证不超过 990 字
    big = [f"{i}. 用户{i} 分享《很长很长的歌名标题第{i}号》- 某歌手" for i in range(200)]
    long_desc = fit_description(header, big, limit=990)
    check("简介截断 <= 990", len(long_desc) <= 990, f"len={len(long_desc)}")
    check("截断有省略提示", "还有" in long_desc)


# ---------------------------------------------------------------- 缓存清理


def test_cache() -> None:
    print("\n[C] 缓存清理")
    import os

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # 3 个旧封面 + 2 个新封面，外加 5 个旧长图
        old = time.time() - 10 * 86400
        new = time.time() - 60
        for i in range(3):
            p = d / f"cover_{i}.img"
            p.write_bytes(b"x" * 100)
            os.utime(p, (old, old))
        for i in range(2):
            p = d / f"cover_{i}_n.img"
            p.write_bytes(b"x" * 100)
            os.utime(p, (new, new))
        render_dir = d / "render"
        render_dir.mkdir()
        for i in range(5):
            p = render_dir / f"list_{i}.png"
            p.write_bytes(b"x" * 100)
            os.utime(p, (old, old))

        # keep_days=3 -> 旧文件全清；max_cover_files=400/max_render_files=60 不触发个数限制
        res = clean_caches(d, keep_days=3, max_render_files=60, max_cover_files=400)
        # 3 旧封面 + 5 旧长图 = 8 删除；2 新封面 + 0 新长图(全部旧) -> kept 只剩 2 封面
        check("删除数量=8", res.removed == 8, f"-> removed={res.removed}")
        check("保留数量=2", res.kept == 2, f"-> kept={res.kept}")

    # 仅按个数限制（keep_days<=0）
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for i in range(10):
            p = d / f"c_{i}.img"
            p.write_bytes(b"x" * 10)
        res2 = clean_dir(d, ("*.img",), keep_days=0, max_files=4)
        # 10 个，保留最新 4，删 6
        check("按个数删6留4", res2.removed == 6 and res2.kept == 4, f"-> {res2.removed}/{res2.kept}")


# ---------------------------------------------------------------- 归档端到端（mock 网易云）

class _FakeAPI:
    """不联网的网易云 API 桩，只实现 archive 用到的接口。"""

    logged_in = True

    def __init__(self) -> None:
        self.created: list[tuple[str, bool]] = []
        self.descriptions: list[str] = []
        self.added_ids: list[list[str]] = []

    async def create_playlist(self, name: str, privacy: bool) -> int:
        self.created.append((name, privacy))
        return 777

    async def add_tracks(self, playlist_id: int, track_ids: list[str]) -> None:
        self.added_ids.append(list(track_ids))

    async def update_description(self, playlist_id: int, desc: str, name: str = "") -> tuple[bool, str]:
        self.descriptions.append(desc)
        return True, "stub"

    def playlist_url(self, playlist_id: int) -> str:
        return f"https://music.163.com/playlist/{playlist_id}"


async def test_archive_naming() -> None:
    print("\n[D] 归档命名 + 简介集成")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        await store.init()
        s1 = Song(platform="netease", song_id="111", title="晴天", artists="周杰伦", sharer_name="张三")
        s2 = Song(platform="netease", song_id="222", title="七里香", artists="周杰伦", sharer_name="李四")
        await store.add_song(123456, "W1", s1)
        await store.add_song(123456, "W1", s2)

        api = _FakeAPI()
        arc = arch.Archiver(api, store)

        from music_collector.config import PlaylistConfig

        cfg = PlaylistConfig(name_template="Wk.{seq}线上学习{slash}",
                             description_template="由 QQ 群 {group} 收集共 {count} 首。",
                             sharer_style="list", include_sharers=True)

        songs = await store.list_songs(123456, "W1")
        report = await arc.archive(
            123456, "W1", "2026-08-03 ~ 2026-08-09", songs, cfg,
            start_at=datetime(2026, 8, 3), end_at=datetime(2026, 8, 7),
        )
        check("归档成功", report.ok, report.message)
        check("歌单名渲染正确", report.playlist_name == "Wk.1线上学习26/8/7", f"-> {report.playlist_name!r}")
        check("简介写入", len(api.descriptions) == 1 and "张三 分享《晴天》" in api.descriptions[0])
        check("加歌被调用", api.added_ids and set(api.added_ids[0]) == {"111", "222"})


async def main() -> int:
    test_naming()
    test_description()
    test_cache()
    await test_archive_naming()

    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

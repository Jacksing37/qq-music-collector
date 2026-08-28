"""store 层编辑 / 排序能力测试（真实建临时 SQLite）。"""

import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector.models import Song  # noqa: E402
from music_collector.store import Store  # noqa: E402


async def main() -> None:
    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    store = Store(db)
    await store.init()

    songs = [Song(platform="netease", song_id=str(i), title=f"歌{i}") for i in range(1, 4)]
    for s in songs:
        await store.add_song(123, "W1", s)

    lst = await store.list_songs(123, "W1")
    assert [s.title for s in lst] == ["歌1", "歌2", "歌3"], lst

    # get_song_by_index
    assert (await store.get_song_by_index(123, "W1", 2)).title == "歌2"

    # set_song_order：把第 3 首移到最前
    row_ids = [s.row_id for s in lst]
    await store.set_song_order(123, "W1", [row_ids[2], row_ids[0], row_ids[1]])
    lst2 = await store.list_songs(123, "W1")
    assert [s.title for s in lst2] == ["歌3", "歌1", "歌2"], [s.title for s in lst2]

    # update_song_meta：改写末位（歌2）
    await store.update_song_meta(lst2[2].row_id, title="改了", artists="某人")
    s2b = await store.get_song_by_index(123, "W1", 3)
    assert s2b.title == "改了" and s2b.artists == "某人"

    # 写 netease_id + matched
    await store.update_song_meta(lst2[0].row_id, netease_id="999", matched=1)
    lst3 = await store.list_songs(123, "W1")
    assert lst3[0].netease_id == "999" and lst3[0].matched is True

    # 清空后重新添加，排序无关但插入顺序正确
    await store.delete_window(123, "W1")
    await store.add_song(123, "W1", Song(platform="netease", song_id="a", title="A"))
    await store.add_song(123, "W1", Song(platform="netease", song_id="b", title="B"))
    lst4 = await store.list_songs(123, "W1")
    assert [s.title for s in lst4] == ["A", "B"]

    print("OK test_store_edit")


if __name__ == "__main__":
    asyncio.run(main())

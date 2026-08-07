"""SQLite 收集池。

按 (群号, 窗口) 分桶存储，同一窗口内同一首歌只记一次（保留首次分享者），
行号即分享先后顺序。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import aiosqlite

from .models import Song

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    window_key  TEXT    NOT NULL,
    platform    TEXT    NOT NULL,
    song_id     TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    artists     TEXT    NOT NULL DEFAULT '',
    album       TEXT    NOT NULL DEFAULT '',
    cover       TEXT    NOT NULL DEFAULT '',
    url         TEXT    NOT NULL DEFAULT '',
    duration    INTEGER NOT NULL DEFAULT 0,
    sharer_id   INTEGER NOT NULL DEFAULT 0,
    sharer_name TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    netease_id  TEXT,
    matched     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, window_key, platform, song_id)
);
CREATE INDEX IF NOT EXISTS idx_songs_window ON songs(group_id, window_key, id);

CREATE TABLE IF NOT EXISTS archives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    window_key   TEXT    NOT NULL,
    playlist_id  TEXT,
    playlist_url TEXT,
    total        INTEGER NOT NULL DEFAULT 0,
    added        INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL,
    UNIQUE(group_id, window_key)
);
"""

_COLUMNS = (
    "id, group_id, window_key, platform, song_id, title, artists, album, "
    "cover, url, duration, sharer_id, sharer_name, created_at, netease_id, matched"
)


def _row_to_song(row: aiosqlite.Row) -> Song:
    return Song(
        platform=row["platform"],
        song_id=row["song_id"],
        title=row["title"],
        artists=row["artists"],
        album=row["album"],
        cover=row["cover"],
        url=row["url"],
        duration=row["duration"],
        sharer_id=row["sharer_id"],
        sharer_name=row["sharer_name"],
        created_at=row["created_at"],
        netease_id=row["netease_id"],
        matched=bool(row["matched"]),
        row_id=row["id"],
    )


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ------------------------------------------------------------ 写入

    async def add_song(self, group_id: int, window_key: str, song: Song) -> tuple[bool, Song]:
        """插入一首歌。返回 (是否新增, 最终落库的 Song)。

        重复分享时返回已有记录，方便上层提示"这首已经有人分享过了"。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO songs
                    (group_id, window_key, platform, song_id, title, artists, album,
                     cover, url, duration, sharer_id, sharer_name, created_at,
                     netease_id, matched)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id, window_key, song.platform, song.song_id, song.title,
                    song.artists, song.album, song.cover, song.url, song.duration,
                    song.sharer_id, song.sharer_name, song.created_at or time.time(),
                    song.netease_id, int(song.matched),
                ),
            )
            inserted = cursor.rowcount > 0
            await db.commit()

            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                "WHERE group_id=? AND window_key=? AND platform=? AND song_id=?",
                (group_id, window_key, song.platform, song.song_id),
            ) as cur:
                row = await cur.fetchone()
        return inserted, (_row_to_song(row) if row else song)

    async def mark_matched(self, row_id: int, netease_id: Optional[str]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE songs SET netease_id=?, matched=? WHERE id=?",
                (netease_id, 1 if netease_id else 0, row_id),
            )
            await db.commit()

    async def record_archive(
        self,
        group_id: int,
        window_key: str,
        playlist_id: Optional[str],
        playlist_url: Optional[str],
        total: int,
        added: int,
        failed: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO archives
                    (group_id, window_key, playlist_id, playlist_url, total, added, failed, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id, window_key) DO UPDATE SET
                    playlist_id=excluded.playlist_id,
                    playlist_url=excluded.playlist_url,
                    total=excluded.total,
                    added=excluded.added,
                    failed=excluded.failed,
                    created_at=excluded.created_at
                """,
                (group_id, window_key, playlist_id, playlist_url, total, added, failed, time.time()),
            )
            await db.commit()

    # ------------------------------------------------------------ 读取

    async def list_songs(self, group_id: int, window_key: str) -> list[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs WHERE group_id=? AND window_key=? ORDER BY id ASC",
                (group_id, window_key),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_song(r) for r in rows]

    async def count(self, group_id: int, window_key: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM songs WHERE group_id=? AND window_key=?",
                (group_id, window_key),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def position_of(self, group_id: int, window_key: str, row_id: int) -> int:
        """某条记录在榜单中的序号（从 1 开始）。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM songs WHERE group_id=? AND window_key=? AND id<=?",
                (group_id, window_key, row_id),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def groups_in_window(self, window_key: str) -> list[int]:
        """该窗口下有数据的所有群，供定时任务遍历。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT group_id FROM songs WHERE window_key=?", (window_key,)
            ) as cur:
                rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

    async def get_archive(self, group_id: int, window_key: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM archives WHERE group_id=? AND window_key=?",
                (group_id, window_key),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def remove_song(self, group_id: int, window_key: str, index: int) -> Optional[Song]:
        """按列表序号（从 1 开始）删除一条记录，用于管理员纠错。"""
        songs = await self.list_songs(group_id, window_key)
        if not (1 <= index <= len(songs)):
            return None
        target = songs[index - 1]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM songs WHERE id=?", (target.row_id,))
            await db.commit()
        return target

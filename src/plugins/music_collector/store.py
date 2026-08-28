"""SQLite 收集池。

按 (群号, 窗口) 分桶存储，同一窗口内同一首歌只记一次（保留首次分享者），
行号即分享先后顺序。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Sequence

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
    -- 显式排序权重：拖拽/上下移动时改写，便于网页端手动调整榜单顺序
    -- （默认 0，按 id 升序回退到分享先后顺序）
    sort_order  INTEGER NOT NULL DEFAULT 0,
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
    -- 该窗口歌单已收录的网易云歌曲 id（JSON 数组），用于同窗口再次归档时增量追加去重
    added_ids    TEXT    NOT NULL DEFAULT '[]',
    UNIQUE(group_id, window_key)
);

-- 简介写入失败时暂存，等定时任务或 /music descfix 补写
CREATE TABLE IF NOT EXISTS pending_desc (
    playlist_id   TEXT    PRIMARY KEY,
    playlist_name TEXT    NOT NULL DEFAULT '',
    group_id      INTEGER NOT NULL DEFAULT 0,
    description   TEXT    NOT NULL,
    last_error    TEXT    NOT NULL DEFAULT '',
    tries         INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    -- 所属窗口：补写时据此取当前歌曲重新生成简介，避免用旧文本硬推
    window_key    TEXT    NOT NULL DEFAULT '',
    -- 归档当时的上下文快照（JSON）：{label, start_at, end_at, songs:[...]}
    -- 歌曲已被清空时用它重建，保证补写的清单与歌单内容一致
    snapshot      TEXT    NOT NULL DEFAULT '{}'
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
            # 老库迁移：archives 表补 added_ids 列（同窗口再次归档增量去重用）
            async with db.execute("PRAGMA table_info(archives)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
            if "added_ids" not in cols:
                await db.execute(
                    "ALTER TABLE archives ADD COLUMN added_ids TEXT NOT NULL DEFAULT '[]'"
                )
            # 老库迁移：pending_desc 表补 window_key / snapshot 列（补写简介重建用）
            async with db.execute("PRAGMA table_info(pending_desc)") as cur:
                pcols = [row[1] for row in await cur.fetchall()]
            if "window_key" not in pcols:
                await db.execute(
                    "ALTER TABLE pending_desc ADD COLUMN window_key TEXT NOT NULL DEFAULT ''"
                )
            if "snapshot" not in pcols:
                await db.execute(
                    "ALTER TABLE pending_desc ADD COLUMN snapshot TEXT NOT NULL DEFAULT '{}'"
                )
            # 老库迁移：songs 表补 sort_order 列（网页端手动排序用）
            async with db.execute("PRAGMA table_info(songs)") as cur:
                scols = [row[1] for row in await cur.fetchall()]
            if "sort_order" not in scols:
                await db.execute(
                    "ALTER TABLE songs ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                )
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
        added_ids: Optional[Sequence[str]] = None,
    ) -> None:
        """记录 / 更新归档信息。

        ``added_ids`` 是该窗口歌单当前已收录的网易云 id 列表（用于同窗口
        再次归档时的增量去重）；不传时保持库中已有值不变。
        """
        prev = await self.get_archive(group_id, window_key)
        merged = added_ids
        if merged is None:
            merged = json.loads((prev or {}).get("added_ids") or "[]")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO archives
                    (group_id, window_key, playlist_id, playlist_url, total, added,
                     failed, created_at, added_ids)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id, window_key) DO UPDATE SET
                    playlist_id=excluded.playlist_id,
                    playlist_url=excluded.playlist_url,
                    total=excluded.total,
                    added=excluded.added,
                    failed=excluded.failed,
                    created_at=excluded.created_at,
                    added_ids=excluded.added_ids
                """,
                (
                    group_id, window_key, playlist_id, playlist_url, total, added,
                    failed, time.time(), json.dumps(merged, ensure_ascii=False),
                ),
            )
            await db.commit()

    # ------------------------------------------------------- 简介待补写队列

    async def save_pending_desc(
        self,
        playlist_id: str,
        playlist_name: str,
        group_id: int,
        description: str,
        last_error: str,
        window_key: Optional[str] = None,
        snapshot: Optional[dict] = None,
    ) -> None:
        """简介写入失败时入队，等后续自动 / 手动补写。

        ``window_key`` / ``snapshot`` 为 None 时保留库中已有值（重试失败时
        不要把首次存档的上下文冲掉）。
        """
        now = time.time()
        prev = await self.get_pending_desc(playlist_id)
        if window_key is None:
            window_key = (prev or {}).get("window_key") or ""
        if snapshot is None:
            raw_snap = (prev or {}).get("snapshot") or "{}"
            try:
                snapshot = json.loads(raw_snap) if isinstance(raw_snap, str) else dict(raw_snap)
            except json.JSONDecodeError:
                snapshot = {}
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO pending_desc
                    (playlist_id, playlist_name, group_id, description,
                     last_error, tries, created_at, updated_at, window_key, snapshot)
                VALUES (?,?,?,?,?,1,?,?,?,?)
                ON CONFLICT(playlist_id) DO UPDATE SET
                    playlist_name=excluded.playlist_name,
                    description=excluded.description,
                    last_error=excluded.last_error,
                    tries=pending_desc.tries + 1,
                    updated_at=excluded.updated_at,
                    window_key=excluded.window_key,
                    snapshot=excluded.snapshot
                """,
                (
                    playlist_id, playlist_name, group_id, description, last_error,
                    now, now, window_key, json.dumps(snapshot, ensure_ascii=False),
                ),
            )
            await db.commit()

    async def get_pending_desc(self, playlist_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM pending_desc WHERE playlist_id=?", (playlist_id,)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def get_archive_by_playlist(self, playlist_id: str) -> Optional[dict]:
        """按歌单 id 反查归档记录（补写简介时用它对齐实际收录数）。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM archives WHERE playlist_id=?", (playlist_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        result = dict(row)
        try:
            result["added_ids"] = set(json.loads(result.get("added_ids") or "[]"))
        except (TypeError, json.JSONDecodeError):
            result["added_ids"] = set()
        return result

    async def list_pending_desc(self, group_id: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM pending_desc"
        args: tuple = ()
        if group_id is not None:
            sql += " WHERE group_id=?"
            args = (group_id,)
        sql += " ORDER BY updated_at ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def drop_pending_desc(self, playlist_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM pending_desc WHERE playlist_id=?", (playlist_id,))
            await db.commit()

    # ------------------------------------------------------------ 读取

    async def list_songs(self, group_id: int, window_key: str) -> list[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                "WHERE group_id=? AND window_key=? ORDER BY sort_order ASC, id ASC",
                (group_id, window_key),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_song(r) for r in rows]

    async def get_song_by_index(self, group_id: int, window_key: str, index: int) -> Optional[Song]:
        """按列表序号（从 1 开始）取一条记录，用于编辑 / 手动匹配定位。"""
        songs = await self.list_songs(group_id, window_key)
        if not (1 <= index <= len(songs)):
            return None
        return songs[index - 1]

    # 允许通过 update_song_meta 修改的字段白名单
    _EDITABLE = (
        "title", "artists", "album", "sharer_name", "sharer_id",
        "netease_id", "matched",
    )

    async def update_song_meta(self, row_id: int, **fields: object) -> bool:
        """按 row_id 更新歌曲的可编辑字段（歌名 / 歌手 / 分享者 / 匹配信息等）。

        只接受 ``_EDITABLE`` 里的键，其余忽略，避免误改主键列。返回是否真的改了。
        """
        sets: list[str] = []
        values: list[object] = []
        for key in self._EDITABLE:
            if key in fields:
                val = fields[key]
                if key == "matched":
                    val = 1 if val else 0
                sets.append(f"{key}=?")
                values.append(val)
        if not sets:
            return False
        values.append(row_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE songs SET {', '.join(sets)} WHERE id=?", tuple(values)
            )
            await db.commit()
        return True

    async def set_song_order(
        self, group_id: int, window_key: str, ordered_row_ids: Sequence[int]
    ) -> int:
        """把本窗口歌曲重排为 ``ordered_row_ids`` 指定的顺序（顺序即榜单顺序）。

        通过重写每行的 sort_order 实现，避免改动自增主键。返回受影响行数。
        """
        async with aiosqlite.connect(self.db_path) as db:
            changed = 0
            for idx, rid in enumerate(ordered_row_ids):
                cur = await db.execute(
                    "UPDATE songs SET sort_order=? WHERE id=? AND group_id=? AND window_key=?",
                    (idx, rid, group_id, window_key),
                )
                changed += cur.rowcount
            await db.commit()
        return changed

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

    async def all_groups(self) -> list[int]:
        """所有出现过收集记录的群（不限窗口），用于"开始收录"全群广播。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT group_id FROM songs"
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
        if not row:
            return None
        result = dict(row)
        try:
            result["added_ids"] = set(json.loads(result.get("added_ids") or "[]"))
        except (TypeError, json.JSONDecodeError):
            result["added_ids"] = set()
        return result

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

    async def delete_songs_by_indices(
        self, group_id: int, window_key: str, indices: Sequence[int]
    ) -> int:
        """按序号（从 1 开始）批量删除，支持不连续与重复序号。返回实际删除条数。"""
        songs = await self.list_songs(group_id, window_key)
        row_ids: set[int] = set()
        for i in indices:
            if 1 <= i <= len(songs) and songs[i - 1].row_id is not None:
                row_ids.add(songs[i - 1].row_id)
        if not row_ids:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "DELETE FROM songs WHERE id=?", [(rid,) for rid in row_ids]
            )
            await db.commit()
        return len(row_ids)

    async def delete_window(self, group_id: int, window_key: str) -> int:
        """清空某个群在某个窗口下的全部已收集歌曲。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM songs WHERE group_id=? AND window_key=?",
                (group_id, window_key),
            )
            await db.commit()
        return cur.rowcount

    async def prune_old(self, before_ts: float) -> int:
        """删除早于 ``before_ts`` 的收集记录（按创建时间）。用于定时清理。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM songs WHERE created_at < ?", (before_ts,))
            await db.commit()
        return cur.rowcount

    async def windows_with_counts(
        self, group_id: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """列出各窗口及其歌曲数，供手动清理时选择目标窗口。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if group_id is None:
                async with db.execute(
                    "SELECT window_key, COUNT(*) AS n FROM songs "
                    "GROUP BY window_key ORDER BY window_key DESC"
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT window_key, COUNT(*) AS n FROM songs WHERE group_id=? "
                    "GROUP BY window_key ORDER BY window_key DESC",
                    (group_id,),
                ) as cur:
                    rows = await cur.fetchall()
        return [(r["window_key"], int(r["n"])) for r in rows]

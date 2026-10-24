"""SQLite 收集池。

按 (群号, 窗口) 分桶存储，同一窗口内同一首歌只记一次（保留首次分享者），
行号即分享先后顺序。
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Optional, Sequence

import aiosqlite

from .models import Song

#: 总库（跨窗口去重的群级歌曲库）在 songs 表里使用的虚拟窗口键。
#: 复用现有 songs / archives 全部基础设施，避免新建表与重复逻辑。
MASTER_KEY = "__master__"

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
    src_window  TEXT    NOT NULL DEFAULT '',
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
    -- 歌单**实际**名字（复用归档时用它显示真实名，避免拿窗口标签当假名展示）
    playlist_name TEXT   NOT NULL DEFAULT '',
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

-- 从网易云歌单导入总库的历史记录，用于「撤回上次导入」
-- added_row_ids 是本批次新插入的 songs.id（JSON 数组），撤回时只删这些行
CREATE TABLE IF NOT EXISTS import_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL,
    window_key    TEXT    NOT NULL,
    playlist_id   TEXT,
    playlist_url  TEXT,
    total         INTEGER NOT NULL DEFAULT 0,
    added         INTEGER NOT NULL DEFAULT 0,
    added_row_ids TEXT    NOT NULL DEFAULT '[]',
    created_at    REAL    NOT NULL,
    undone        INTEGER NOT NULL DEFAULT 0
);

-- 「同一窗口同一用户只收录第一首」的**占位**记录。
-- 关键点：占位发生在「分享」这一刻，而不是「收录成功」那一刻——所以不能靠
-- songs 表反推（首发那首可能因重复 / 无法匹配没进榜，甚至属于别人）。
-- 每个 (group, window, sharer) 只会有一行，记下首发歌名，供提示文案使用。
CREATE TABLE IF NOT EXISTS sharer_claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    window_key   TEXT    NOT NULL,
    sharer_id    INTEGER NOT NULL,
    sharer_name  TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    artists      TEXT    NOT NULL DEFAULT '',
    platform     TEXT    NOT NULL DEFAULT '',
    -- 首发那首的平台 key（与 songs.song_id 同一套），用于判断占位是否已失效：
    -- 管理员把歌删掉后，靠它就能认出「这个名额所指的歌已经不在库里了」。
    song_id      TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL,
    UNIQUE(group_id, window_key, sharer_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_window ON sharer_claims(group_id, window_key);

-- 机器人发出的「收录 / 无法匹配」提示消息 -> 歌曲行 的映射。
-- 群里**引用**这条消息并贴上网易云链接，就能精确回填这首歌的匹配
-- （见 service.match_unmatched_by_row）。必须落库而不是放内存：重启 / 部署后
-- 群里那条旧消息还在，引用它得仍然能定位到歌。随 prune_old 按时间清理。
CREATE TABLE IF NOT EXISTS song_notices (
    message_id  TEXT    PRIMARY KEY,
    group_id    INTEGER NOT NULL,
    window_key  TEXT    NOT NULL,
    row_id      INTEGER NOT NULL,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_song_notices_group ON song_notices(group_id, message_id);

-- 群聊记录。一张表两个用途，靠 kind 区分：
--   kind='chat'   与机器人的一问一答 —— 就是「对话上下文」，重启后从这里恢复；
--   kind='listen' 旁听到的群消息 —— 只喂给记忆整理任务，绝不进请求上下文
--                （否则群友的闲聊会被当成「机器人和你的对话」，几小时就把窗口撑爆）。
-- digested 是整理游标：整理任务处理过的行标 1，下次只取 0 的。
CREATE TABLE IF NOT EXISTS chat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0,
    nick        TEXT    NOT NULL DEFAULT '',
    kind        TEXT    NOT NULL DEFAULT 'chat',
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    reasoning   TEXT    NOT NULL DEFAULT '',
    digested    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_log_group ON chat_log(group_id, kind, id);
CREATE INDEX IF NOT EXISTS idx_chat_log_time ON chat_log(created_at);
"""

_COLUMNS = (
    "id, group_id, window_key, platform, song_id, title, artists, album, "
    "cover, url, duration, sharer_id, sharer_name, created_at, netease_id, matched, src_window"
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
        window_key=row["window_key"],
        src_window=row["src_window"],
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
            # 老库迁移：archives 表补 playlist_name 列（复用归档显示真实歌单名）
            if "playlist_name" not in cols:
                await db.execute(
                    "ALTER TABLE archives ADD COLUMN playlist_name TEXT NOT NULL DEFAULT ''"
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
            if "src_window" not in scols:
                await db.execute(
                    "ALTER TABLE songs ADD COLUMN src_window TEXT NOT NULL DEFAULT ''"
                )
            # 老库迁移：sharer_claims 表补 song_id 列（判定占位是否失效用）。
            # 老占位没有这一列，回落到按歌名匹配，见 claim_song_in_library()。
            async with db.execute("PRAGMA table_info(sharer_claims)") as cur:
                ccols = [row[1] for row in await cur.fetchall()]
            if "song_id" not in ccols:
                await db.execute(
                    "ALTER TABLE sharer_claims ADD COLUMN song_id TEXT NOT NULL DEFAULT ''"
                )
            await db.commit()

    # ------------------------------------------------------------ 写入

    async def add_song(
        self, group_id: int, window_key: str, song: Song,
        src_window: Optional[str] = None,
    ) -> tuple[bool, Song]:
        """插入一首歌。返回 (是否新增, 最终落库的 Song)。

        重复分享时返回已有记录，方便上层提示"这首已经有人分享过了"。
        ``src_window`` 用于总库记录回溯首发来源窗口（默认等于 window_key）。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO songs
                    (group_id, window_key, platform, song_id, title, artists, album,
                     cover, url, duration, sharer_id, sharer_name, created_at,
                     netease_id, matched, src_window)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id, window_key, song.platform, song.song_id, song.title,
                    song.artists, song.album, song.cover, song.url, song.duration,
                    song.sharer_id, song.sharer_name, song.created_at or time.time(),
                    song.netease_id, int(song.matched),
                    src_window or window_key,
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

    async def find_in_window(
        self, group_id: int, window_key: str, song: Song
    ) -> Optional[Song]:
        """查询某窗口是否已收录过同一首歌（platform + song_id 相同）。

        仅查询、不写入。用于开启总库后判定「同窗口重复分享」：已存在总库的歌
        不再写入当前窗口，但仍需据其在当前窗口是否已有记录决定要不要发
        ``notify_duplicate`` 提示，避免重复分享完全静默。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                "WHERE group_id=? AND window_key=? AND platform=? AND song_id=?",
                (group_id, window_key, song.platform, song.song_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_song(row) if row else None

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
        playlist_name: Optional[str] = None,
    ) -> None:
        """记录 / 更新归档信息。

        ``added_ids`` 是该窗口歌单当前已收录的网易云 id 列表（用于同窗口
        再次归档时的增量去重）；``playlist_name`` 是歌单实际名字。
        两者不传时都保持库中已有值不变。
        """
        prev = await self.get_archive(group_id, window_key)
        merged = added_ids
        if merged is None:
            merged = json.loads((prev or {}).get("added_ids") or "[]")
        name = playlist_name if playlist_name is not None else (prev or {}).get("playlist_name") or ""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO archives
                    (group_id, window_key, playlist_id, playlist_url, total, added,
                     failed, created_at, added_ids, playlist_name)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id, window_key) DO UPDATE SET
                    playlist_id=excluded.playlist_id,
                    playlist_url=excluded.playlist_url,
                    total=excluded.total,
                    added=excluded.added,
                    failed=excluded.failed,
                    created_at=excluded.created_at,
                    added_ids=excluded.added_ids,
                    playlist_name=excluded.playlist_name
                """,
                (
                    group_id, window_key, playlist_id, playlist_url, total, added,
                    failed, time.time(), json.dumps(merged, ensure_ascii=False), name,
                ),
            )
            await db.commit()

    # ----------------------------------------------------- 歌单导入历史 / 撤回
    async def record_import(
        self, group_id: int, window_key: str, playlist_id: Optional[str],
        playlist_url: str, total: int, added: int, added_row_ids: Sequence[int],
    ) -> int:
        """记录一次「从歌单导入总库」的结果，返回 history id（供撤回）。

        ``added_row_ids`` 是本批次**新插入**的 songs.id 列表；撤回时只删这些行，
        不会误伤导入前已存在或后来手动添加的歌。
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO import_history
                    (group_id, window_key, playlist_id, playlist_url, total, added,
                     added_row_ids, created_at, undone)
                VALUES (?,?,?,?,?,?,?,?,0)
                """,
                (
                    group_id, window_key, playlist_id, playlist_url, total, added,
                    json.dumps(list(added_row_ids), ensure_ascii=False),
                    time.time(),
                ),
            )
            await db.commit()
            return cur.lastrowid

    async def list_imports(
        self, group_id: Optional[int] = None, window_key: str = MASTER_KEY,
        limit: int = 20,
    ) -> list[dict]:
        """列出导入历史（含已撤回），按时间倒序。供 WebUI 展示与撤回。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if group_id is None:
                async with db.execute(
                    "SELECT * FROM import_history WHERE window_key=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (window_key, limit),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT * FROM import_history WHERE group_id=? AND window_key=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (group_id, window_key, limit),
                ) as cur:
                    rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["added_row_ids"] = json.loads(d.get("added_row_ids") or "[]")
            except (TypeError, json.JSONDecodeError):
                d["added_row_ids"] = []
            out.append(d)
        return out

    async def get_import(self, history_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM import_history WHERE id=?", (history_id,)
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["added_row_ids"] = json.loads(d.get("added_row_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            d["added_row_ids"] = []
        return d

    async def undo_import(self, history_id: int) -> dict:
        """撤回一次导入：删除该批次新插入的总库歌曲，并标记记录为已撤回。

        只删 ``added_row_ids`` 中仍存在的行（手动删过的会被跳过）；幂等：
        已撤回的记录再次调用直接返回提示。撤回不影响已生成的网易云歌单，
        需要同步以移除请手动点「同步全部歌单」。
        """
        rec = await self.get_import(history_id)
        if not rec:
            return {"ok": False, "message": "找不到该次导入记录"}
        if rec.get("undone"):
            return {"ok": False, "message": "该次导入已撤回过"}
        row_ids = [int(x) for x in (rec.get("added_row_ids") or [])]
        async with aiosqlite.connect(self.db_path) as db:
            removed = 0
            if row_ids:
                placeholders = ",".join("?" * len(row_ids))
                cur = await db.execute(
                    f"DELETE FROM songs WHERE id IN ({placeholders}) "
                    f"AND window_key=? AND group_id=?",
                    (*row_ids, rec["window_key"], rec["group_id"]),
                )
                removed = cur.rowcount
            await db.execute(
                "UPDATE import_history SET undone=1 WHERE id=?", (history_id,)
            )
            await db.commit()
        added = rec.get("added", 0)
        return {
            "ok": True,
            "removed": removed,
            "added": added,
            "message": f"已撤回该次导入，从总库移除 {removed} 首"
            + ("" if removed == added else "（部分歌曲已被手动删除）"),
        }

    async def distinct_group_ids(self) -> list[int]:
        """返回 songs 表中出现过的全部群号（用于「导入到哪个群」下拉）。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT DISTINCT group_id FROM songs") as cur:
                rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

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

    async def list_songs(
        self, group_id: int, window_key: str, newest_first: bool = False
    ) -> list[Song]:
        order = "sort_order DESC, id DESC" if newest_first else "sort_order ASC, id ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                f"WHERE group_id=? AND window_key=? ORDER BY {order}",
                (group_id, window_key),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_song(r) for r in rows]

    async def search_songs(
        self, group_id: int, window_key: str, query: str = "",
        date: Optional[str] = None, newest_first: bool = False,
    ) -> list[Song]:
        """按关键词 / 日期过滤某窗口某群的歌曲，用于 WebUI 搜索。

        - ``query`` 不区分大小写，模糊匹配 title / artists / sharer_name /
          song_id / netease_id；传空串表示不过滤。
        - ``date`` 形如 ``'YYYY-MM-DD'``（服务器本地时区，与前端表格展示一致），
          只返回该天收录的歌；格式非法则忽略。
        """
        clauses = ["group_id=?", "window_key=?"]
        params: list[object] = [group_id, window_key]
        if query:
            like = f"%{query}%"
            clauses.append(
                "(title LIKE ? OR artists LIKE ? OR sharer_name LIKE ? "
                "OR song_id LIKE ? OR netease_id LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        if date:
            try:
                y, m, d = (int(x) for x in date.split("-"))
                start = time.mktime(
                    datetime.datetime(y, m, d, 0, 0, 0).timetuple()
                )
                clauses.append("created_at >= ? AND created_at < ?")
                params.append(start)
                params.append(start + 86400)
            except (ValueError, TypeError):
                pass
        order = "sort_order DESC, id DESC" if newest_first else "sort_order ASC, id ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                f"WHERE {' AND '.join(clauses)} ORDER BY {order}",
                params,
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_song(r) for r in rows]

    async def get_song_by_index(
        self, group_id: int, window_key: str, index: int, newest_first: bool = False
    ) -> Optional[Song]:
        """按列表序号（从 1 开始）取一条记录，用于编辑 / 手动匹配定位。

        ``newest_first`` 必须与渲染该列表时一致（总库视图为 True），否则序号与
        row_id 映射错位，会定位/操作到错误的歌曲。
        """
        songs = await self.list_songs(group_id, window_key, newest_first=newest_first)
        if not (1 <= index <= len(songs)):
            return None
        return songs[index - 1]

    async def get_song_by_row(self, row_id: int) -> Optional[Song]:
        """按主键 row_id 直接取一条歌曲（引用消息回填匹配时用，无序号歧义）。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs WHERE id=?", (row_id,)
            ) as cur:
                row = await cur.fetchone()
        return _row_to_song(row) if row else None

    # 允许通过 update_song_meta 修改的字段白名单
    _EDITABLE = (
        "title", "artists", "album", "sharer_name", "sharer_id",
        "netease_id", "matched", "url",
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
        """所有出现过收集记录的群（不限窗口），用于"开始收录"全群广播。

        排除总库虚拟窗口，否则只往总库里分享过的群也会被纳入广播。
        """
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT group_id FROM songs WHERE window_key<>?", (MASTER_KEY,)
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
            # 歌被管理员删掉后，该分享者的「本期已分享」占位也要一并释放，
            # 否则本人再分享会被当成重复、永远收不进来。
            await self._release_claims(db, group_id, window_key, {target.sharer_id})
            await db.commit()
        return target

    async def delete_songs_by_indices(
        self,
        group_id: int,
        window_key: str,
        indices: Sequence[int],
        newest_first: bool = False,
    ) -> int:
        """按序号（从 1 开始）批量删除，支持不连续与重复序号。返回实际删除条数。

        ``newest_first`` 必须与渲染该列表时一致（总库视图为 True），否则序号与
        row_id 映射错位，删错歌曲（勾选的没删掉、反而删了别的）。
        """
        songs = await self.list_songs(group_id, window_key, newest_first=newest_first)
        row_ids: set[int] = set()
        sharer_ids: set[int] = set()
        for i in indices:
            if 1 <= i <= len(songs) and songs[i - 1].row_id is not None:
                row_ids.add(songs[i - 1].row_id)
                if songs[i - 1].sharer_id:
                    sharer_ids.add(int(songs[i - 1].sharer_id))
        if not row_ids:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "DELETE FROM songs WHERE id=?", [(rid,) for rid in row_ids]
            )
            # 顺手释放这些分享者的「本期已分享」占位：管理员把歌删掉，就说明
            # 想把名额还给他（否则他再分享会被拦，且提示的还是已删掉的那首）。
            await self._release_claims(db, group_id, window_key, sharer_ids)
            await db.commit()
        return len(row_ids)

    async def _release_claims(
        self, db: aiosqlite.Connection, group_id: int, window_key: str,
        sharer_ids: "set[int] | list[int]",
    ) -> int:
        """在已打开的连接里删除这些分享者在该窗口的占位，返回删除条数。

        ``sharer_claims`` 是按 (群, 窗口, 用户) 唯一的，所以只有「把他这个窗口的
        收录记录删干净」才该释放——这也是调用方的语义。
        总库（MASTER_KEY）是跨窗口聚合视图，占位只属于具体窗口，直接跳过，
        避免管理员在总库视图里删一首歌就把某个窗口的名额误放掉。
        """
        if window_key == MASTER_KEY:
            return 0
        ids = sorted({int(s) for s in sharer_ids if s})
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = await db.execute(
            f"DELETE FROM sharer_claims WHERE group_id=? AND window_key=? "
            f"AND sharer_id IN ({marks})",
            (group_id, window_key, *ids),
        )
        return cur.rowcount

    async def release_claims_for(
        self, group_id: int, window_key: str, sharer_ids: Sequence[int]
    ) -> int:
        """在独立连接里释放占位（供 service / 测试直接调用）。"""
        async with aiosqlite.connect(self.db_path) as db:
            n = await self._release_claims(db, group_id, window_key, set(sharer_ids))
            await db.commit()
        return n

    async def delete_window(self, group_id: int, window_key: str) -> int:
        """清空某个群在某个窗口下的全部已收集歌曲（含该窗口的分享占位记录）。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM songs WHERE group_id=? AND window_key=?",
                (group_id, window_key),
            )
            await db.execute(
                "DELETE FROM sharer_claims WHERE group_id=? AND window_key=?",
                (group_id, window_key),
            )
            await db.commit()
        return cur.rowcount

    async def prune_old(self, before_ts: float) -> int:
        """删除早于 ``before_ts`` 的收集记录（按创建时间）。用于定时清理。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM songs WHERE created_at < ?", (before_ts,))
            await db.execute(
                "DELETE FROM sharer_claims WHERE created_at < ?", (before_ts,)
            )
            # 引用消息映射跟着一起清，否则表会随收集无限长大
            await db.execute(
                "DELETE FROM song_notices WHERE created_at < ?", (before_ts,)
            )
            await db.commit()
        return cur.rowcount

    # ------------------------------------------- 提示消息 -> 歌曲（引用回填匹配）

    async def record_song_notice(
        self,
        message_id: str,
        group_id: int,
        window_key: str,
        row_id: int,
        created_at: Optional[float] = None,
    ) -> None:
        """记下「机器人发的这条提示消息对应库里哪首歌」，供群里引用时反查。

        ``message_id`` 是 OneBot 发消息后返回的 id；同一首歌会有多条提示
        （文字 + 卡片），各自一行，引用任意一条都能定位。
        """
        mid = str(message_id or "").strip()
        if not mid or row_id is None:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO song_notices"
                "(message_id, group_id, window_key, row_id, created_at) VALUES(?,?,?,?,?)",
                (mid, group_id, window_key, row_id,
                 float(created_at if created_at is not None else time.time())),
            )
            await db.commit()

    async def resolve_song_notice(
        self, message_id: str, group_id: Optional[int] = None
    ) -> Optional[dict]:
        """由被引用消息 id 反查它对应的歌曲记录；查不到返回 None。

        返回 ``{message_id, group_id, window_key, row_id, created_at}``。
        带上 ``group_id`` 时只在同群内匹配，避免极端情况下消息 id 串群。
        """
        mid = str(message_id or "").strip()
        if not mid:
            return None
        sql = "SELECT * FROM song_notices WHERE message_id=?"
        params: list[object] = [mid]
        if group_id is not None:
            sql += " AND group_id=?"
            params.append(group_id)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------ 同一用户本期占位

    async def get_sharer_claim(
        self, group_id: int, window_key: str, sharer_id: int
    ) -> Optional[dict]:
        """取某用户在某窗口的首发占位记录；没有则 None。

        返回的 dict 含 ``title`` / ``artists`` / ``platform`` / ``song_id`` /
        ``created_at``，提示文案里的「你已经分享过《…》」就是用这里的首发歌名。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sharer_claims "
                "WHERE group_id=? AND window_key=? AND sharer_id=?",
                (group_id, window_key, sharer_id),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def add_sharer_claim(
        self,
        group_id: int,
        window_key: str,
        sharer_id: int,
        title: str,
        artists: str = "",
        platform: str = "",
        sharer_name: str = "",
        song_id: str = "",
    ) -> bool:
        """登记首发占位。返回 True 表示本次真的占上了（此前没有记录）。

        并发分享同一窗口时用 ``INSERT OR IGNORE`` 兜底：只有第一个写进去的
        算占位成功，后来者会拿到 False 并被拦截。
        ``song_id`` 存首发歌的平台 key，供 ``claim_song_in_library`` 判断占位
        是否已失效（歌被删了，名额就该还回去）。
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO sharer_claims "
                "(group_id, window_key, sharer_id, sharer_name, title, artists, "
                " platform, song_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_id, window_key, sharer_id, sharer_name,
                    title, artists, platform, song_id or "", time.time(),
                ),
            )
            await db.commit()
        return cur.rowcount > 0

    async def claim_song_in_library(
        self, group_id: int, window_key: str, claim: dict
    ) -> bool:
        """占位所指的那首歌是否还在库里（该窗口 或 该群总库）。

        用于识别「幽灵名额」：管理员在网页端把歌删掉（哪些视图、哪条路径都可能）、
        或清理任务把它带走之后，占位还在，本人再分享就会被拦、而且提示的还是
        那首已经没了的歌。此时应把名额还回去。

        - 优先按 (platform, song_id) 精确匹配，与 ``add_song`` 的去重键一致；
        - 老占位没有 ``song_id``（升级前登记的），退回按歌名精确匹配；
        - 两者都没有信息时保守返回 True（宁可继续拦，也不误放名额）。
        """
        song_key = str(claim.get("song_id") or "").strip()
        title = str(claim.get("title") or "").strip()
        if song_key:
            key_sql = "(platform=? AND song_id=?)"
            key_args: tuple = (str(claim.get("platform") or ""), song_key)
        elif title:
            key_sql = "title=?"
            key_args = (title,)
        else:
            return True
        sql = (
            f"SELECT 1 FROM songs WHERE group_id=? AND window_key IN (?, ?) "
            f"AND {key_sql} LIMIT 1"
        )
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                sql, (group_id, window_key, MASTER_KEY, *key_args)
            ) as cur:
                return await cur.fetchone() is not None

    async def first_song_of_sharer(
        self, group_id: int, window_key: str, sharer_id: int
    ) -> Optional[Song]:
        """该用户在该窗口**已收录**的第一首歌（按 id 升序）。没有则 None。

        仅用于给提示文案补 {index}（首发占位的那首歌可能在别人名下、也可能
        压根没入榜，此时返回 None，序号显示为 —）。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM songs "
                "WHERE group_id=? AND window_key=? AND sharer_id=? "
                "ORDER BY id ASC LIMIT 1",
                (group_id, window_key, sharer_id),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_song(row) if row else None

    async def windows_with_counts(
        self, group_id: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """列出各窗口及其歌曲数，供手动清理 / 概览下拉选择目标窗口。

        排除总库虚拟窗口，避免它混进普通窗口列表。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if group_id is None:
                async with db.execute(
                    "SELECT window_key, COUNT(*) AS n FROM songs "
                    "WHERE window_key<>? GROUP BY window_key ORDER BY window_key DESC",
                    (MASTER_KEY,),
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    "SELECT window_key, COUNT(*) AS n FROM songs "
                    "WHERE group_id=? AND window_key<>? "
                    "GROUP BY window_key ORDER BY window_key DESC",
                    (group_id, MASTER_KEY),
                ) as cur:
                    rows = await cur.fetchall()
        return [(r["window_key"], int(r["n"])) for r in rows]

    # ------------------------------------------- 群聊记录（上下文 / 记忆素材）

    async def append_chat(
        self,
        group_id: int,
        user_id: int = 0,
        nick: str = "",
        role: str = "user",
        content: str = "",
        reasoning: str = "",
        kind: str = "chat",
        created_at: Optional[float] = None,
    ) -> int:
        """写一行群聊记录，返回行 id；内容为空则跳过（返回 0）。

        ``kind='chat'`` 是与机器人的对话（= 上下文），``kind='listen'`` 是旁听到的
        群消息（只给记忆整理用）。两者混在一张表里是因为它们共用「按群按时间」
        这一个索引；靠 kind 隔开就不会互相污染。
        """
        text = str(content or "")
        if not text.strip():
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO chat_log"
                "(group_id,user_id,nick,kind,role,content,reasoning,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    int(group_id),
                    int(user_id or 0),
                    str(nick or "")[:64],
                    str(kind),
                    str(role),
                    text,
                    str(reasoning or ""),
                    float(created_at if created_at is not None else time.time()),
                ),
            )
            await db.commit()
            return int(cur.lastrowid or 0)

    async def load_chat(
        self,
        group_id: int,
        *,
        kind: str = "chat",
        max_chars: int = 0,
        limit: int = 0,
        since: float = 0.0,
    ) -> list[dict]:
        """按时间正序取上下文消息，返回 ``[{role, content[, reasoning_content]}]``。

        两个约束要注意：

        - ``limit`` 是「最新的 N 行」，取完再反转成正序；
        - ``max_chars`` 超预算时从**最旧**的一对（user + assistant）开始丢——
          按单条丢会把某一轮的问答拆开，模型看到「答非所问」的历史只会更糊涂。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT id, role, content, reasoning FROM chat_log WHERE group_id=? AND kind=?"
            params: list[object] = [int(group_id), str(kind)]
            if since > 0:
                sql += " AND created_at>=?"
                params.append(float(since))
            sql += " ORDER BY id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(int(limit))
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()

        msgs: list[dict] = []
        for row in reversed(rows):
            item = {"role": row["role"], "content": row["content"]}
            if row["reasoning"]:
                item["reasoning_content"] = row["reasoning"]
            msgs.append(item)

        def size(msg: dict) -> int:
            return len(msg.get("content") or "") + len(msg.get("reasoning_content") or "")

        # 首条必须是 user：limit 截断后很容易以 assistant 开头
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        if max_chars and max_chars > 0:
            total = sum(size(m) for m in msgs)
            while len(msgs) > 2 and total > max_chars:
                total -= size(msgs.pop(0))
                if msgs and msgs[0]["role"] == "assistant":
                    total -= size(msgs.pop(0))
        return msgs

    async def latest_chat(
        self, group_id: int, *, max_age: float = 0.0
    ) -> Optional[dict]:
        """取这群**最近一条群友消息**（旁听记录）。

        主动搭话已经改用 :meth:`recent_chat`（它要看「上次搭话之后的一整段」，
        只给一句只能机械附和），这个单条版本留给「只关心最新一句」的场景和测试。

        只认 ``kind='listen'``：那才是群友平时闲聊的原话，格式干净（纯文本）；
        ``kind='chat'`` 里存的是「昵称(QQ号)：提问」，拿去当引子会多一层前缀。

        ``max_age``（秒）非零时只看这段时间内的消息——两小时前的话现在接起来
        很突兀，宁可这轮空过。
        """
        sql = (
            "SELECT id, user_id, nick, content, created_at FROM chat_log "
            "WHERE group_id=? AND kind='listen' AND role='user'"
        )
        params: list[object] = [int(group_id)]
        if max_age > 0:
            sql += " AND created_at>=?"
            params.append(float(time.time()) - float(max_age))
        sql += " ORDER BY id DESC LIMIT 1"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def last_bot_chat(self, group_id: int) -> Optional[dict]:
        """该群机器人**最后一次说话**的位置（``id`` + ``created_at``）。

        给「被动回答时补一段群聊背景」用：从这条之后，群里聊的那些它都没参与过，
        正好是它「不知道」的部分。找不到（这个群它从没说过话）返回 ``None``，
        调用方回退到「最近 N 分钟」。

        主动搭话的回复也是走 ``chat()`` 落库的（``kind='chat'``），所以同样算
        「说过话」——这正合适：它接完话之后群里继续聊的内容，才需要补给它。
        """
        sql = (
            "SELECT id, created_at FROM chat_log "
            "WHERE group_id=? AND kind='chat' AND role='assistant' "
            "ORDER BY id DESC LIMIT 1"
        )
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (int(group_id),)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def recent_chat(
        self,
        group_id: int,
        *,
        after_id: int = 0,
        limit: int = 0,
        max_chars: int = 0,
        max_age: float = 0.0,
    ) -> list[dict]:
        """取「游标之后」的群友消息（旁听记录），按时间**正序**。

        给「主动搭话」当素材：从上次接过的那条之后，群里又聊了些什么。和
        :meth:`latest_chat` 的区别是它只要最新的一条，这里要一整段——模型看到
        「这半小时都聊了什么」才能接过话题，只给最后一句就只能机械附和。

        三个量纲一起用的原因：

        - ``after_id``：`chat_log.id` 自增且唯一，比按时间戳判重更可靠（同一秒可能多条）；
        - ``limit``：群里刷屏时别一次读几百条；
        - ``max_chars``：超了**从最旧的丢**——接话要接的是眼下的话题，
          半小时前的开场白没什么用。

        ``max_age``（秒）非零时只认这段时间内产生的消息，防止接很久以前的话。
        """
        sql = (
            "SELECT id, user_id, nick, content, created_at FROM chat_log "
            "WHERE group_id=? AND kind='listen' AND role='user'"
        )
        params: list[object] = [int(group_id)]
        if after_id > 0:
            sql += " AND id>?"
            params.append(int(after_id))
        if max_age > 0:
            sql += " AND created_at>=?"
            params.append(float(time.time()) - float(max_age))
        sql += " ORDER BY id DESC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = list(await cur.fetchall())

        rows.reverse()  # DESC 取完再翻正序，这样 limit 才是「最新的 N 条」
        if max_chars > 0:
            total = sum(len(r["content"] or "") for r in rows)
            while len(rows) > 1 and total > max_chars:
                total -= len(rows.pop(0)["content"] or "")
        return [dict(r) for r in rows]

    async def pending_digest(
        self, group_id: int, *, limit: int = 300, since: float = 0.0
    ) -> list[dict]:
        """取「还没整理过的群友消息」，按时间正序，只保留最新的 ``limit`` 条。

        只取 ``role='user'``：机器人自己的回复对「这个人是什么样」没有信息量，
        混进去反而会让模型把自己的口气当成成员特征。
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            sql = (
                "SELECT id, user_id, nick, content, created_at FROM chat_log "
                "WHERE group_id=? AND role='user' AND digested=0"
            )
            params: list[object] = [int(group_id)]
            if since > 0:
                sql += " AND created_at>=?"
                params.append(float(since))
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, int(limit)))
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def mark_digested(self, group_id: int, up_to_id: int = 0) -> int:
        """把该群 ``id <= up_to_id`` 的**群友消息**标成已整理。返回影响行数。

        只标 ``role='user'``：整理任务本来就只看群友说的话，把机器人的回复也标掉
        只会让「已整理条数」这个数字失真，排查时还会以为漏了内容。
        """
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE chat_log SET digested=1 "
                "WHERE group_id=? AND id<=? AND digested=0 AND role='user'",
                (int(group_id), int(up_to_id)),
            )
            await db.commit()
            return int(cur.rowcount or 0)

    async def chat_log_stats(self, group_id: Optional[int] = None) -> dict:
        """群聊记录的规模统计（网页状态区用）：条数、未整理数、最早时间。"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            where = "" if group_id is None else " WHERE group_id=?"
            params: tuple = () if group_id is None else (int(group_id),)
            async with db.execute(
                "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, "
                "SUM(CASE WHEN digested=0 AND role='user' THEN 1 ELSE 0 END) AS pending, "
                "SUM(LENGTH(content)) AS chars FROM chat_log" + where,
                params,
            ) as cur:
                row = await cur.fetchone()
        return {
            "rows": int(row["n"] or 0) if row else 0,
            "pending": int(row["pending"] or 0) if row else 0,
            "chars": int(row["chars"] or 0) if row else 0,
            "oldest": float(row["oldest"] or 0) if row else 0.0,
        }

    async def chat_groups(self) -> list[int]:
        """出现过群聊记录的群号（定时整理按这个列表遍历）。"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT DISTINCT group_id FROM chat_log ORDER BY group_id"
            ) as cur:
                rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

    async def prune_chat(self, before_ts: float) -> int:
        """删除早于 ``before_ts`` 的群聊记录（上下文与整理素材一并清）。"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM chat_log WHERE created_at < ?", (before_ts,))
            await db.commit()
        return int(cur.rowcount or 0)

    async def clear_chat(self, group_id: int, kind: Optional[str] = None) -> int:
        """清空某群的群聊记录：``kind`` 为空时连上下文带旁听一起清。"""
        async with aiosqlite.connect(self.db_path) as db:
            if kind is None:
                cur = await db.execute("DELETE FROM chat_log WHERE group_id=?", (int(group_id),))
            else:
                cur = await db.execute(
                    "DELETE FROM chat_log WHERE group_id=? AND kind=?",
                    (int(group_id), str(kind)),
                )
            await db.commit()
        return int(cur.rowcount or 0)

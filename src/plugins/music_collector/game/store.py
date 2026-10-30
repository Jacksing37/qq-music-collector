"""猜歌小游戏的数据层：白名单、登录验证码、登录态。

**为什么不塞进 ``store.Store``**：那 1300 行是收集主流程的库，猜歌只是它旁边的一个
小功能，三张表放进去只会让两边都难读。DB 文件仍然是同一个 ``data/collector.db``，
写入模式也照抄主库（每次操作开一条 aiosqlite 连接，短事务），所以并发上互不影响。

房间状态**不落库**——房间是内存态（见 ``rooms.py``）。进程重启房间就没了，
这是刻意的取舍：房间 60 分钟没人动就该消失，为它设计一套持久化不划算。
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_users (
    qq           INTEGER PRIMARY KEY,
    nick         TEXT    NOT NULL DEFAULT '',
    note         TEXT    NOT NULL DEFAULT '',
    source_group INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL    NOT NULL
);

-- 登录验证码。只存最近若干条，过期的由 prune() 顺手清掉。
-- tries 记「输错了几次」，错满 5 次直接作废，避免 6 位码被穷举。
CREATE TABLE IF NOT EXISTS game_codes (
    code       TEXT    PRIMARY KEY,
    qq         INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    tries      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_game_codes_qq ON game_codes(qq, expires_at);

-- 登录态（网页 localStorage 里那串 token）
CREATE TABLE IF NOT EXISTS game_sessions (
    token      TEXT    PRIMARY KEY,
    qq         INTEGER NOT NULL,
    nick       TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL,
    last_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_sessions_qq ON game_sessions(qq);
"""

#: 验证码错几次作废
MAX_CODE_TRIES = 5


class GameStore:
    """白名单 / 验证码 / 登录态三张表的读写。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ---------------------------------------------------------------- 白名单

    async def add_users(self, rows: Iterable[tuple[int, str, int]], note: str = "") -> int:
        """批量写入白名单，返回**新增**条数（已存在的不覆盖昵称）。

        ``rows`` 每项是 ``(qq, nick, source_group)``。已存在的记录只更新昵称
        （群昵称会变，导入时顺手刷新比留着旧的好），``enabled`` / ``note`` 不动——
        那是管理员手工设过的状态，不该被一次批量导入抹掉。
        """
        now = time.time()
        added = 0
        async with aiosqlite.connect(self.db_path) as db:
            for qq, nick, gid in rows:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO game_users (qq, nick, note, source_group, enabled, created_at)"
                    " VALUES (?,?,?,?,1,?)",
                    (qq, nick or "", note, gid, now),
                )
                if cur.rowcount:
                    added += 1
                elif nick:
                    await db.execute(
                        "UPDATE game_users SET nick=? WHERE qq=? AND nick<>?",
                        (nick, qq, nick),
                    )
            await db.commit()
        return added

    async def list_users(self, keyword: str = "") -> list[dict]:
        sql = "SELECT qq, nick, note, source_group, enabled, created_at FROM game_users"
        args: list[object] = []
        if keyword.strip():
            sql += " WHERE CAST(qq AS TEXT) LIKE ? OR nick LIKE ?"
            like = f"%{keyword.strip()}%"
            args = [like, like]
        sql += " ORDER BY created_at DESC, qq ASC"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_user(self, qq: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT qq, nick, note, source_group, enabled, created_at"
                " FROM game_users WHERE qq=?",
                (qq,),
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def set_enabled(self, qq: int, enabled: bool) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE game_users SET enabled=? WHERE qq=?", (int(enabled), qq)
            )
            await db.commit()
            return bool(cur.rowcount)

    async def delete_users(self, qqs: Iterable[int]) -> int:
        ids = [int(q) for q in qqs]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(f"DELETE FROM game_users WHERE qq IN ({marks})", ids)
            await db.commit()
            return int(cur.rowcount)

    async def count_users(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM game_users WHERE enabled=1") as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- 验证码

    async def new_code(self, qq: int, ttl_seconds: float) -> str:
        """生成一次性验证码。同一 QQ 的旧码全部作废（只认最新那条）。"""
        now = time.time()
        code = f"{secrets.randbelow(1_000_000):06d}"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM game_codes WHERE qq=?", (qq,))
            await db.execute(
                "INSERT INTO game_codes (code, qq, created_at, expires_at, used, tries)"
                " VALUES (?,?,?,?,0,0)",
                (code, qq, now, now + ttl_seconds),
            )
            await db.commit()
        return code

    async def consume_code(self, qq: int, code: str) -> tuple[bool, str]:
        """校验并消费验证码。返回 ``(是否通过, 失败原因)``。

        错满 :data:`MAX_CODE_TRIES` 次直接作废，防止 6 位码被穷举。
        """
        code = (code or "").strip()
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT code, expires_at, used, tries FROM game_codes WHERE qq=?",
                (qq,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False, "没有可用的验证码，请重新获取"
            if row["used"]:
                return False, "验证码已用过，请重新获取"
            if row["expires_at"] < now:
                return False, "验证码已过期，请重新获取"
            if row["tries"] >= MAX_CODE_TRIES:
                await db.execute("DELETE FROM game_codes WHERE qq=?", (qq,))
                await db.commit()
                return False, "错误次数过多，请重新获取"
            if not secrets.compare_digest(str(row["code"]), code):
                await db.execute(
                    "UPDATE game_codes SET tries=tries+1 WHERE qq=?", (qq,)
                )
                await db.commit()
                return False, "验证码不对"
            await db.execute("UPDATE game_codes SET used=1 WHERE qq=?", (qq,))
            await db.commit()
        return True, ""

    # ---------------------------------------------------------------- 登录态

    async def new_session(self, qq: int, nick: str, days: float) -> str:
        now = time.time()
        token = secrets.token_urlsafe(32)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO game_sessions (token, qq, nick, created_at, last_at)"
                " VALUES (?,?,?,?,?)",
                (token, qq, nick or "", now, now),
            )
            await db.commit()
        return token

    async def get_session(self, token: str, days: float) -> Optional[dict]:
        """取登录态并顺手续期；超过 ``days`` 天没用过视为失效。"""
        if not token:
            return None
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT token, qq, nick, created_at, last_at FROM game_sessions WHERE token=?",
                (token,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            if now - float(row["last_at"]) > days * 86400:
                await db.execute("DELETE FROM game_sessions WHERE token=?", (token,))
                await db.commit()
                return None
            await db.execute(
                "UPDATE game_sessions SET last_at=? WHERE token=?", (now, token)
            )
            await db.commit()
        return dict(row)

    async def drop_session(self, token: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM game_sessions WHERE token=?", (token,))
            await db.commit()

    async def prune(self) -> None:
        """清掉过期验证码与过期登录态；由房间巡检顺带调用。"""
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM game_codes WHERE expires_at < ?", (now - 86400,))
            await db.commit()

    # ------------------------------------------------------- 只读：收集窗口

    async def window_stats(self) -> list[dict]:
        """列出各群各窗口的曲目概况，供房主「选题库」下拉用。

        直接读 ``songs`` 表（只读，不改动）。总库那条虚拟窗口要排除掉——
        它跨窗口混在一起，没有「这一期」的语义。
        """
        from ..store import MASTER_KEY

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT group_id, window_key, COUNT(*) AS n,"
                " MIN(created_at) AS first_at, MAX(created_at) AS last_at,"
                " COUNT(DISTINCT sharer_id) AS sharers"
                " FROM songs WHERE window_key<>?"
                " GROUP BY group_id, window_key"
                " ORDER BY last_at DESC",
                (MASTER_KEY,),
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

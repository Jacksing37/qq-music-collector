"""运行时日志缓冲：把最近若干条日志留在内存里，供 WebUI「日志」页查看。

**为什么不用现成的日志文件 / journalctl**

- NoneBot 用 loguru 把日志输出到 stdout，进程里没有可读的日志文件；
  服务器上由 systemd 收进 journald，进程内读不到（Docker 里更不存在）。
- 内存缓冲跨平台且行为可预期：只含本次进程启动后的日志，不混入历史噪声。

**跨重启留存（2026-11-06）**

把 ``logs.file`` 配成路径（线上是 ``logs``）后会额外用 loguru 轮转落盘一份。
以前这份文件只给人看，网页端重启后依旧是空的——「日志」页只剩本次启动的记录。
现在 ``install()`` 启动时会把该文件的**尾部回灌**进内存缓冲（只读、不重写），
所以重启后打开网页仍能看到上一次运行在干什么。回灌只在「换了落盘路径」时读一次，
热更新重复调用 ``install()`` 不会读第二遍（否则会重复）。

**为什么挂 loguru sink 而不是 stdlib logging.Handler**

插件里大量日志走 ``nonebot.log.logger``（loguru 对象），stdlib 的 handler 收不到；
而 nonebot 已经把 stdlib logging 桥接进了 loguru。所以挂在 loguru 上能一次拿到两边。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .config import ROOT_DIR

try:  # loguru 是 nonebot 的硬依赖，离线脚本里缺了也只降级、不炸
    from loguru import logger as _loguru
except Exception:  # pragma: no cover - 仅极端环境
    _loguru = None

#: 默认缓冲条数
DEFAULT_LINES = 2000
#: 默认记录的最低等级（与 bot 默认的 LOG_LEVEL 一致）
DEFAULT_LEVEL = "INFO"

#: 本插件自己的 logger 名（插件包名）。服务器上日志的大头是 OneBot 适配器
#: （logger=nonebot）与 uvicorn 的访问日志，「只看机器人」就是按这个名字过滤。
BOT_LOGGER = "music_collector"

#: 等级名 -> 数值。顺序即严重程度，供前端下拉与后端过滤共用。
LEVEL_ORDER: dict[str, int] = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
#: 前端筛选下拉用的等级列表（由轻到重）
LEVELS: tuple[str, ...] = tuple(sorted(LEVEL_ORDER, key=lambda k: LEVEL_ORDER[k]))

#: 落盘轮转阈值与保留份数（写死，避免配置项过多）
_FILE_ROTATION = "5 MB"
_FILE_RETENTION = 3

#: 回灌时读文件尾部多少字节。8MB 够覆盖 5MB 轮转阈值，再多也只是白读
_HISTORY_TAIL_BYTES = 8 * 1024 * 1024
#: 落盘行格式，必须与 :func:`install` 里 loguru 的 ``format=`` 一字不差
_HISTORY_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([A-Z]+)\] (\S+) \| (.*)$"
)


class LogBuffer:
    """定长环形缓冲，线程安全。

    ``deque(maxlen=N)`` 满了自动丢最旧的，所以内存占用有硬上限。
    """

    def __init__(self, lines: int = DEFAULT_LINES, level: str = DEFAULT_LEVEL) -> None:
        self._lock = threading.Lock()
        self._buf: deque[dict[str, Any]] = deque(maxlen=_norm_lines(lines))
        self._level = _norm_level(level)
        self._levelno = LEVEL_ORDER[self._level]
        self._started = time.time()
        self._total = 0
        #: 从落盘文件回灌进来的条数（重启后仍能看到的那部分）
        self._restored = 0

    # ------------------------------------------------------------ 写入

    def push(
        self,
        level: str,
        name: str,
        message: str,
        exc: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        """记一条。等级低于阈值时直接丢弃（不占缓冲）。"""
        lv = str(level or "INFO").upper()
        no = LEVEL_ORDER.get(lv, LEVEL_ORDER["INFO"])
        if no < self._levelno:
            return
        when = float(ts) if ts is not None else time.time()
        item = {
            "ts": when,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)),
            "level": lv,
            "levelno": no,
            "name": str(name or ""),
            "message": str(message or ""),
            "exc": exc or None,
        }
        with self._lock:
            self._buf.append(item)
            self._total += 1

    def sink(self, message: Any) -> None:
        """loguru sink：从 loguru 的 Message 里取字段存下来。

        绝不能让异常冒出去——loguru 会把它打到 stderr，反而污染日志。
        """
        try:
            record = message.record
            exc = None
            if record["exception"]:
                exc = "".join(traceback.format_exception(*record["exception"]))
            self.push(
                level=str(record["level"].name),
                name=str(record["name"] or ""),
                message=str(record["message"]),
                exc=exc,
                ts=record["time"].timestamp(),
            )
        except Exception:
            return

    def restore(self, items: list[dict[str, Any]]) -> int:
        """把历史日志塞到缓冲区**最前面**，返回实际塞进去的条数。

        只塞「还装得下」的那部分（``capacity - 当前条数``）：``deque.extendleft``
        在满员时会从**右端**丢数据，也就是把刚启动的日志丢掉——那是我们最想看的。
        以 newest-first 倒序 appendleft，保证最终仍是时间升序。
        """
        if not items:
            return 0
        with self._lock:
            room = (self._buf.maxlen or 0) - len(self._buf)
            keep = items[-room:] if room > 0 else []
            for item in reversed(keep):
                self._buf.appendleft(item)
            self._restored += len(keep)
        return len(keep)

    # ------------------------------------------------------------ 读取

    def snapshot(
        self,
        *,
        level: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
        source: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按等级 / 关键词 / 来源过滤后返回（时间升序）。

        ``limit`` 取**最新**的 N 条（截尾），符合"看最近的日志"的直觉。

        ``source`` 是 logger 名（如 ``music_collector`` / ``nonebot`` / ``uvicorn``）。
        特殊值 ``bot`` 表示"只看本插件的日志"，等价于 ``source=BOT_LOGGER``——
        服务器上 OneBot 适配器与 uvicorn 的访问日志占了大头，只想看脚本干了什么
        时用这个一键过滤。
        """
        with self._lock:
            items = list(self._buf)
        if level:
            lv = str(level).strip().upper()
            if lv in LEVEL_ORDER:
                min_no = LEVEL_ORDER[lv]
                items = [i for i in items if i["levelno"] >= min_no]
        src = (source or "").strip()
        if src:
            want = BOT_LOGGER if src.lower() in ("bot", "music", "self") else src
            items = [i for i in items if i["name"] == want]
        q = (query or "").strip().lower()
        if q:
            items = [
                i
                for i in items
                if q in i["message"].lower()
                or q in i["name"].lower()
                or q in (i["exc"] or "").lower()
            ]
        if limit:
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                items = items[-n:]
        return items

    def sources(self) -> list[dict[str, Any]]:
        """缓冲区里出现过的 logger 名及条数（多的在前，本插件永远排最前）。

        供网页端「来源」下拉框用：用户一眼能看出噪音都来自谁。
        """
        with self._lock:
            items = list(self._buf)
        counter: dict[str, int] = {}
        for i in items:
            key = i["name"] or "(未知)"
            counter[key] = counter.get(key, 0) + 1
        rows = [{"name": k, "count": v} for k, v in counter.items()]
        rows.sort(key=lambda r: (r["name"] != BOT_LOGGER, -r["count"], r["name"]))
        return rows

    def clear(self) -> int:
        """清空缓冲，返回清掉的条数。落盘文件不动（下次重启仍会回灌）。"""
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            self._restored = 0
        return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            size = len(self._buf)
            total = self._total
            restored = self._restored
        return {
            "size": size,
            "capacity": self._buf.maxlen,
            "total": total,
            "restored": restored,
            "level": self._level,
            "started_at": self._started,
            "installed": _sink_id is not None,
            "file": str(_file_path) if _file_path else None,
            "history_from": str(_history_from) if _history_from else None,
            # stdlib→loguru 的桥是否装上：没装的话插件里用 logging.getLogger 的模块
            # （archiver / service / config…）的日志不会出现在这里
            "bridge": stdlib_bridge_installed(),
        }

    # ------------------------------------------------------------ 设置

    @property
    def level(self) -> str:
        return self._level

    @property
    def levelno(self) -> int:
        """当前记录等级对应的数值（回灌时按它过滤历史行）。"""
        return self._levelno

    @property
    def capacity(self) -> int:
        return int(self._buf.maxlen or 0)

    def set_capacity(self, lines: int) -> None:
        n = _norm_lines(lines)
        with self._lock:
            if self._buf.maxlen == n:
                return
            # 换 maxlen 会丢超出部分（deque 语义），正是我们想要的
            self._buf = deque(self._buf, maxlen=n)

    def set_level(self, level: str) -> None:
        self._level = _norm_level(level)
        self._levelno = LEVEL_ORDER[self._level]


def _norm_lines(lines: Any) -> int:
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = DEFAULT_LINES
    # 下限 50：太小会让 WebUI 几乎看不到东西；上限 20 万：防手滑写爆内存
    return max(50, min(200_000, n))


def _norm_level(level: Any) -> str:
    lv = str(level or DEFAULT_LEVEL).strip().upper()
    return lv if lv in LEVEL_ORDER else DEFAULT_LEVEL


# ---------------------------------------------------------------- 模块级单例

buffer = LogBuffer()

_sink_id: Optional[int] = None
_sink_level: Optional[str] = None
_file_id: Optional[int] = None
_file_path: Optional[Path] = None
#: 已经回灌过的落盘路径（同一个路径只读一次，避免热更新时重复塞历史）
_history_from: Optional[Path] = None


def _read_history(path: Path, min_levelno: int, limit: int) -> list[dict[str, Any]]:
    """从落盘文件尾部读出最近的日志行，返回与 :meth:`LogBuffer.snapshot` 同构的条目。

    格式必须与 :func:`install` 里那个 loguru ``format=`` 一致：
    ``2026-11-06 18:00:20 [INFO] music_collector | 正文``。

    以**续行**处理不匹配的行：loguru 把带换行的消息（以及异常 traceback）原样续在后面，
    这些行归给上一条，否则回溯会被拆成一堆「无时间戳」的孤立行。
    只读尾部 :data:`_HISTORY_TAIL_BYTES` 字节，被截断的首行直接丢弃。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _HISTORY_TAIL_BYTES:
                fh.seek(size - _HISTORY_TAIL_BYTES)
                fh.readline()  # 丢掉可能被切掉一半的首行
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _HISTORY_RE.match(line)
        if not m:
            if items:                     # 续行：并入上一条
                items[-1]["message"] += ("\n" + line) if line else "\n"
            continue
        when, lv, name, message = m.groups()
        try:
            ts = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        items.append({
            "ts": ts,
            "time": when,
            "level": lv,
            "levelno": LEVEL_ORDER.get(lv, LEVEL_ORDER["INFO"]),
            "name": name,
            "message": message,
            "exc": None,
        })

    items = [i for i in items if i["levelno"] >= min_levelno]
    return items[-limit:] if limit > 0 else items


def _resolve_path(raw: Any) -> Optional[Path]:
    text = str(raw or "").strip()
    if not text:
        return None
    p = Path(text).expanduser()
    return p if p.is_absolute() else (ROOT_DIR / p)


def _drop(sink_id: Optional[int]) -> None:
    if sink_id is None or _loguru is None:
        return
    try:
        _loguru.remove(sink_id)
    except Exception:
        pass


def install(
    lines: int = DEFAULT_LINES,
    level: str = DEFAULT_LEVEL,
    file: Any = None,
) -> dict[str, Any]:
    """安装 / 更新日志捕获，幂等且可热更新。返回 ``buffer.stats()``。

    等级变化需要重新注册 sink（loguru 的等级在 add 时固定）；条数与文件路径变化
    就地调整即可。重复调用不会装出多个 sink。
    """
    global _sink_id, _sink_level, _file_id, _file_path, _history_from

    buffer.set_capacity(lines)
    buffer.set_level(level)

    if _loguru is not None:
        if _sink_id is None or _sink_level != buffer.level:
            _drop(_sink_id)
            _sink_id = None
            try:
                _sink_id = _loguru.add(
                    buffer.sink, level=buffer.level, format="{message}"
                )
                _sink_level = buffer.level
            except Exception:
                _sink_id = None
                _sink_level = None

        path = _resolve_path(file)
        if path != _file_path:
            _drop(_file_id)
            _file_id = None
            _file_path = path
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _file_id = _loguru.add(
                        str(path),
                        level=buffer.level,
                        rotation=_FILE_ROTATION,
                        retention=_FILE_RETENTION,
                        encoding="utf-8",
                        enqueue=True,
                        backtrace=False,
                        diagnose=False,
                        format="{time:YYYY-MM-DD HH:mm:ss} [{level}] {name} | {message}",
                    )
                except Exception:
                    _file_id = None
    else:  # pragma: no cover - 仅缺 loguru 的极端环境
        _sink_id = None
        _file_path = _resolve_path(file)

    # 回灌历史：只在「落盘路径变了」时读一次。重复 install（改配置热更新）不再读，
    # 否则同一段历史会被塞进缓冲好几遍。
    path_now = _resolve_path(file)
    if path_now is not None and path_now != _history_from:
        got = _read_history(path_now, buffer.levelno, buffer.capacity)
        if got:
            buffer.restore(got)
        _history_from = path_now

    _install_stdlib_bridge()
    return buffer.stats()


# ---------------------------------------------------------------- stdlib 桥接

#: 需要桥接的 stdlib logger 树（插件的模块都叫 music_collector.xxx）
_STDLIB_ROOT_LOGGER = "music_collector"


def _install_stdlib_bridge() -> bool:
    """把插件的 stdlib logger 桥接进 loguru，返回本次是否新装。

    **为什么必须自己装**：nonebot 只给 uvicorn（``drivers/fastapi.py`` 里配
    ``LoguruHandler``）和 apscheduler（插件自己 addHandler）各自接了桥，**没有全局桥**。
    而插件的 archiver / service / config / detector / cache / netease_api 用的都是
    ``logging.getLogger("music_collector.xxx")``，stdlib root 默认等级是 WARNING——
    它们的 INFO 日志既到不了 loguru（也就进不了这个缓冲），也不会打到 stdout。
    结果就是「网页端日志页只看得到协议端和 uvicorn 在刷屏，看不到机器人自己干了什么」。

    桥接后这些日志会经 loguru 走一遍，``nonebot`` 的 ``_log_patcher`` 会把
    ``record["name"]`` 归一成插件名（``music_collector``），所以日志页的
    「只看机器人」筛选能一次盖住全部模块。

    幂等：重复调用不会装出第二个 handler 导致日志翻倍。
    """
    if _loguru is None:  # pragma: no cover - 仅缺 loguru 的极端环境
        return False
    try:
        from nonebot.log import LoguruHandler
    except Exception:  # pragma: no cover - 离线脚本里没有 nonebot
        return False

    lg = logging.getLogger(_STDLIB_ROOT_LOGGER)
    if getattr(lg, "_music_collector_bridged", False):
        return False
    lg.setLevel(logging.DEBUG)   # 子 logger 继承；真正的等级由 loguru 的 sink 决定
    lg.addHandler(LoguruHandler())
    # 别再往 root 传：否则会再走一遍 lastResort / 用户自配的 root handler，同一条日志出两次
    lg.propagate = False
    lg._music_collector_bridged = True  # type: ignore[attr-defined]
    return True


def stdlib_bridge_installed() -> bool:
    """自检用：stdlib→loguru 的桥是否已装上。"""
    return bool(getattr(logging.getLogger(_STDLIB_ROOT_LOGGER), "_music_collector_bridged", False))


def uninstall() -> None:
    """卸载全部 sink（测试收尾用，避免污染同进程后续用例）。"""
    global _sink_id, _sink_level, _file_id, _file_path, _history_from
    _drop(_sink_id)
    _drop(_file_id)
    _sink_id = None
    _sink_level = None
    _file_id = None
    _file_path = None
    _history_from = None


def configure_from(cfg: Any) -> dict[str, Any]:
    """按 ``LogsConfig`` 应用配置。配置缺失时退回默认值。"""
    return install(
        lines=getattr(cfg, "lines", DEFAULT_LINES),
        level=getattr(cfg, "level", DEFAULT_LEVEL),
        file=getattr(cfg, "file", ""),
    )


def snapshot(**kwargs: Any) -> list[dict[str, Any]]:
    """模块级快捷方式，等价于 ``buffer.snapshot(...)``。"""
    return buffer.snapshot(**kwargs)


def stats() -> dict[str, Any]:
    return buffer.stats()


def clear() -> int:
    return buffer.clear()


def sources() -> list[dict[str, Any]]:
    """模块级快捷方式，等价于 ``buffer.sources()``。"""
    return buffer.sources()


__all__ = [
    "BOT_LOGGER",
    "DEFAULT_LEVEL",
    "DEFAULT_LINES",
    "LEVELS",
    "LEVEL_ORDER",
    "LogBuffer",
    "buffer",
    "clear",
    "configure_from",
    "install",
    "snapshot",
    "sources",
    "stats",
    "stdlib_bridge_installed",
    "uninstall",
]

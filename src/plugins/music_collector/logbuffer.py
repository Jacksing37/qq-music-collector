"""运行时日志缓冲：把最近若干条日志留在内存里，供 WebUI「日志」页查看。

**为什么不用现成的日志文件 / journalctl**

- NoneBot 用 loguru 把日志输出到 stdout，进程里没有可读的日志文件；
  服务器上由 systemd 收进 journald，进程内读不到（Docker 里更不存在）。
- 内存缓冲跨平台且行为可预期：只含本次进程启动后的日志，不混入历史噪声。

需要跨重启留存时把 ``logs.file`` 配成路径，会**另外**用 loguru 轮转落盘一份，
内存缓冲照旧（它是 WebUI 的数据源，与文件互不影响）。

**为什么挂 loguru sink 而不是 stdlib logging.Handler**

插件里大量日志走 ``nonebot.log.logger``（loguru 对象），stdlib 的 handler 收不到；
而 nonebot 已经把 stdlib logging 桥接进了 loguru。所以挂在 loguru 上能一次拿到两边。
"""

from __future__ import annotations

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

    # ------------------------------------------------------------ 读取

    def snapshot(
        self,
        *,
        level: Optional[str] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """按等级 / 关键词过滤后返回（时间升序）。

        ``limit`` 取**最新**的 N 条（截尾），符合"看最近的日志"的直觉。
        """
        with self._lock:
            items = list(self._buf)
        if level:
            lv = str(level).strip().upper()
            if lv in LEVEL_ORDER:
                min_no = LEVEL_ORDER[lv]
                items = [i for i in items if i["levelno"] >= min_no]
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

    def clear(self) -> int:
        """清空缓冲，返回清掉的条数。"""
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
        return n

    def stats(self) -> dict[str, Any]:
        with self._lock:
            size = len(self._buf)
            total = self._total
        return {
            "size": size,
            "capacity": self._buf.maxlen,
            "total": total,
            "level": self._level,
            "started_at": self._started,
            "installed": _sink_id is not None,
            "file": str(_file_path) if _file_path else None,
        }

    # ------------------------------------------------------------ 设置

    @property
    def level(self) -> str:
        return self._level

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
    global _sink_id, _sink_level, _file_id, _file_path

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

    return buffer.stats()


def uninstall() -> None:
    """卸载全部 sink（测试收尾用，避免污染同进程后续用例）。"""
    global _sink_id, _sink_level, _file_id, _file_path
    _drop(_sink_id)
    _drop(_file_id)
    _sink_id = None
    _sink_level = None
    _file_id = None
    _file_path = None


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


__all__ = [
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
    "stats",
    "uninstall",
]

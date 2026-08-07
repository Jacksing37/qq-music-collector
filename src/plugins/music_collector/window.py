"""时间窗口计算：把配置里的时间点翻译成"当前是否在收集期"和调度参数。

支持三种模式：
- weekly  每周循环，时间点格式 ``MON 20:00``
- daily   每日循环，时间点格式 ``23:00``
- once    单次区间，时间点格式 ``2026-08-10 00:00``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import WindowConfig

# 星期缩写 -> Python weekday()（周一=0）
_DOW_TO_INDEX: dict[str, int] = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}
_INDEX_TO_DOW = {v: k for k, v in _DOW_TO_INDEX.items()}
_INDEX_TO_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_INDEX_TO_CRON = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# 中文星期也接受，方便群里直接打命令
_CN_DOW: dict[str, int] = {
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
    "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4,
    "星期六": 5, "星期日": 6, "星期天": 6, "周天": 6,
}

_WEEKLY_RE = re.compile(r"^\s*([A-Za-z\u4e00-\u9fa5]+)\s+(\d{1,2}):(\d{2})\s*$")
_DAILY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_ONCE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[\sT]+(\d{1,2}):(\d{2})\s*$")


class WindowParseError(ValueError):
    """时间点格式非法。"""


@dataclass(frozen=True)
class WeeklyPoint:
    dow: int
    hour: int
    minute: int

    def label(self) -> str:
        return f"{_INDEX_TO_CN[self.dow]} {self.hour:02d}:{self.minute:02d}"

    def cron_kwargs(self) -> dict:
        return {
            "day_of_week": _INDEX_TO_CRON[self.dow],
            "hour": self.hour,
            "minute": self.minute,
        }


@dataclass(frozen=True)
class DailyPoint:
    hour: int
    minute: int

    def label(self) -> str:
        return f"每天 {self.hour:02d}:{self.minute:02d}"

    def cron_kwargs(self) -> dict:
        return {"hour": self.hour, "minute": self.minute}


def parse_weekly(text: str) -> WeeklyPoint:
    m = _WEEKLY_RE.match(text)
    if not m:
        raise WindowParseError(f"每周时间点格式应为 `MON 20:00` 或 `周一 20:00`，收到: {text!r}")
    dow_raw, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    key = dow_raw.upper()
    if key in _DOW_TO_INDEX:
        dow = _DOW_TO_INDEX[key]
    elif dow_raw in _CN_DOW:
        dow = _CN_DOW[dow_raw]
    else:
        raise WindowParseError(f"无法识别的星期: {dow_raw!r}")
    _validate_hm(hh, mm, text)
    return WeeklyPoint(dow, hh, mm)


def parse_daily(text: str) -> DailyPoint:
    m = _DAILY_RE.match(text)
    if not m:
        raise WindowParseError(f"每日时间点格式应为 `23:00`，收到: {text!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    _validate_hm(hh, mm, text)
    return DailyPoint(hh, mm)


def parse_once(text: str, tz: ZoneInfo) -> datetime:
    m = _ONCE_RE.match(text)
    if not m:
        raise WindowParseError(f"单次时间点格式应为 `2026-08-10 00:00`，收到: {text!r}")
    y, mo, d, hh, mm = (int(g) for g in m.groups())
    _validate_hm(hh, mm, text)
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=tz)
    except ValueError as exc:
        raise WindowParseError(f"非法日期: {text!r} ({exc})") from exc


def _validate_hm(hh: int, mm: int, text: str) -> None:
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise WindowParseError(f"时间超出范围: {text!r}")


def _last_weekly(now: datetime, p: WeeklyPoint) -> datetime:
    days_back = (now.weekday() - p.dow) % 7
    cand = (now - timedelta(days=days_back)).replace(
        hour=p.hour, minute=p.minute, second=0, microsecond=0
    )
    if cand > now:
        cand -= timedelta(days=7)
    return cand


def _next_weekly(after: datetime, p: WeeklyPoint) -> datetime:
    days_fwd = (p.dow - after.weekday()) % 7
    cand = (after + timedelta(days=days_fwd)).replace(
        hour=p.hour, minute=p.minute, second=0, microsecond=0
    )
    if cand <= after:
        cand += timedelta(days=7)
    return cand


def _last_daily(now: datetime, p: DailyPoint) -> datetime:
    cand = now.replace(hour=p.hour, minute=p.minute, second=0, microsecond=0)
    if cand > now:
        cand -= timedelta(days=1)
    return cand


def _next_daily(after: datetime, p: DailyPoint) -> datetime:
    cand = after.replace(hour=p.hour, minute=p.minute, second=0, microsecond=0)
    if cand <= after:
        cand += timedelta(days=1)
    return cand


@dataclass
class WindowState:
    """某一时刻下的窗口状态快照。"""

    mode: str
    key: str                    # 窗口唯一标识，用于数据库分桶
    label: str                  # 人类可读区间，用于歌单命名
    collecting: bool
    start_at: Optional[datetime]
    archive_at: Optional[datetime]

    def describe(self) -> str:
        state = "收集中" if self.collecting else "未开始/已结束"
        fmt = "%Y-%m-%d %H:%M"
        s = self.start_at.strftime(fmt) if self.start_at else "-"
        a = self.archive_at.strftime(fmt) if self.archive_at else "-"
        return f"[{self.mode}] {s} → {a}（{state}）"


class WindowResolver:
    """把 WindowConfig 解析成可用的时间对象，并回答"现在算哪个窗口"。"""

    def __init__(self, cfg: WindowConfig) -> None:
        self.cfg = cfg
        try:
            self.tz = ZoneInfo(cfg.timezone)
        except Exception:  # 时区名写错时回落到东八区，不让机器人起不来
            self.tz = ZoneInfo("Asia/Shanghai")

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ---------- 各模式的时间点 ----------

    def weekly_points(self) -> tuple[WeeklyPoint, WeeklyPoint, WeeklyPoint]:
        w = self.cfg.weekly
        return parse_weekly(w.start), parse_weekly(w.summary), parse_weekly(w.archive)

    def daily_points(self) -> tuple[DailyPoint, DailyPoint, DailyPoint]:
        d = self.cfg.daily
        return parse_daily(d.start), parse_daily(d.summary), parse_daily(d.archive)

    def once_points(self) -> tuple[datetime, datetime, datetime]:
        o = self.cfg.once
        return (
            parse_once(o.start, self.tz),
            parse_once(o.summary, self.tz),
            parse_once(o.archive, self.tz),
        )

    # ---------- 状态计算 ----------

    def resolve(self, now: Optional[datetime] = None) -> WindowState:
        now = now or self.now()
        mode = self.cfg.mode
        if mode == "weekly":
            return self._resolve_weekly(now)
        if mode == "daily":
            return self._resolve_daily(now)
        return self._resolve_once(now)

    def _resolve_weekly(self, now: datetime) -> WindowState:
        p_start, _, p_archive = self.weekly_points()
        start_at = _last_weekly(now, p_start)
        archive_at = _next_weekly(start_at, p_archive)
        return WindowState(
            mode="weekly",
            key=f"W{start_at:%Y%m%d-%H%M}",
            label=f"{start_at:%Y-%m-%d} ~ {archive_at:%Y-%m-%d}",
            collecting=start_at <= now < archive_at,
            start_at=start_at,
            archive_at=archive_at,
        )

    def _resolve_daily(self, now: datetime) -> WindowState:
        p_start, _, p_archive = self.daily_points()
        start_at = _last_daily(now, p_start)
        archive_at = _next_daily(start_at, p_archive)
        return WindowState(
            mode="daily",
            key=f"D{start_at:%Y%m%d-%H%M}",
            label=f"{start_at:%Y-%m-%d}",
            collecting=start_at <= now < archive_at,
            start_at=start_at,
            archive_at=archive_at,
        )

    def _resolve_once(self, now: datetime) -> WindowState:
        start_at, _, archive_at = self.once_points()
        return WindowState(
            mode="once",
            key=f"O{start_at:%Y%m%d-%H%M}",
            label=f"{start_at:%Y-%m-%d} ~ {archive_at:%Y-%m-%d}",
            collecting=start_at <= now < archive_at,
            start_at=start_at,
            archive_at=archive_at,
        )

    # ---------- 调度参数 ----------

    def schedule_specs(self) -> list[tuple[str, str, dict]]:
        """返回 [(任务名, 触发器类型, 触发器参数)]，供 APScheduler 注册。"""
        mode = self.cfg.mode
        specs: list[tuple[str, str, dict]] = []
        if mode == "weekly":
            p_start, p_summary, p_archive = self.weekly_points()
            for name, point in (
                ("start", p_start), ("summary", p_summary), ("archive", p_archive)
            ):
                specs.append((name, "cron", {**point.cron_kwargs(), "timezone": self.tz}))
        elif mode == "daily":
            p_start, p_summary, p_archive = self.daily_points()
            for name, point in (
                ("start", p_start), ("summary", p_summary), ("archive", p_archive)
            ):
                specs.append((name, "cron", {**point.cron_kwargs(), "timezone": self.tz}))
        else:
            d_start, d_summary, d_archive = self.once_points()
            for name, when in (
                ("start", d_start), ("summary", d_summary), ("archive", d_archive)
            ):
                specs.append((name, "date", {"run_date": when}))
        return specs

    def summary_text(self) -> str:
        """给 /music window 命令用的可读描述。"""
        mode = self.cfg.mode
        lines = [f"模式: {mode}    时区: {self.cfg.timezone}"]
        if mode == "weekly":
            s, m, a = self.weekly_points()
            lines += [f"开始收集: {s.label()}", f"汇总播报: {m.label()}", f"归档建歌单: {a.label()}"]
        elif mode == "daily":
            s, m, a = self.daily_points()
            lines += [f"开始收集: {s.label()}", f"汇总播报: {m.label()}", f"归档建歌单: {a.label()}"]
        else:
            s, m, a = self.once_points()
            fmt = "%Y-%m-%d %H:%M"
            lines += [
                f"开始收集: {s:{fmt}}",
                f"汇总播报: {m:{fmt}}",
                f"归档建歌单: {a:{fmt}}",
            ]
        state = self.resolve()
        lines.append(f"当前窗口: {state.label}（{'收集中' if state.collecting else '不在收集期'}）")
        return "\n".join(lines)

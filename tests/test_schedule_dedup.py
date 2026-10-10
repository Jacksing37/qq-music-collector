"""回归测试：调度撞点去重 + 音乐卡片熔断。

对应修复的两个线上问题：
1. 收集结束时榜单被发两次（summary 与 archive 撞在同一时刻）
2. 音乐卡片签名服务 500 导致卡片发不出去
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

# 插件包导入时会 require("nonebot_plugin_apscheduler")，先把 NoneBot 起起来
import nonebot  # noqa: E402

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi")

from music_collector.bot_utils import CardBreaker  # noqa: E402
from music_collector.config import WeeklyWindow, WindowConfig  # noqa: E402
from music_collector.window import WindowResolver  # noqa: E402


def _names(cfg: WindowConfig) -> list[str]:
    return [s[0] for s in WindowResolver(cfg).schedule_specs()]


def test_weekly_summary_collides_with_archive():
    """用户实际配置：summary=end=archive=周六15:20，只应留 start + archive。"""
    cfg = WindowConfig(
        mode="weekly",
        archive_same_as_end=True,
        weekly=WeeklyWindow(
            start="周六 15:05", summary="周六 15:20",
            end="周六 15:20", archive="周六 15:20",
        ),
    )
    assert _names(cfg) == ["start", "archive"]
    assert WindowResolver(cfg).summary_merged() is True


def test_weekly_summary_independent_kept():
    """summary 是独立时刻时必须保留。"""
    cfg = WindowConfig(
        mode="weekly",
        archive_same_as_end=True,
        weekly=WeeklyWindow(
            start="周六 15:05", summary="周六 15:10",
            end="周六 15:20", archive="周六 15:20",
        ),
    )
    assert _names(cfg) == ["start", "summary", "archive"]
    assert WindowResolver(cfg).summary_merged() is False


def test_summary_collides_with_end_when_archive_separate():
    """归档独立设置时，summary 撞 end 同样要去掉。"""
    cfg = WindowConfig(
        mode="weekly",
        archive_same_as_end=False,
        weekly=WeeklyWindow(
            start="周六 15:05", summary="周六 15:20",
            end="周六 15:20", archive="周六 16:00",
        ),
    )
    assert _names(cfg) == ["start", "end", "archive"]


def test_daily_and_once_dedup():
    daily = WindowConfig(mode="daily", archive_same_as_end=True)
    daily.daily.start = "00:00"
    daily.daily.summary = daily.daily.end = daily.daily.archive = "23:30"
    assert _names(daily) == ["start", "archive"]

    once = WindowConfig(mode="once", archive_same_as_end=True)
    once.once.start = "2026-08-10 00:00"
    once.once.summary = once.once.end = once.once.archive = "2026-08-20 22:30"
    assert _names(once) == ["start", "archive"]


def test_card_breaker():
    b = CardBreaker()
    assert b.blocked("qq", 3, 10) is False
    assert b.record_fail("qq", 3, 10) is False
    assert b.record_fail("qq", 3, 10) is False
    assert b.record_fail("qq", 3, 10) is True      # 第 3 次触发熔断
    assert b.blocked("qq", 3, 10) is True
    assert b.blocked("netease", 3, 10) is False    # 平台之间互不影响

    b.record_ok("qq")
    assert b.blocked("qq", 3, 10) is False

    # threshold <= 0 表示关闭熔断
    off = CardBreaker()
    assert off.record_fail("qq", 0, 10) is False
    assert off.blocked("qq", 0, 10) is False


def test_per_group_window_schedules():
    """某群单独改过收集窗口 → 为它注册独立任务，并从全局任务里摘出去。

    否则同一时刻该群会被全局任务和按群任务各播报一次（撞点重复）。
    """
    from music_collector import scheduler as sch
    from music_collector.config import config_manager, group_overrides

    cfg = config_manager.config
    weekly = cfg.window.weekly
    snap = (weekly.start, weekly.summary, weekly.end, weekly.archive)
    weekly.start, weekly.summary = "MON 20:00", "SUN 22:00"
    weekly.end = weekly.archive = "SUN 22:30"

    gid, other = 777000111, 777000222
    try:
        sch.reload_jobs()
        ids = {j.id for j in sch.scheduler.get_jobs()}
        assert "music_collector_start" in ids
        assert not any(i.endswith(f"_g{gid}") for i in ids)
        assert sch.per_group_scheduled() == set()

        group_overrides.set(gid, "window.weekly.start", "TUE 09:00")
        sch.reload_jobs()
        ids = {j.id for j in sch.scheduler.get_jobs()}
        assert f"music_collector_start_g{gid}" in ids, ids
        assert gid in sch.per_group_scheduled()

        # 全局任务必须把该群排除掉，否则重复播报
        assert sch._global_targets([gid, other], "2026-W01") == [other]

        # 取消覆盖 → 按群任务撤掉、标记清空
        group_overrides.clear(gid)
        sch.reload_jobs()
        ids = {j.id for j in sch.scheduler.get_jobs()}
        assert not any(i.endswith(f"_g{gid}") for i in ids), ids
        assert sch.per_group_scheduled() == set()
        assert sch._global_targets([gid, other], "2026-W01") == [gid, other]
    finally:
        group_overrides.clear(gid)
        weekly.start, weekly.summary, weekly.end, weekly.archive = snap
        sch.reload_jobs()
        sch.remove_jobs()
        assert sch.per_group_scheduled() == set()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("\n全部通过")

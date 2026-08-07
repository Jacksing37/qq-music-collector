"""定时调度：开始收集 / 汇总播报 / 归档建歌单。

三个时间点全部来自配置，改配置后调用 reload_jobs() 立即生效。
"""

from __future__ import annotations

from typing import Optional

import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .bot_utils import safe_send_group, send_report
from .service import service
from .window import WindowParseError, parse_daily

JOB_PREFIX = "music_collector_"


def _get_bot() -> Optional[Bot]:
    try:
        bot = nonebot.get_bot()
    except (ValueError, KeyError):
        logger.warning("[music] 当前没有已连接的机器人，跳过定时任务")
        return None
    return bot if isinstance(bot, Bot) else None


async def job_start() -> None:
    """收集窗口开启，向目标群播报"开始收录"提醒。

    注意：开始时本窗口还没有任何收集数据，所以不能靠 groups_in_window 找群，
    否则提醒永远发不出去。优先用配置的 groups / report_groups，都没有再退回到
    所有"曾经收集过"的群。
    """
    bot = _get_bot()
    if bot is None:
        return
    state = service.current_window()
    cfg = service.config
    groups: list[int] = list(cfg.groups) or list(cfg.report_groups)
    if not groups:
        groups = await service.store.all_groups()
    if not groups:
        logger.warning("[music] 没有可广播的群（未配置 groups/report_groups，也无历史收集），跳过开始提醒")
        return
    text = (
        f"🎵 本期音乐收集开始啦（{state.label}）\n"
        f"把网易云 / QQ音乐 / 酷狗 / 酷我 等平台的歌曲分享到群里，就会自动收录并排序。\n"
        f"发送 /music help 查看全部命令。"
    )
    for group_id in groups:
        if service.group_enabled(group_id):
            await safe_send_group(bot, group_id, Message(text))


async def job_summary() -> None:
    """汇总播报：文字列表 + 长图。"""
    bot = _get_bot()
    if bot is None:
        return
    state = service.current_window()
    for group_id in await service.target_groups(state.key):
        if not service.group_enabled(group_id):
            continue
        text, images, songs = await service.build_report(group_id, state)
        if not songs:
            continue
        await send_report(bot, group_id, text, images)


async def job_archive() -> None:
    """归档：建网易云歌单，并把结果播报回群。"""
    bot = _get_bot()
    if bot is None:
        return
    state = service.current_window()
    for group_id in await service.target_groups(state.key):
        if not service.group_enabled(group_id):
            continue
        songs = await service.store.list_songs(group_id, state.key)
        if not songs:
            continue
        # 归档前先出一次最终榜单
        text, images, _ = await service.build_report(group_id, state)
        await send_report(bot, group_id, text, images)

        report = await service.run_archive(group_id, state)
        await safe_send_group(bot, group_id, Message(report.summary()))


async def job_clean() -> None:
    """每日缓存回收。"""
    cfg = service.config.cache
    if not cfg.enabled:
        return
    result = service.clean_cache()
    logger.info(f"[music] 定时缓存清理：{result.text()}")


async def job_prune() -> None:
    """定时清理过期的已收集歌曲（区别于图片缓存清理）。"""
    cfg = service.config.clear
    if not cfg.scheduled_enabled:
        return
    if cfg.keep_days and cfg.keep_days > 0:
        removed = await service.prune_old(cfg.keep_days)
        logger.info(
            f"[music] 定时清理已收集歌曲：删除 {removed} 首（保留 {cfg.keep_days} 天）"
        )
    else:
        logger.info("[music] 定时清理跳过：keep_days <= 0")


_JOB_FUNCS = {
    "start": job_start,
    "summary": job_summary,
    "archive": job_archive,
    "clean": job_clean,
    "prune": job_prune,
}


def remove_jobs() -> None:
    for name in _JOB_FUNCS:
        job_id = JOB_PREFIX + name
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)


def _clean_spec() -> Optional[tuple[str, str, dict]]:
    """缓存清理任务：每天固定时刻跑一次。"""
    cfg = service.config.cache
    if not cfg.enabled:
        return None
    try:
        point = parse_daily(cfg.clean_at)
    except WindowParseError:
        point = parse_daily("04:30")
    return ("clean", "cron", {**point.cron_kwargs(), "timezone": service.resolver.tz})


def _prune_spec() -> Optional[tuple[str, str, dict]]:
    """已收集歌曲定时清理：每天固定时刻跑一次。"""
    cfg = service.config.clear
    if not cfg.scheduled_enabled:
        return None
    try:
        point = parse_daily(cfg.prune_at)
    except WindowParseError:
        point = parse_daily("05:00")
    return ("prune", "cron", {**point.cron_kwargs(), "timezone": service.resolver.tz})


def reload_jobs() -> tuple[bool, str]:
    """按当前配置重建定时任务。返回 (是否成功, 提示)。"""
    try:
        specs = service.resolver.schedule_specs()
    except WindowParseError as exc:
        return False, f"时间配置有误，定时任务未生效：{exc}"

    clean_spec = _clean_spec()
    if clean_spec is not None:
        specs.append(clean_spec)

    prune_spec = _prune_spec()
    if prune_spec is not None:
        specs.append(prune_spec)

    remove_jobs()
    lines: list[str] = []
    for name, trigger, kwargs in specs:
        func = _JOB_FUNCS[name]
        try:
            job = scheduler.add_job(
                func, trigger, id=JOB_PREFIX + name, replace_existing=True,
                misfire_grace_time=300, **kwargs,
            )
            next_run = getattr(job, "next_run_time", None)
            lines.append(f"{name}: {next_run or '待触发'}")
        except Exception as exc:
            logger.warning(f"[music] 注册定时任务 {name} 失败: {exc}")
            lines.append(f"{name}: 注册失败 {exc}")
    logger.info("[music] 定时任务已更新\n" + "\n".join(lines))
    return True, "\n".join(lines)


def next_runs() -> str:
    lines = []
    for name in _JOB_FUNCS:
        job = scheduler.get_job(JOB_PREFIX + name)
        if job is None:
            lines.append(f"{name}: 未注册")
        else:
            lines.append(f"{name}: {getattr(job, 'next_run_time', None) or '待触发'}")
    return "\n".join(lines)

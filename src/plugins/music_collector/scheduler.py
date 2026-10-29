"""定时调度：开始收集 / 汇总播报 / 归档建歌单。

时间点全部来自配置，改配置后调用 reload_jobs() 立即生效。

**按群独立窗口**：某个群如果在「配置 → 群选择器」里单独改过收集时间窗口
（config.group_overrides 里有 ``window.*``），就为它单独注册一套任务
（``music_collector_<name>_g<群号>``），全局那套任务会把该群排除掉，避免
同一份榜单被两套任务各播一次。没有单独设过的群仍旧共用全局时间表，
所以「一个群都没配」时行为与以前完全一致。

进程级任务（缓存清理 / 收集记录清理 / 简介补写）与群无关，仍然只有一份：
它们的调度参数一律读**全局**配置。
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import partial
from typing import Optional

import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from . import banter as banter_mod
from . import style as style_mod
from .bot_utils import safe_send_group, send_report
from .config import group_overrides
from .digest import digest_all
from .service import service
from .window import WindowParseError, parse_daily

JOB_PREFIX = "music_collector_"

#: 榜单播报幂等窗口（秒）。APScheduler 的 misfire 补跑、summary 与 archive 撞点、
#: 手动触发等都可能让同一份榜单在极短时间内被播报两次，这里做最后一道兜底。
REPORT_DEDUP_SECONDS = 120

# (group_id, window_key) -> 上次播报的单调时钟
_last_report: dict[tuple[int, str], float] = {}

#: 已经单独排了班（window 被按群覆盖且注册成功）的群。全局任务要跳过它们。
_PER_GROUP_IDS: set[int] = set()


def per_group_scheduled() -> set[int]:
    """当前有哪些群走的是自己的时间表（全局任务据此让路）。"""
    return set(_PER_GROUP_IDS)


def _groups_with_window_override() -> list[int]:
    return [g for g in group_overrides.group_ids() if group_overrides.has_window_override(g)]



def _should_report(group_id: int, window_key: str) -> bool:
    """同一群、同一窗口在幂等期内只播报一次。"""
    now = time.monotonic()
    key = (group_id, window_key)
    last = _last_report.get(key)
    if last is not None and now - last < REPORT_DEDUP_SECONDS:
        logger.info(
            f"[music] 跳过重复榜单播报 group={group_id} window={window_key}"
            f"（{int(now - last)}s 前刚发过）"
        )
        return False
    _last_report[key] = now
    return True


def reset_report_dedup() -> None:
    """清空播报幂等标记（配置变更 / 手动重载时调用）。"""
    _last_report.clear()


def _get_bot() -> Optional[Bot]:
    try:
        bot = nonebot.get_bot()
    except (ValueError, KeyError):
        logger.warning("[music] 当前没有已连接的机器人，跳过定时任务")
        return None
    return bot if isinstance(bot, Bot) else None


def _global_targets(group_ids: list[int], window_key: str) -> list[int]:
    """全局任务要处理的群：候选里去掉已单独排班的群。"""
    per_group = per_group_scheduled()
    return [g for g in group_ids if g not in per_group]


async def _default_targets() -> list[int]:
    """全局任务默认处理哪些群（不含已单独排班的群）。"""
    wk = service.current_window().key
    return _global_targets(await service.target_groups(wk), wk)


async def job_start(group_ids: Optional[list[int]] = None) -> None:
    """收集窗口开启，向目标群播报"开始收录"提醒。

    注意：开始时本窗口还没有任何收集数据，所以不能靠 groups_in_window 找群，
    否则提醒永远发不出去。优先用配置的 groups / report_groups，都没有再退回到
    所有"曾经收集过"的群。

    ``group_ids`` 非空时只处理这些群（按群独立时间窗口的那套任务用）。
    """
    bot = _get_bot()
    if bot is None:
        return
    cfg = service.config
    if group_ids:
        targets = list(group_ids)
    else:
        groups: list[int] = list(cfg.groups) or list(cfg.report_groups)
        if not groups:
            groups = await service.store.all_groups()
        targets = _global_targets(groups, service.current_window().key)
    if not targets:
        logger.warning("[music] 没有可广播的群（未配置 groups/report_groups，也无历史收集），跳过开始提醒")
        return
    for group_id in targets:
        if not service.group_enabled(group_id):
            continue
        state = service.current_window(group_id)
        text = (
            f"🎵 本期音乐收集开始啦（{state.label}）\n"
            f"把网易云 / QQ音乐 / 酷狗 / 酷我 等平台的歌曲分享到群里，就会自动收录并排序。\n"
            f"发送 /music help 查看全部命令。"
        )
        await safe_send_group(bot, group_id, Message(text))


async def job_summary(group_ids: Optional[list[int]] = None) -> None:
    """汇总播报：文字列表 + 长图。"""
    bot = _get_bot()
    if bot is None:
        return
    if group_ids:
        targets = list(group_ids)
    else:
        targets = await _default_targets()
    for group_id in targets:
        if not service.group_enabled(group_id):
            continue
        state = service.current_window(group_id)
        songs = await service.store.list_songs(group_id, state.key)
        if not songs:
            continue
        if not _should_report(group_id, state.key):
            continue
        text, images, _ = await service.build_report(group_id, state)
        await send_report(bot, group_id, text, images)


async def job_archive(group_ids: Optional[list[int]] = None) -> None:
    """归档：建网易云歌单，并把结果播报回群。"""
    bot = _get_bot()
    if bot is None:
        return
    if group_ids:
        targets = list(group_ids)
    else:
        targets = await _default_targets()
    for group_id in targets:
        if not service.group_enabled(group_id):
            continue
        state = service.current_window(group_id)
        songs = await service.store.list_songs(group_id, state.key)
        if not songs:
            continue
        # 归档前先出一次最终榜单（若 summary 刚播报过则跳过，避免刷屏）
        if _should_report(group_id, state.key):
            text, images, _ = await service.build_report(group_id, state)
            await send_report(bot, group_id, text, images)

        report = await service.run_archive(group_id, state)
        await safe_send_group(
            bot, group_id,
            Message(report.summary(service.cfg(group_id).playlist.sharer_aliases))
        )


async def job_end(group_ids: Optional[list[int]] = None) -> None:
    """结束收集：向目标群播报「收集结束」，并提示稍后归档。

    仅在 ``archive_same_as_end=False`` 时由调度器注册——此时结束收集与归档是
    两个独立时刻，需要一个专门任务在结束收集那一刻发通知并停止收录。
    """
    bot = _get_bot()
    if bot is None:
        return
    if group_ids:
        targets = list(group_ids)
    else:
        targets = await _default_targets()
    for group_id in targets:
        if not service.group_enabled(group_id):
            continue
        state = service.current_window(group_id)
        songs = await service.store.list_songs(group_id, state.key)
        text = (
            f"🛑 本期音乐收集已结束（{state.label}）\n"
            f"共收集 {len(songs)} 首。歌单将在归档时刻自动生成，"
            f"也可由管理员执行 /music archive 立即归档。"
        )
        await safe_send_group(bot, group_id, Message(text))


async def job_clean() -> None:
    """每日缓存回收。"""
    cfg = service.config.cache
    if not cfg.enabled:
        return
    result = service.clean_cache()
    logger.info(f"[music] 定时缓存清理：{result.text()}")


async def job_prune() -> None:
    """定时清理过期的已收集歌曲（区别于图片缓存清理）。"""
    # 群聊记录跟着一起清：它是记忆整理任务的素材，留太久只会让库无限长大。
    # 这一条**不受 clear.scheduled_enabled 控制**——记忆有自己的保留天数。
    mem = service.config.memory
    if mem.keep_days and mem.keep_days > 0:
        before_chat = time.time() - float(mem.keep_days) * 86400
        dropped = await service.store.prune_chat(before_chat)
        if dropped:
            logger.info(
                f"[memory] 清理过期群聊记录 {dropped} 条（保留 {mem.keep_days} 天）"
            )

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


async def job_descfix() -> None:
    """补写此前失败的歌单简介（网易云对改简介有频控，过一阵往往就能写进去）。"""
    if service.config.playlist.desc_retry_minutes <= 0:
        return
    ok, failed = await service.retry_pending_desc()
    if ok or failed:
        logger.info(f"[music] 简介补写：成功 {ok} 个，仍失败 {failed} 个")


async def job_memory_digest() -> None:
    """定时整理群成员记忆：把旁听到的群聊归纳进每个人的档案。

    它做的事和 ``remember`` 工具一样，只是**主动**——不用等模型在对话里想起来，
    隔几小时自己扫一遍。失败只写日志，群里什么都不会看到。
    """
    mem = service.config.memory
    if not mem.enabled or mem.digest_hours <= 0:
        return
    result = await digest_all()
    if not result:
        return
    ok = {g: r for g, r in result.items() if r.get("status") == "ok"}
    bad = {g: r for g, r in result.items() if r.get("status") == "error"}
    if ok:
        parts = [
            f"群{g}:{r.get('facts', 0)}条/{r.get('people', 0)}人" for g, r in ok.items()
        ]
        logger.info("[memory] 定时整理完成 " + "；".join(parts))
    if bad:
        detail = "；".join(f"群{g}:{r.get('why', '')}" for g, r in bad.items())
        logger.warning(f"[memory] 定时整理有 {len(bad)} 个群失败：{detail}")


async def job_style() -> None:
    """定时学「这个群怎么说话」，把结果写成风格卡。

    和 ``job_memory_digest`` 是**两件事**：那个归纳「谁是谁」（事实），这个归纳
    「怎么说话」（语体 + 原话样本）。合成一个任务的话，一份 JSON 要同时承载两种
    粒度，两边的质量都会掉。失败只写日志，群里什么都看不到。
    """
    mem = service.config.memory
    if not (mem.enabled and mem.style_enabled) or mem.style_hours <= 0:
        return
    result = await style_mod.distill_all()
    if not result:
        return
    ok = {g: r for g, r in result.items() if r.get("status") == "ok"}
    bad = {g: r for g, r in result.items() if r.get("status") == "error"}
    if ok:
        parts = [f"群{g}:{r.get('chars', 0)}字" for g, r in ok.items()]
        logger.info("[style] 定时学习完成 " + "；".join(parts))
    if bad:
        detail = "；".join(f"群{g}:{r.get('why', '')}" for g, r in bad.items())
        logger.warning(f"[style] 定时学习有 {len(bad)} 个群失败：{detail}")


async def job_banter() -> None:
    """主动搭话：非勿扰时段里自己冒个泡，接住群里最近的一段对话。

    和别的任务最大的不同是**它是自己排自己的**：跑完（不管成没成）都要重新
    挑一个随机时间点，否则只会响一次。``date`` 触发器 + 自我重排，比固定
    ``interval`` 更贴近「在 30~60 分钟之间随机取一个点」这个要求。
    """
    try:
        result = await banter_mod.run_banter()
        if result.get("sent"):
            logger.info(
                f"[banter] 本轮发了 {result['sent']} 条"
                f"（跳过 {result.get('skipped', 0)} 个群，"
                f"其中判官拦下 {result.get('declined', 0)} 个）"
            )
        elif result.get("declined"):
            # 和「没素材」区分开：看日志时能立刻知道它是主动闭嘴，不是坏了
            logger.info(f"[banter] 本轮判官认为都不该插嘴（{result['declined']} 个群）沉默")
        elif result.get("status") not in ("ok", "quiet"):
            logger.info(f"[banter] 本轮没发（{result.get('status')}）")
    except Exception as exc:  # noqa: BLE001 — 冒泡失败绝不能影响别的任务
        logger.warning(f"[banter] 本轮异常: {exc}")
    finally:
        # 放 finally 里：就算上面抛了异常，节奏也不能断（断了就再也不说话了）
        _schedule_banter_next()


def _schedule_banter_next() -> None:
    """排下一次主动搭话。关掉开关时什么都不做（已注册的那次会自己跑空）。"""
    cfg = service.config.llm
    if not cfg.banter:
        return
    try:
        now = datetime.now(service.resolver.tz)
        when = banter_mod.next_run_at(cfg, now)
        # 某个群刚被 @ 过、计时重算过：就排到它解禁那一刻，别提前空跑一轮
        # （提前跑也只会因为冷却被跳过，日志里白白多一句「跳过」）
        gate = banter_mod.next_gate_at(now)
        if gate is not None and gate > when:
            when = gate
        scheduler.add_job(
            job_banter, "date", run_date=when,
            id=JOB_PREFIX + "banter", replace_existing=True, misfire_grace_time=300,
        )
        logger.info(f"[banter] 下次主动搭话 {when:%m-%d %H:%M}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[banter] 排下一次失败（本轮节奏中断）: {exc}")


def reschedule_banter() -> None:
    """把主动搭话的计时重新排一次（被动回复完之后调，见 ``banter.note_reply``）。"""
    _schedule_banter_next()


_JOB_FUNCS = {
    "start": job_start,
    "summary": job_summary,
    "end": job_end,
    "archive": job_archive,
    "clean": job_clean,
    "prune": job_prune,
    "descfix": job_descfix,
    "memory_digest": job_memory_digest,
    "style": job_style,
    "banter": job_banter,
}


def remove_jobs() -> None:
    """摘掉本插件注册过的**所有**定时任务（含按群独立的那套）。"""
    try:
        jobs = list(scheduler.get_jobs())
    except Exception:  # noqa: BLE001 — scheduler 未就绪时静默跳过
        jobs = []
    for job in jobs:
        jid = getattr(job, "id", "") or ""
        if jid.startswith(JOB_PREFIX):
            try:
                scheduler.remove_job(jid)
            except Exception:  # noqa: BLE001
                pass
    # 兜底：老版本可能用名字登记，get_jobs 拿不到时也要清掉
    for name in _JOB_FUNCS:
        job_id = JOB_PREFIX + name
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001
            pass
    # 按群任务一起消失了，标记也要清掉，否则「哪些群走自己的表」会留着幽灵群号
    _PER_GROUP_IDS.clear()


def _clean_spec() -> Optional[tuple[str, str, dict]]:
    """缓存清理任务：每天固定时刻跑一次（进程级，读全局配置）。"""
    cfg = service.config.cache
    if not cfg.enabled:
        return None
    try:
        point = parse_daily(cfg.clean_at)
    except WindowParseError:
        point = parse_daily("04:30")
    return ("clean", "cron", {**point.cron_kwargs(), "timezone": service.resolver.tz})


def _prune_spec() -> Optional[tuple[str, str, dict]]:
    """已收集歌曲定时清理：每天固定时刻跑一次（进程级，读全局配置）。"""
    cfg = service.config.clear
    if not cfg.scheduled_enabled:
        return None
    try:
        point = parse_daily(cfg.prune_at)
    except WindowParseError:
        point = parse_daily("05:00")
    return ("prune", "cron", {**point.cron_kwargs(), "timezone": service.resolver.tz})


def _descfix_spec() -> Optional[tuple[str, str, dict]]:
    """简介补写：按分钟间隔轮询待补写队列（间隔取**全局**配置）。"""
    minutes = service.config.playlist.desc_retry_minutes
    if minutes <= 0:
        return None
    return ("descfix", "interval", {"minutes": minutes})


def _memory_spec() -> Optional[tuple[str, str, dict]]:
    """群记忆整理：进程级任务，按全局配置的间隔跑（不看单个群的时间窗口）。"""
    mem = service.config.memory
    if not mem.enabled or mem.digest_hours <= 0:
        return None
    return ("memory_digest", "interval", {"hours": float(mem.digest_hours)})


def _style_spec() -> Optional[tuple[str, str, dict]]:
    """风格学习：进程级任务，按全局配置的间隔跑。"""
    mem = service.config.memory
    if not (mem.enabled and mem.style_enabled) or mem.style_hours <= 0:
        return None
    return ("style", "interval", {"hours": float(mem.style_hours)})


def _banter_spec() -> Optional[tuple[str, str, dict]]:
    """主动搭话：只排**第一次**，之后由任务自己在每次跑完时重排。

    用 ``date`` 而不是 ``interval``：APScheduler 的 interval 是等距的，做不出
    「每次都在 30~60 分钟里重新抽一个数」；顺带 ``next_runs()`` 里也能直接看到
    下一次的具体时刻，排查「怎么没动静」时很有用。
    """
    cfg = service.config.llm
    if not cfg.banter or not cfg.enabled:
        return None
    when = banter_mod.next_run_at(cfg, datetime.now(service.resolver.tz))
    return ("banter", "date", {"run_date": when})


def _register(name: str, trigger: str, kwargs: dict, group_id: Optional[int] = None):
    """注册一个任务；``group_id`` 非空时任务只处理该群。"""
    func = _JOB_FUNCS[name]
    if group_id is not None:
        func = partial(func, [group_id])
    job_id = JOB_PREFIX + name + (f"_g{group_id}" if group_id is not None else "")
    return scheduler.add_job(
        func, trigger, id=job_id, replace_existing=True,
        misfire_grace_time=300, **kwargs,
    )


def reload_jobs() -> tuple[bool, str]:
    """按当前配置重建定时任务。返回 (是否成功, 提示)。

    两套任务：
    - **全局**：一套，处理所有没有单独设过窗口的群（与老版本行为一致）；
    - **按群**：某个群单独改过收集时间窗口时，为它注册 ``music_collector_<名>_g<群号>``。
    """
    try:
        specs = list(service.resolver.schedule_specs())
    except WindowParseError as exc:
        return False, f"时间配置有误，定时任务未生效：{exc}"

    for extra in (
        _clean_spec(), _prune_spec(), _descfix_spec(), _memory_spec(),
        _style_spec(), _banter_spec(),
    ):
        if extra is not None:
            specs.append(extra)

    # 逐个群解析它自己的时间窗口；解析失败的群沿用全局时间表（不静默丢任务）
    per_group: list[tuple[int, list[tuple[str, str, dict]]]] = []
    for gid in _groups_with_window_override():
        try:
            per_group.append((gid, list(service.resolver_for(gid).schedule_specs())))
        except WindowParseError as exc:
            logger.warning(f"[music] 群{gid} 的窗口配置有误，改用全局时间表: {exc}")

    remove_jobs()
    reset_report_dedup()

    lines: list[str] = []
    for name, trigger, kwargs in specs:
        try:
            job = _register(name, trigger, kwargs)
            lines.append(f"{name}: {getattr(job, 'next_run_time', None) or '待触发'}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] 注册定时任务 {name} 失败: {exc}")
            lines.append(f"{name}: 注册失败 {exc}")

    for gid, gspecs in per_group:
        failed = None
        added_ids: list[str] = []
        for name, trigger, kwargs in gspecs:
            try:
                job = _register(name, trigger, kwargs, group_id=gid)
                added_ids.append(getattr(job, "id", "") or "")
                lines.append(
                    f"[群{gid}] {name}: {getattr(job, 'next_run_time', None) or '待触发'}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[music] 注册群{gid} 定时任务 {name} 失败: {exc}")
                lines.append(f"[群{gid}] {name}: 注册失败 {exc}")
                failed = exc
        # 只有整套都注册成功才把该群从全局任务里摘出去，否则会两边都不发；
        # 半套注册则整组撤掉（留着会和全局那套重复播报同一时刻的榜单）
        if failed is None:
            _PER_GROUP_IDS.add(gid)
        else:
            for jid in added_ids:
                if not jid:
                    continue
                try:
                    scheduler.remove_job(jid)
                except Exception:  # noqa: BLE001
                    pass

    logger.info("[music] 定时任务已更新\n" + "\n".join(lines))
    return True, "\n".join(lines)


def next_run_of(name: str):
    """某个任务的下次触发时刻；没注册或读不到就返回 ``None``。

    单独抽出来是给网页端用的：主动搭话用 ``date`` 触发器自我重排，界面上
    能直接看到「下次 05:31」，排查「开了怎么没动静」时省一大圈日志。
    """
    try:
        job = scheduler.get_job(JOB_PREFIX + name)
    except Exception:  # noqa: BLE001
        return None
    return getattr(job, "next_run_time", None) if job else None


def next_runs() -> str:
    """列出所有已注册任务的下次触发时间（含按群那套）。"""
    seen: dict[str, object] = {}
    try:
        for job in scheduler.get_jobs():
            jid = getattr(job, "id", "") or ""
            if jid.startswith(JOB_PREFIX):
                seen[jid[len(JOB_PREFIX):]] = getattr(job, "next_run_time", None)
    except Exception:  # noqa: BLE001
        pass
    lines = []
    for name in _JOB_FUNCS:
        if name in seen:
            lines.append(f"{name}: {seen.pop(name) or '待触发'}")
        else:
            lines.append(f"{name}: 未注册")
    for name in sorted(seen):
        lines.append(f"{name}: {seen[name] or '待触发'}")
    return "\n".join(lines)

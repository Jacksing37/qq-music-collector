"""群内管理命令：/music <子命令>"""

from __future__ import annotations

from typing import Optional

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GROUP_ADMIN,
    GROUP_OWNER,
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from . import detector
from .bot_utils import card_breaker, send_report, split_text
from .config import config_manager
from .naming import build_context, render_template, unknown_placeholders, resolve_alias
from .scheduler import next_runs, reload_jobs
from .service import service
from .window import WindowParseError, parse_daily, parse_once, parse_weekly

HELP_TEXT = """音乐收集机器人 · 命令一览（每条命令不带参数发送可看各自详细用法）

【查询】所有人可用
/music list      本期榜单（文字 + 长图）
/music count     已收集数量
/music window    时间窗口配置与下次触发
/music status    运行状态 / 网易云登录状态
/music preview   预览本期歌单名

【时间】管理员
/music mode weekly|daily|once                切换循环模式
/music set time <开始>-<结束>                 一键设：开始-结束（汇总/归档统一=结束）
   例: /music set time 周五 12:00-周五 20:00
/music set start|summary|end|archive <时间点>  单独设置某一时刻
/music set tz <时区>                          如 Asia/Shanghai

【歌单】管理员
/music name <模板>                 命名模板（如 Wk.{seq}线上学习{slash}）
/music title <名称>                仅本次归档生效的歌单名
/music seq <数字>                 期号（可 seq auto on|off 开自动递增）
/music desc <模板>                 简介开头模板
/music sharer list|by_person|by_name|none  简介分享清单样式

【简介样式】管理员
/music emoji text|strip|keep   昵称/歌名表情处理（text=转中文词，推荐）
/music artist on|off           简介是否带歌手名
/music blank on|off            简介条目间空行（by_person 样式生效）

【收集控制】管理员
/music on|off                 开关收集
/music collect auto|on|off    临时覆盖收集状态（测试用，不改时间表）
/music replycard on|off       是否回发音乐卡片（@+文字提示始终发送）
/music archive [歌单名]       立即归档建歌单
/music descfix                补写失败的歌单简介

【音乐卡片】管理员
/music card                        查看卡片状态与用法
/music card native|custom|off      切换卡片模式
       签名服务老是 500 就切 custom（不依赖签名服务）
/music card reset                  解除卡片熔断

【自我介绍】被 @ 时回应
/music intro [on|off|text|cooldown|at|always|skipcmd|skipmusic]
       不带参数 = 查看当前配置与预览；text 后接自定义文案（\\n 换行）

【清理 / 维护】管理员
/music del <序号|范围|all|window>   删除收集（del window 看历史窗口）
/music delauto on|off               归档后自动清空本期
/music prune on|off|days|at         定时清理历史收集
/music clean [天数]                 立即清理图片缓存
/music cookie <MUSIC_U>            设置网易云登录凭证（建议私聊）

【诊断】
/music parse <链接>    诊断链接为何没被识别
/music debug on|off    识别过程详细日志
/music export          导出榜单文本（手动建歌单用）

【时间格式】weekly: MON 20:00 ｜ daily: 23:00 ｜ once: 2026-08-10 00:00
【命名占位符】{seq}{slash}{yy}{m}{d}{week}{window}{count}{sharers}{group}
例：/music name Wk.{seq}线上学习{slash}  →  Wk.86线上学习26/8/7"""

_FIELD_ALIASES = {
    "start": "start", "开始": "start", "起始": "start",
    "summary": "summary", "汇总": "summary", "播报": "summary",
    "end": "end", "结束收集": "end", "截止": "end",
    "archive": "archive", "归档": "archive",
}

_CARD_MODE_CN = {
    "native": "原生卡片（依赖签名服务，可能 500）",
    "custom": "自定义卡片（不走签名服务，最稳）",
    "off": "关闭（只发文字 + 封面）",
}


def _parse_indices(tokens: list[str]) -> list[int]:
    """把 `3` / `1-5` / `2 4 7` 这类参数解析成去重排序后的序号列表。"""
    out: set[int] = set()
    for tok in tokens:
        if "-" in tok:
            parts = tok.split("-", 1)
            try:
                a, b = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            if a <= b:
                out.update(range(a, b + 1))
        elif tok.isdigit():
            out.add(int(tok))
    return sorted(out)

cmd = on_command("music", aliases={"音乐"}, priority=10, block=True)


async def _is_admin(bot: Bot, event: MessageEvent) -> bool:
    if await SUPERUSER(bot, event):
        return True
    if isinstance(event, GroupMessageEvent):
        return await GROUP_ADMIN(bot, event) or await GROUP_OWNER(bot, event)
    return False


def _validate_point(mode: str, value: str) -> None:
    """按当前模式校验时间点格式，非法会抛 WindowParseError。"""
    if mode == "weekly":
        parse_weekly(value)
    elif mode == "daily":
        parse_daily(value)
    else:
        parse_once(value, service.resolver.tz)


def _demo_context(group_id: Optional[int] = None) -> dict[str, str]:
    """用当前窗口造一份占位符样例，供模板预览。"""
    state = service.current_window()
    return build_context(
        group_id=group_id or 0,
        window_label=state.label,
        start_at=state.start_at,
        end_at=state.archive_at,
        count=0,
        total=0,
        seq=service.config.playlist.seq,
        songs=[],
    )


async def _intro_context(nick: str, group_id: Optional[int]) -> dict[str, str]:
    """自我介绍文案占位符表，含 {nick}/{count}/{state}/{playlist}。

    与 __init__._build_intro 共用同一套占位符，保证命令预览与运行时一致。
    """
    cfg = service.config
    state = service.current_window()
    count = 0
    if group_id is not None:
        try:
            count = await service.store.count(group_id, state.key)
        except Exception:
            count = 0
    ctx = build_context(
        group_id=group_id or 0,
        window_label=state.label,
        start_at=state.start_at,
        end_at=state.archive_at,
        count=count,
        total=count,
        seq=cfg.playlist.seq,
        songs=[],
    )
    ctx["nick"] = nick
    ctx["state"] = "收集中" if state.collecting else "未在收集期"
    ctx["playlist"] = render_template(
        cfg.playlist.pending_name or cfg.playlist.name_template, ctx
    )
    return ctx


@cmd.handle()
async def handle_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()
    parts = raw.split()
    action = parts[0].lower() if parts else "help"
    rest = parts[1:]
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else None

    if action in ("help", "帮助", "?"):
        await cmd.finish(Message(HELP_TEXT))

    if action in ("list", "榜单", "列表"):
        await _cmd_list(bot, group_id)
    elif action in ("count", "数量"):
        await _cmd_count(group_id)
    elif action in ("window", "时间", "窗口"):
        await _cmd_window()
    elif action in ("status", "状态"):
        await _cmd_status()
    elif action in ("preview", "预览"):
        await _cmd_preview(group_id)
    elif action in ("mode", "模式"):
        await _cmd_mode(bot, event, rest)
    elif action in ("set", "设置"):
        await _cmd_set(bot, event, rest)
    elif action in ("name", "命名"):
        await _cmd_name(bot, event, rest, group_id)
    elif action in ("title", "本期", "本次"):
        await _cmd_title(bot, event, rest, group_id)
    elif action in ("seq", "期号"):
        await _cmd_seq(bot, event, rest)
    elif action in ("desc", "简介"):
        await _cmd_desc(bot, event, rest, group_id)
    elif action in ("sharer", "清单"):
        await _cmd_sharer(bot, event, rest)
    elif action in ("archive", "归档"):
        await _cmd_archive(bot, event, group_id, rest)
    elif action in ("del", "删除", "清理"):
        await _cmd_delete(bot, event, group_id, rest)
    elif action in ("delauto", "自动清", "清空"):
        await _cmd_delauto(bot, event, rest)
    elif action in ("prune", "定时清", "定期清"):
        await _cmd_prune(bot, event, rest)
    elif action in ("on", "off", "开", "关"):
        await _cmd_toggle(bot, event, action)
    elif action in ("collect", "收集"):
        await _cmd_collect(bot, event, rest)
    elif action in ("replycard", "回卡", "卡片回复"):
        await _cmd_replycard(bot, event, rest)
    elif action in ("emoji", "表情"):
        await _cmd_emoji(bot, event, rest)
    elif action in ("artist", "歌手"):
        await _cmd_artist(bot, event, rest)
    elif action in ("blank", "空行"):
        await _cmd_blank(bot, event, rest)
    elif action in ("cookie", "cookies", "凭证"):
        await _cmd_cookie(bot, event, rest)
    elif action in ("export", "导出"):
        await _cmd_export(bot, event, group_id)
    elif action in ("clean", "清理"):
        await _cmd_clean(bot, event, rest)
    elif action in ("parse", "解析", "诊断"):
        await _cmd_parse(rest)
    elif action in ("debug", "调试"):
        await _cmd_debug(bot, event, rest)
    elif action in ("card", "卡片"):
        await _cmd_card(bot, event, rest)
    elif action in ("intro", "介绍", "自我介绍"):
        await _cmd_intro(bot, event, rest, group_id)
    elif action in ("descfix", "补写", "补简介"):
        await _cmd_descfix(bot, event, group_id)
    else:
        await cmd.finish(Message(f"未知子命令: {action}\n发送 /music help 查看用法"))


# ---------------------------------------------------------------- 查询类


async def _cmd_list(bot: Bot, group_id: Optional[int]) -> None:
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    text, images, songs = await service.build_report(group_id)
    if not songs:
        await cmd.finish(Message(text))
    await send_report(bot, group_id, text, images)


async def _cmd_count(group_id: Optional[int]) -> None:
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    state = service.current_window()
    total = await service.store.count(group_id, state.key)
    status = "收集中" if state.collecting else "不在收集期"
    await cmd.finish(Message(f"当前窗口 {state.label}（{status}）已收集 {total} 首"))


async def _cmd_window() -> None:
    text = service.resolver.summary_text() + "\n\n下次触发：\n" + next_runs()
    await cmd.finish(Message(text))


async def _cmd_status() -> None:
    cfg = service.config
    state = service.current_window()
    profile = await service.netease.login_status()
    if profile and profile.get("userId"):
        account = f"已登录（{profile.get('nickname')}）"
    elif profile:
        account = "已提供凭证，但实时校验失败（可能已过期，建议重新 /music cookie）"
    else:
        account = "未登录"
    groups = "、".join(str(g) for g in cfg.groups) if cfg.groups else "全部群"
    text = (
        f"收集开关: {'开启' if cfg.enabled else '关闭'}\n"
        f"收集模式: {cfg.collect_override}（auto=自动 / on=强制 / off=强制关）\n"
        f"生效群: {groups}\n"
        f"网易云账号: {account}\n"
        f"当前窗口: {state.label}（{'收集中' if state.collecting else '不在收集期'}）\n"
        f"跨平台匹配: {'开启' if cfg.playlist.cross_platform_match else '关闭'}"
        f"（{'严格' if cfg.playlist.strict_match else '宽松'}）\n"
        f"歌单命名: {cfg.playlist.name_template}（期号 {cfg.playlist.seq}）\n"
        f"缓存清理: {'开启' if cfg.cache.enabled else '关闭'}"
        f"（保留 {cfg.cache.keep_days} 天 / 每天 {cfg.cache.clean_at}）\n"
        f"归档后清空本期: {'开启' if cfg.clear.after_archive else '关闭'}\n"
        f"定时清理收集: {'开启' if cfg.clear.scheduled_enabled else '关闭'}"
        f"（保留 {cfg.clear.keep_days} 天 / 每天 {cfg.clear.prune_at}）\n"
        f"音乐卡片: {_CARD_MODE_CN.get(cfg.card.mode, cfg.card.mode)}"
        f" · {card_breaker.status()}\n"
        f"识别调试日志: {'开启' if cfg.debug_detect else '关闭'}"
    )
    await cmd.finish(Message(text))


async def _cmd_preview(group_id: Optional[int]) -> None:
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    name = await service.preview_playlist_name(group_id)
    cfg = service.config.playlist
    extra = f"\n（本次一次性命名: {cfg.pending_name}）" if cfg.pending_name else ""
    await cmd.finish(Message(f"本期歌单名将是：\n{name}{extra}"))


# ---------------------------------------------------------------- 配置类


async def _cmd_mode(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest or rest[0] not in ("weekly", "daily", "once"):
        await cmd.finish(Message("用法: /music mode weekly|daily|once"))
    config_manager.update("window.mode", rest[0])
    ok, info = reload_jobs()
    await cmd.finish(Message(f"模式已切换为 {rest[0]}\n" + (info if ok else info)))


async def _cmd_set(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))

    field_raw = rest[0].lower() if rest else ""

    # `set time 开始-结束`：一键设开始与结束，且汇总/归档统一对齐到结束时刻
    if field_raw in ("time", "时间"):
        value = " ".join(rest[1:]).strip()
        if "-" not in value:
            await cmd.finish(Message(
                "用法: /music set time <开始>-<结束>\n"
                "例: /music set time 周五 12:00-周五 20:00\n"
                "（汇总播报与归档建歌单都会对齐到结束时刻）"
            ))
        start_str, _, end_str = value.partition("-")
        start_str, end_str = start_str.strip(), end_str.strip()
        if not start_str or not end_str:
            await cmd.finish(Message(
                "开始和结束都要有，用 - 连接，例: 周五 12:00-周五 20:00"
            ))
        mode = service.config.window.mode
        try:
            _validate_point(mode, start_str)
            _validate_point(mode, end_str)
        except WindowParseError as exc:
            await cmd.finish(Message(str(exc)))
        config_manager.update(f"window.{mode}.start", start_str)
        config_manager.update(f"window.{mode}.summary", end_str)
        config_manager.update(f"window.{mode}.end", end_str)
        config_manager.update(f"window.{mode}.archive", end_str)
        ok, info = reload_jobs()
        await cmd.finish(Message(
            f"[{mode}] 开始={start_str}，结束(汇总/归档统一)={end_str}\n\n"
            + service.resolver.summary_text() + "\n\n下次触发：\n" + info
        ))

    if len(rest) < 2:
        await cmd.finish(Message("用法: /music set start MON 20:00"))

    field_raw, value = rest[0].lower(), " ".join(rest[1:]).strip()

    if field_raw in ("tz", "timezone", "时区"):
        try:
            config_manager.update("window.timezone", value)
        except Exception as exc:
            await cmd.finish(Message(f"时区设置失败: {exc}"))
        ok, info = reload_jobs()
        await cmd.finish(Message(f"时区已设为 {value}\n{info}"))

    field = _FIELD_ALIASES.get(field_raw)
    if field is None:
        await cmd.finish(Message(f"未知配置项: {field_raw}\n可用: start / summary / archive / tz"))

    mode = service.config.window.mode
    try:
        _validate_point(mode, value)
    except WindowParseError as exc:
        await cmd.finish(Message(str(exc)))

    config_manager.update(f"window.{mode}.{field}", value)
    # 用户要求：设结束收集时，汇总播报也对齐到结束时刻；
    # 归档时刻在 archive_same_as_end 打开时同样跟随结束时刻
    if field == "end":
        config_manager.update(f"window.{mode}.summary", value)
        if service.resolver.same_archive:
            config_manager.update(f"window.{mode}.archive", value)
    ok, info = reload_jobs()
    await cmd.finish(Message(
        f"[{mode}] {field} 已设为 {value}\n\n"
        + service.resolver.summary_text() + "\n\n下次触发：\n" + info
    ))


async def _cmd_name(
    bot: Bot, event: MessageEvent, rest: list[str], group_id: Optional[int]
) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest:
        ctx = _demo_context(group_id)
        await cmd.finish(Message(
            f"当前模板: {service.config.playlist.name_template}\n"
            f"预览: {render_template(service.config.playlist.name_template, ctx)}\n\n"
            "用法: /music name Wk.{seq}线上学习{slash}\n"
            "占位符: {seq} {slash} {dot} {y} {yy} {m} {mm} {d} {dd} {ymd}\n"
            "        {week} {weekday} {start} {end} {window} {count} {sharers} {group}"
        ))
    template = " ".join(rest)
    ctx = _demo_context(group_id)
    unknown = unknown_placeholders(template, ctx)
    if unknown:
        await cmd.finish(Message(
            f"这些占位符不认识: {'、'.join('{' + u + '}' for u in unknown)}\n"
            "发送 /music name 查看可用列表"
        ))
    config_manager.update("playlist.name_template", template)
    await cmd.finish(Message(
        f"歌单命名模板已更新\n预览: {render_template(template, ctx)}"
    ))


async def _cmd_title(
    bot: Bot, event: MessageEvent, rest: list[str], group_id: Optional[int]
) -> None:
    """设置只对下一次归档生效的歌单名。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest:
        current = service.config.playlist.pending_name
        await cmd.finish(Message(
            (f"当前一次性歌单名: {current}\n清除请发 /music title clear"
             if current else "还没有设置一次性歌单名") +
            "\n用法: /music title Wk.86线上学习26/8/7"
        ))
    value = " ".join(rest).strip()
    if value.lower() in ("clear", "clean", "none", "清除", "取消"):
        config_manager.update("playlist.pending_name", "")
        await cmd.finish(Message("已清除一次性歌单名，下次归档回到通用模板"))
    ctx = _demo_context(group_id)
    config_manager.update("playlist.pending_name", value)
    await cmd.finish(Message(
        f"下一次归档将使用歌单名：\n{render_template(value, ctx)}\n"
        "（用完自动失效）"
    ))


async def _cmd_seq(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.playlist
    if not rest:
        await cmd.finish(Message(
            f"当前期号: {cfg.seq}（自动递增: {'开' if cfg.seq_auto_increment else '关'}）\n"
            "用法: /music seq 86        设置期号\n"
            "      /music seq auto on|off  开关自动递增"
        ))
    first = rest[0].lower()
    if first in ("auto", "自动"):
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music seq auto on|off"))
        enabled = rest[1].lower() in ("on", "开")
        config_manager.update("playlist.seq_auto_increment", enabled)
        await cmd.finish(Message(f"期号自动递增已{'开启' if enabled else '关闭'}"))
    if not first.isdigit():
        await cmd.finish(Message("期号必须是数字，例如 /music seq 86"))
    config_manager.update("playlist.seq", int(first))
    await cmd.finish(Message(f"期号已设为 {first}"))


async def _cmd_desc(
    bot: Bot, event: MessageEvent, rest: list[str], group_id: Optional[int]
) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.playlist
    if not rest:
        ctx = _demo_context(group_id)
        await cmd.finish(Message(
            f"当前简介模板:\n{cfg.description_template}\n"
            f"预览: {render_template(cfg.description_template, ctx)}\n"
            f"分享清单样式: {cfg.sharer_style}\n"
            "用法: /music desc Wk.{seq} 线上学习歌单，共 {count} 首"
        ))
    template = " ".join(rest)
    ctx = _demo_context(group_id)
    unknown = unknown_placeholders(template, ctx)
    if unknown:
        await cmd.finish(Message(
            f"这些占位符不认识: {'、'.join('{' + u + '}' for u in unknown)}"
        ))
    config_manager.update("playlist.description_template", template)
    await cmd.finish(Message(
        f"简介模板已更新\n预览: {render_template(template, ctx)}\n"
        "（后面会自动附上「谁分享了什么歌」的清单）"
    ))


async def _cmd_sharer(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    styles = {"list": "逐首列出（含分享者）", "by_person": "按人聚合",
              "by_name": "只列分享者名", "none": "不附清单"}
    if not rest or rest[0] not in styles:
        current = service.config.playlist.sharer_style
        await cmd.finish(Message(
            f"当前样式: {current}（{styles.get(current, '')}）\n"
            "用法: /music sharer list|by_person|by_name|none\n"
            "  list      1. 张三 分享《晴天》- 周杰伦\n"
            "  by_person 张三（3首）：晴天、七里香、稻香\n"
            "  by_name   1.张三 / 2.李四（只列分享者名字）\n"
            "  none      简介只保留开头文案"
        ))
    style = rest[0]
    config_manager.update("playlist.sharer_style", style)
    config_manager.update("playlist.include_sharers", style != "none")
    await cmd.finish(Message(f"简介清单样式已设为 {style}（{styles[style]}）"))


async def _cmd_toggle(bot: Bot, event: MessageEvent, action: str) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    enabled = action in ("on", "开")
    config_manager.update("enabled", enabled)
    await cmd.finish(Message("收集已开启" if enabled else "收集已关闭"))


async def _cmd_collect(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """手动覆盖收集状态，方便测试（不改动时间表）。

    auto=按窗口自动判断  on=强制正在收集  off=强制停止。
    """
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以切换收集模式"))
    if not rest or rest[0].lower() not in (
        "auto", "on", "off", "自动", "开", "关"
    ):
        cur = service.config.collect_override
        await cmd.finish(Message(
            f"当前收集模式: {cur}\n"
            "用法: /music collect auto|on|off\n"
            "  auto  按时间窗口自动判断（默认）\n"
            "  on    强制正在收集（无视时间，方便测试）\n"
            "  off   强制停止收集（无视时间，方便测试）"
        ))
    raw = rest[0].lower()
    value = (
        "auto" if raw in ("auto", "自动")
        else "on" if raw in ("on", "开")
        else "off"
    )
    note = service.set_collect_override(value)
    await cmd.finish(Message(f"收集模式已切换：{note}"))


async def _cmd_replycard(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """识别到音乐后是否回发音乐卡片（@+文字提示始终发送，本项只控制卡片）。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest or rest[0].lower() not in ("on", "off", "开", "关"):
        cur = service.config.reply_card
        await cmd.finish(Message(
            f"当前回发音乐卡片: {'开启' if cur else '关闭'}\n"
            "用法: /music replycard on|off\n"
            "  on    识别到音乐后 @+文字提示，并回发音乐卡片（默认）\n"
            "  off   只发 @+文字提示，不发卡片（签名服务不稳时可关）"
        ))
    enabled = rest[0].lower() in ("on", "开")
    config_manager.update("reply_card", enabled)
    await cmd.finish(Message(
        f"音乐卡片回发已{'开启' if enabled else '关闭'}（@+文字提示不受影响）"
    ))


async def _cmd_card(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """音乐卡片发送策略。

    原生卡片要协议端向签名服务换 ArkShare 结构，那个服务经常 500，
    表现为群里只回文字不回卡片、日志刷「音乐卡片签名失败」。
    切到 custom 就不再依赖签名服务。
    """
    cfg = service.config.card
    sub = rest[0].lower() if rest else ""

    if sub in ("", "status", "状态"):
        await cmd.finish(Message(
            f"音乐卡片模式: {_CARD_MODE_CN.get(cfg.mode, cfg.mode)}\n"
            f"原生失败转自定义: {'开' if cfg.fallback_custom else '关'}\n"
            f"卡片失败转文字: {'开' if cfg.fallback_text else '关'}"
            f"（附封面: {'是' if cfg.fallback_cover else '否'}）\n"
            f"熔断: 连续失败 {cfg.failure_threshold} 次后停 {cfg.cooldown_minutes} 分钟\n"
            f"当前状态: {card_breaker.status()}\n\n"
            "用法:\n"
            "/music card native|custom|off   切换卡片模式\n"
            "/music card text on|off         卡片失败时是否补发文字\n"
            "/music card cover on|off        文字兜底是否附封面\n"
            "/music card retry <次数> [分钟]  熔断阈值与冷却时长\n"
            "/music card reset               立即解除熔断\n"
            "提示: 老是签名失败就用 /music card custom"
        ))

    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))

    if sub in ("native", "原生", "custom", "自定义", "off", "关闭", "关"):
        value = (
            "native" if sub in ("native", "原生")
            else "custom" if sub in ("custom", "自定义")
            else "off"
        )
        config_manager.update("card.mode", value)
        card_breaker.reset()
        await cmd.finish(Message(
            f"音乐卡片模式已设为 {value}：{_CARD_MODE_CN[value]}\n熔断状态已重置"
        ))

    if sub in ("text", "文字"):
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music card text on|off"))
        enabled = rest[1].lower() in ("on", "开")
        config_manager.update("card.fallback_text", enabled)
        await cmd.finish(Message(f"卡片失败时{'会' if enabled else '不会'}补发文字"))

    if sub in ("cover", "封面"):
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music card cover on|off"))
        enabled = rest[1].lower() in ("on", "开")
        config_manager.update("card.fallback_cover", enabled)
        await cmd.finish(Message(f"文字兜底{'会' if enabled else '不会'}附封面图"))

    if sub in ("retry", "熔断"):
        if len(rest) < 2 or not rest[1].isdigit():
            await cmd.finish(Message(
                "用法: /music card retry <连续失败次数> [冷却分钟]\n"
                "例: /music card retry 3 10   （失败 3 次停 10 分钟；0 = 不熔断）"
            ))
        config_manager.update("card.failure_threshold", int(rest[1]))
        minutes = cfg.cooldown_minutes
        if len(rest) >= 3 and rest[2].isdigit():
            minutes = int(rest[2])
            config_manager.update("card.cooldown_minutes", minutes)
        card_breaker.reset()
        await cmd.finish(Message(
            f"熔断已设为：连续失败 {rest[1]} 次后停 {minutes} 分钟"
            if int(rest[1]) > 0 else "熔断已关闭，每首歌都会尝试发卡片"
        ))

    if sub in ("reset", "重置"):
        card_breaker.reset()
        await cmd.finish(Message("卡片熔断状态已重置，下一首会重新尝试发卡片"))

    await cmd.finish(Message(f"未知参数: {sub}\n发送 /music card 查看用法"))


async def _cmd_emoji(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """昵称 / 歌名里的表情处理方式。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.playlist
    if not rest or rest[0].lower() not in (
        "text", "strip", "keep", "文字", "删除", "原样"
    ):
        await cmd.finish(Message(
            f"当前表情处理: {cfg.emoji_style}\n"
            "用法: /music emoji text|strip|keep\n"
            "  text   转成中文词（如 🎵→[音符]），推荐\n"
            "  strip  直接删除表情\n"
            "  keep   原样（带 emoji 的昵称简介可能写不进网易云）"
        ))
    raw = rest[0].lower()
    value = (
        "text" if raw in ("text", "文字")
        else "strip" if raw in ("strip", "删除")
        else "keep"
    )
    config_manager.update("playlist.emoji_style", value)
    await cmd.finish(Message(f"昵称/歌名表情处理已设为 {value}"))


async def _cmd_artist(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """简介清单里是否带歌手名。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.playlist
    if not rest or rest[0].lower() not in ("on", "off", "开", "关"):
        await cmd.finish(Message(
            f"当前简介是否带歌手: {'开' if cfg.desc_show_artist else '关'}\n"
            "用法: /music artist on|off"
        ))
    enabled = rest[0].lower() in ("on", "开")
    config_manager.update("playlist.desc_show_artist", enabled)
    await cmd.finish(Message(f"简介清单{'已带' if enabled else '已不带'}歌手名"))


async def _cmd_blank(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """简介清单条目之间插空行（by_person 样式下按人分段）。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.playlist
    if not rest or rest[0].lower() not in ("on", "off", "开", "关"):
        await cmd.finish(Message(
            f"当前简介条目间空行: {'开' if cfg.desc_blank_line else '关'}\n"
            "用法: /music blank on|off\n"
            "（仅 by_person 样式下生效，按人分段更清晰）"
        ))
    enabled = rest[0].lower() in ("on", "开")
    config_manager.update("playlist.desc_blank_line", enabled)
    await cmd.finish(Message(f"简介条目间空行已{'开启' if enabled else '关闭'}"))


# ---------------------------------------------------------------- 操作类


async def _cmd_archive(
    bot: Bot, event: MessageEvent, group_id: Optional[int], rest: list[str]
) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以手动归档"))
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    override = " ".join(rest).strip()
    tip = f"（本次歌单名: {override}）" if override else ""
    await bot.send(event, Message(f"开始归档{tip}，正在匹配网易云曲库，请稍候…"))
    report = await service.run_archive(group_id, name_override=override)
    for chunk in split_text(report.summary(service.config.playlist.sharer_aliases)):
        await bot.send(event, Message(chunk))


async def _cmd_delete(
    bot: Bot, event: MessageEvent, group_id: Optional[int], rest: list[str]
) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以删除记录"))
    if not rest:
        await cmd.finish(Message(
            "用法: /music del <序号|范围|all|window>\n"
            "  del 3          删除第 3 首\n"
            "  del 1-5        批量删除第 1~5 首\n"
            "  del 2 4 7      批量删除指定序号\n"
            "  del all        清空本期榜单\n"
            "  del window     查看可清理的历史窗口\n"
            "  del window <key>  清空指定窗口"
        ))
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))

    sub = rest[0].lower()
    state = service.current_window()

    if sub == "all":
        n = await service.clear_window(group_id, state.key)
        await cmd.finish(Message(f"已清空本期榜单（{n} 首）"))

    if sub == "window":
        if len(rest) < 2:
            wins = await service.windows_with_counts(group_id)
            if not wins:
                await cmd.finish(Message("当前没有可清理的历史窗口"))
            lines = ["可清理的历史窗口（del window <key> 清空）："]
            for key, n in wins:
                tag = "（本期）" if key == state.key else ""
                lines.append(f"  {key}  {n} 首 {tag}")
            await cmd.finish(Message("\n".join(lines)))
        key = rest[1]
        n = await service.clear_window(group_id, key)
        await cmd.finish(Message(f"已清空窗口 {key}（{n} 首）"))

    # 单个 / 范围 / 多个序号的批量删除
    indices = _parse_indices(rest)
    if not indices:
        await cmd.finish(Message("没看懂要删哪些，用法见 /music del"))
    n = await service.clear_indices(group_id, state.key, indices)
    await cmd.finish(Message(f"已删除 {n} 首"))


async def _cmd_delauto(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """归档（结束收集）后是否自动清空本期。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest or rest[0].lower() not in ("on", "off", "开", "关"):
        cur = service.config.clear.after_archive
        await cmd.finish(Message(
            f"当前: 归档后自动清空本期 = {'开启' if cur else '关闭'}\n"
            "用法: /music delauto on|off"
        ))
    enabled = rest[0].lower() in ("on", "开")
    config_manager.update("clear.after_archive", enabled)
    await cmd.finish(Message(
        f"归档（结束收集）后自动清空本期已{'开启' if enabled else '关闭'}"
    ))


async def _cmd_prune(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    """定时清理历史收集数据。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    cfg = service.config.clear
    if not rest:
        await cmd.finish(Message(
            f"定时清理: {'开启' if cfg.scheduled_enabled else '关闭'}"
            f"（保留 {cfg.keep_days} 天 / 每天 {cfg.prune_at}）\n"
            "用法:\n"
            "  /music prune on|off        开关定时清理\n"
            "  /music prune days <天数>   保留天数\n"
            "  /music prune at <HH:MM>    每天执行时刻"
        ))
    first = rest[0].lower()
    if first in ("on", "off", "开", "关"):
        enabled = first in ("on", "开")
        config_manager.update("clear.scheduled_enabled", enabled)
        if enabled:
            ok, info = reload_jobs()
            await cmd.finish(Message(f"定时清理已开启\n下次执行：\n{info}"))
        await cmd.finish(Message("定时清理已关闭"))
    if first == "days":
        if len(rest) < 2:
            await cmd.finish(Message("用法: /music prune days <天数>，例如 /music prune days 30"))
        try:
            days = float(rest[1])
        except ValueError:
            await cmd.finish(Message("天数必须是数字"))
        if days < 0:
            await cmd.finish(Message("天数不能为负"))
        config_manager.update("clear.keep_days", days)
        await cmd.finish(Message(f"保留天数已设为 {rest[1]} 天"))
    if first == "at":
        if len(rest) < 2:
            await cmd.finish(Message("用法: /music prune at <HH:MM>，例如 /music prune at 05:00"))
        try:
            parse_daily(rest[1])
        except WindowParseError as exc:
            await cmd.finish(Message(str(exc)))
        config_manager.update("clear.prune_at", rest[1])
        ok, info = reload_jobs()
        await cmd.finish(Message(f"定时清理时刻已设为 {rest[1]}\n{info}"))
    await cmd.finish(Message("用法见 /music prune"))


async def _cmd_cookie(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以设置网易云登录凭证"))
    if not rest:
        await cmd.finish(Message(
            "用法：/music cookie <MUSIC_U>\n"
            "请在浏览器登录网易云音乐网页版，打开开发者工具 -> 网络 -> "
            "music.163.com 请求的请求头里找到 Cookie 字段，\n"
            "复制其中 MUSIC_U=xxxxx 的值（只要 xx 部分）发给我。"
        ))
    value = " ".join(rest).strip()
    # 用户可能把 "MUSIC_U=xxx" 整体粘贴过来，做兼容
    if "=" in value:
        value = value.split("=", 1)[1].strip()
    if not value:
        await cmd.finish(Message("MUSIC_U 不能为空"))

    service.netease.set_cookie_string(f"MUSIC_U={value}")
    profile = await service.netease.login_status()
    nickname = profile.get("nickname") if profile else "未知"
    await cmd.finish(Message(f"网易云凭证已保存（{nickname}），现在可以执行 /music archive 建歌单了"))


async def _cmd_export(bot: Bot, event: MessageEvent, group_id: Optional[int]) -> None:
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    state = service.current_window()
    songs = await service.store.list_songs(group_id, state.key)
    if not songs:
        await cmd.finish(Message("当前窗口没有歌曲"))

    lines = [
        f"群歌单 · {state.label}",
        f"共 {len(songs)} 首，可手动在网易云创建歌单后批量搜索添加：",
        "-" * 20,
    ]
    for idx, song in enumerate(songs, start=1):
        artist = song.artists or "未知歌手"
        sharer = resolve_alias(song.sharer_name or "匿名", service.config.playlist.sharer_aliases)
        lines.append(f"{idx}. {song.title} - {artist}（{sharer} 分享）")
    text = "\n".join(lines)
    for chunk in split_text(text):
        await bot.send(event, Message(chunk))
    await bot.send(event, Message("提示：也可用 /music archive 尝试自动建歌单"))


async def _cmd_clean(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以清理缓存"))
    keep_days: Optional[float] = None
    if rest:
        try:
            keep_days = float(rest[0])
        except ValueError:
            await cmd.finish(Message("用法: /music clean [保留天数]，例如 /music clean 0 清空全部"))
    result = service.clean_cache(keep_days)
    await cmd.finish(Message(result.text()))


async def _cmd_parse(rest: list[str]) -> None:
    if not rest:
        await cmd.finish(Message(
            "用法: /music parse <链接>\n把没被识别的分享链接贴进来，我告诉你卡在哪一步"
        ))
    text = " ".join(rest)
    try:
        report = await detector.diagnose(text)
    except Exception as exc:
        await cmd.finish(Message(f"诊断异常: {type(exc).__name__} {exc}"))
    await cmd.finish(Message(report))


async def _cmd_debug(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest or rest[0].lower() not in ("on", "off", "开", "关"):
        await cmd.finish(Message("用法: /music debug on|off"))
    enabled = rest[0].lower() in ("on", "开")
    config_manager.update("debug_detect", enabled)
    detector.set_debug(enabled)
    await cmd.finish(Message(
        f"识别调试日志已{'开启' if enabled else '关闭'}"
        + ("，现在分享一首歌，然后看机器人控制台的 [music/detect] 日志" if enabled else "")
    ))


# ---------------------------------------------------------------- 自我介绍 / 简介补写


async def _cmd_intro(
    bot: Bot, event: MessageEvent, rest: list[str], group_id: Optional[int]
) -> None:
    """被 @ 时的自我介绍：查看 / 开关 / 自定义文案 / 冷却等。"""
    cfg = service.config.intro

    # 无参数：展示当前配置 + 渲染预览（所有人可见）
    if not rest:
        ctx = await _intro_context("你", group_id)
        preview = render_template(cfg.text, ctx)
        await cmd.finish(Message(
            "被 @ 时自我介绍：\n"
            f"  开关: {'开启' if cfg.enabled else '关闭'}\n"
            f"  冷却: {cfg.cooldown} 秒（0=不限频）\n"
            f"  @提问者: {'是' if cfg.at_sender else '否'}\n"
            f"  关收集时也回应: {'是' if cfg.always_reply else '否'}\n"
            f"  遇 /music 命令跳过: {'是' if cfg.skip_commands else '否'}\n"
            f"  遇音乐分享跳过: {'是' if cfg.skip_music else '否'}\n\n"
            f"当前文案：\n{cfg.text}\n\n"
            f"预览效果：\n{preview}\n\n"
            "用法:\n"
            "  /music intro on|off            开关自我介绍\n"
            "  /music intro text <文案>       自定义文案（\\n 表示换行）\n"
            "  /music intro cooldown <秒>     冷却秒数（0=不限）\n"
            "  /music intro at on|off         是否 @ 提问者\n"
            "  /music intro always on|off     关收集时也回应\n"
            "  /music intro skipcmd on|off    遇命令跳过\n"
            "  /music intro skipmusic on|off  遇音乐跳过\n"
            "  /music intro preview           只看渲染预览"
        ))

    sub = rest[0].lower()
    if sub == "preview":
        ctx = await _intro_context("你", group_id)
        await cmd.finish(Message("预览效果：\n" + render_template(cfg.text, ctx)))

    # 其余均为写操作，仅管理员
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改自我介绍设置"))

    if sub in ("on", "off", "开", "关"):
        enabled = sub in ("on", "开")
        config_manager.update("intro.enabled", enabled)
        await cmd.finish(Message(f"被 @ 时的自我介绍已{'开启' if enabled else '关闭'}"))
    if sub == "text":
        if len(rest) < 2:
            await cmd.finish(Message(
                "用法: /music intro text 你好 {nick}，我是…（\\n 表示换行）\n"
                "可用占位符: {nick} {state} {playlist} 以及 {window} {count} {seq} 等"
            ))
        template = " ".join(rest[1:]).replace("\\n", "\n")
        ctx = await _intro_context("你", group_id)
        unknown = unknown_placeholders(template, ctx)
        if unknown:
            await cmd.finish(Message(
                f"这些占位符不认识: {'、'.join('{' + u + '}' for u in unknown)}\n"
                "可用的有：{nick} {state} {playlist} 以及命名占位符 {window} {count} {seq} 等"
            ))
        config_manager.update("intro.text", template)
        await cmd.finish(Message("自我介绍文案已更新\n预览：\n" + render_template(template, ctx)))
    if sub == "cooldown":
        if len(rest) < 2 or not rest[1].lstrip("-").isdigit():
            await cmd.finish(Message("用法: /music intro cooldown <秒数>，0 表示不限"))
        cd = int(rest[1])
        if cd < 0:
            await cmd.finish(Message("冷却秒数不能为负"))
        config_manager.update("intro.cooldown", cd)
        await cmd.finish(Message(f"自我介绍冷却已设为 {cd} 秒" + ("（不限频）" if cd == 0 else "")))
    if sub == "at":
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music intro at on|off"))
        v = rest[1].lower() in ("on", "开")
        config_manager.update("intro.at_sender", v)
        await cmd.finish(Message(f"@ 提问者已{'开启' if v else '关闭'}"))
    if sub == "always":
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music intro always on|off"))
        v = rest[1].lower() in ("on", "开")
        config_manager.update("intro.always_reply", v)
        await cmd.finish(Message(f"关收集时也回应已{'开启' if v else '关闭'}"))
    if sub in ("skipcmd", "skip_command", "skip-cmd"):
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music intro skipcmd on|off"))
        v = rest[1].lower() in ("on", "开")
        config_manager.update("intro.skip_commands", v)
        await cmd.finish(Message(f"遇到 /music 命令时跳过自我介绍已{'开启' if v else '关闭'}"))
    if sub in ("skipmusic", "skip_music", "skip-music"):
        if len(rest) < 2 or rest[1].lower() not in ("on", "off", "开", "关"):
            await cmd.finish(Message("用法: /music intro skipmusic on|off"))
        v = rest[1].lower() in ("on", "开")
        config_manager.update("intro.skip_music", v)
        await cmd.finish(Message(f"遇到音乐分享时跳过自我介绍已{'开启' if v else '关闭'}"))

    await cmd.finish(Message("用法见 /music intro"))


async def _cmd_descfix(
    bot: Bot, event: MessageEvent, group_id: Optional[int]
) -> None:
    """手动补写之前归档失败（频控拦截）的歌单简介。"""
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以补写简介"))
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))

    pending = await service.pending_desc_list(group_id)
    if not pending:
        await cmd.finish(Message("当前没有待补写的歌单简介 ✓"))
    await bot.send(event, Message(f"发现 {len(pending)} 个待补写简介，开始重试…"))
    ok, failed = await service.retry_pending_desc(group_id)
    if failed == 0:
        await cmd.finish(Message(f"简介补写完成，成功 {ok} 个，全部搞定 ✓"))
    await cmd.finish(Message(
        f"简介补写结果：成功 {ok} 个，仍失败 {failed} 个。\n"
        "失败的可能是网易云仍在频控，定时补写任务（desc_retry_minutes）会自动再试，"
        "也可稍后再次 /music descfix。"
    ))

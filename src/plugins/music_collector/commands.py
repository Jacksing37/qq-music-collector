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
from .bot_utils import send_report, split_text
from .config import config_manager
from .naming import build_context, render_template, unknown_placeholders
from .scheduler import next_runs, reload_jobs
from .service import service
from .window import WindowParseError, parse_daily, parse_once, parse_weekly

HELP_TEXT = """音乐收集机器人 · 命令一览
【所有人】
/music list        当前榜单（文字 + 长图）
/music count       已收集数量
/music window      查看时间窗口配置
/music status      运行状态与网易云登录状态
/music preview     预览本期歌单名
【管理员 · 时间】
/music mode weekly|daily|once   切换循环模式
/music set start   <时间点>     设置开始收集时刻
/music set summary <时间点>     设置汇总播报时刻
/music set archive <时间点>     设置归档建歌单时刻
/music set tz      <时区>       如 Asia/Shanghai
【管理员 · 歌单】
/music name <模板>          歌单命名模板
/music title <名称>         只对下一次归档生效的歌单名
/music seq <数字>           设置自增期号（Wk.86 里的 86）
/music desc <模板>          歌单简介开头模板
/music sharer list|by_person|none   简介里分享清单的样式
/music archive [歌单名]     立即归档并建歌单
/music export      导出榜单文本（weapi 不可用时手动建歌单）
【管理员 · 清理收集数据】
/music del <序号|范围|all|window>   删除已收集歌曲
   del 3          删除第 3 首
   del 1-5        批量删除第 1~5 首
   del 2 4 7      批量删除指定序号
   del all        清空本期榜单
   del window     查看可清理的历史窗口
   del window <key>   清空指定窗口
/music delauto on|off   归档（结束收集）后自动清空本期
/music prune on|off|days|at   定时清理历史收集
   prune on|off        开关定时清理
   prune days <天数>   保留天数（默认 30）
   prune at <HH:MM>    每天执行时刻（默认 05:00）
/music clean [天数]        立即清理图片缓存
/music on | off    开关收集
/music cookie <MUSIC_U>    设置网易云登录凭证（建议私聊使用）
/music parse <链接>        诊断某个链接为什么没被识别
/music debug on|off        开关识别过程详细日志
【时间点格式】
weekly: MON 20:00 或 周一 20:00 ／ daily: 23:00 ／ once: 2026-08-10 00:00
【命名占位符】
{seq} 期号  {slash} 26/8/7  {yy}{m}{d} 年月日  {week} 周数
{window} 区间  {count} 首数  {sharers} 参与人数  {group} 群号
例：/music name Wk.{seq}线上学习{slash}  ->  Wk.86线上学习26/8/7"""

_FIELD_ALIASES = {
    "start": "start", "开始": "start", "起始": "start",
    "summary": "summary", "汇总": "summary", "播报": "summary",
    "archive": "archive", "归档": "archive", "结束": "archive",
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
    account = f"已登录（{profile.get('nickname')}）" if profile else "未登录"
    groups = "、".join(str(g) for g in cfg.groups) if cfg.groups else "全部群"
    text = (
        f"收集开关: {'开启' if cfg.enabled else '关闭'}\n"
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
    styles = {"list": "逐首列出（含分享者）", "by_person": "按人聚合", "none": "不附清单"}
    if not rest or rest[0] not in styles:
        current = service.config.playlist.sharer_style
        await cmd.finish(Message(
            f"当前样式: {current}（{styles.get(current, '')}）\n"
            "用法: /music sharer list|by_person|none\n"
            "  list      1. 张三 分享《晴天》- 周杰伦\n"
            "  by_person 张三（3首）：晴天、七里香、稻香\n"
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
    for chunk in split_text(report.summary()):
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
        sharer = song.sharer_name or "匿名"
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

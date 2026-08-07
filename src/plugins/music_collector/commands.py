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

from .bot_utils import send_report, split_text
from .config import config_manager
from .scheduler import next_runs, reload_jobs
from .service import service
from .window import WindowParseError, parse_daily, parse_once, parse_weekly

HELP_TEXT = """音乐收集机器人 · 命令一览
【所有人】
/music list        当前榜单（文字 + 长图）
/music count       已收集数量
/music window      查看时间窗口配置
/music status      运行状态与网易云登录状态
【管理员】
/music mode weekly|daily|once   切换循环模式
/music set start   <时间点>     设置开始收集时刻
/music set summary <时间点>     设置汇总播报时刻
/music set archive <时间点>     设置归档建歌单时刻
/music set tz      <时区>       如 Asia/Shanghai
/music name <模板>              歌单命名模板，可用 {window} {count}
/music archive     立即归档并建歌单
/music del <序号>  从榜单中删除某首
/music on | off    开关收集
/music cookie <MUSIC_U>    设置网易云登录凭证（建议私聊使用）
/music export      导出当前榜单歌单文本（weapi 不可用时手动建歌单）
【时间点格式】
weekly: MON 20:00 或 周一 20:00
daily : 23:00
once  : 2026-08-10 00:00"""

_FIELD_ALIASES = {
    "start": "start", "开始": "start", "起始": "start",
    "summary": "summary", "汇总": "summary", "播报": "summary",
    "archive": "archive", "归档": "archive", "结束": "archive",
}

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


@cmd.handle()
async def handle_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    parts = args.extract_plain_text().strip().split()
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
    elif action in ("mode", "模式"):
        await _cmd_mode(bot, event, rest)
    elif action in ("set", "设置"):
        await _cmd_set(bot, event, rest)
    elif action in ("name", "命名"):
        await _cmd_name(bot, event, rest)
    elif action in ("archive", "归档"):
        await _cmd_archive(bot, event, group_id)
    elif action in ("del", "删除"):
        await _cmd_delete(bot, event, group_id, rest)
    elif action in ("on", "off", "开", "关"):
        await _cmd_toggle(bot, event, action)
    elif action in ("cookie", "cookies", "凭证"):
        await _cmd_cookie(bot, event, rest)
    elif action in ("export", "导出"):
        await _cmd_export(bot, event, group_id)
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
        f"（{'严格' if cfg.playlist.strict_match else '宽松'}）"
    )
    await cmd.finish(Message(text))


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


async def _cmd_name(bot: Bot, event: MessageEvent, rest: list[str]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    if not rest:
        await cmd.finish(Message(
            f"当前模板: {service.config.playlist.name_template}\n"
            "用法: /music name 群歌单 {window}"
        ))
    template = " ".join(rest)
    config_manager.update("playlist.name_template", template)
    state = service.current_window()
    try:
        preview = template.format(window=state.label, group=0, count=0, date=state.label)
    except KeyError as exc:
        await cmd.finish(Message(f"模板里有不支持的占位符: {exc}"))
    await cmd.finish(Message(f"歌单命名模板已更新\n预览: {preview}"))


async def _cmd_toggle(bot: Bot, event: MessageEvent, action: str) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以修改配置"))
    enabled = action in ("on", "开")
    config_manager.update("enabled", enabled)
    await cmd.finish(Message("收集已开启" if enabled else "收集已关闭"))


# ---------------------------------------------------------------- 操作类


async def _cmd_archive(bot: Bot, event: MessageEvent, group_id: Optional[int]) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以手动归档"))
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    await bot.send(event, Message("开始归档，正在匹配网易云曲库，请稍候…"))
    report = await service.run_archive(group_id)
    for chunk in split_text(report.summary()):
        await bot.send(event, Message(chunk))


async def _cmd_delete(
    bot: Bot, event: MessageEvent, group_id: Optional[int], rest: list[str]
) -> None:
    if not await _is_admin(bot, event):
        await cmd.finish(Message("只有管理员可以删除记录"))
    if group_id is None:
        await cmd.finish(Message("该命令请在群里使用"))
    if not rest or not rest[0].isdigit():
        await cmd.finish(Message("用法: /music del 3   （序号来自 /music list）"))
    state = service.current_window()
    removed = await service.store.remove_song(group_id, state.key, int(rest[0]))
    if removed is None:
        await cmd.finish(Message("序号不存在"))
    await cmd.finish(Message(f"已删除: {removed.display()}"))


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
        lines.append(f"{idx}. {song.title} - {artist}")
    text = "\n".join(lines)
    for chunk in split_text(text):
        await bot.send(event, Message(chunk))
    await bot.send(event, Message("提示：也可用 /music archive 尝试自动建歌单"))

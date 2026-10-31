"""配置管理 Web UI。

把 ``AppConfig`` 的 pydantic 结构自省成一份 schema，前端据此自动渲染表单——
以后在 ``config.py`` 里加字段，UI 会自动出现，无需改前端。

挂载在 NoneBot 的 FastAPI 应用上（与 OneBot 共用 8080 端口，不同路径），
用 ``MUSIC_WEBUI_TOKEN`` 做统一鉴权，避免暴露在公网时被改配置。

核心（schema 构建 / 值转换 / 原子更新）都是纯函数，方便单测，不依赖 HTTP。
"""

from __future__ import annotations

import ast
import os
import secrets
import typing
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# NoneBot 用 pydantic-settings 读 .env，但不会把变量注入 os.environ；
# 这里保存 .env 路径，启动时再显式 load_dotenv 一次，确保令牌可被读到。
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from nonebot import get_app
from nonebot.log import logger
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from .config import (
    AppConfig,
    apply_config_value,
    config_manager,
    effective_config,
    group_overrides,
    is_group_scopable,
    reset_config_value,
)
from . import logbuffer
from . import llmtrace as llmtrace_mod
from . import memory as memory_mod
from . import persona as persona_mod
from . import skills as skills_mod
from . import style as style_mod
from . import tools as tools_mod
from . import webtool
from .llm import (
    SYSTEM_CHAR_LIMIT,
    deepseek_effort,
    is_reasoning_model,
    llm_bot,
    normalize_endpoint,
    parse_extra_body,
    payload_preview,
    resolve_thinking_style,
    thinking_budget,
)
from .models import PLATFORM_NAMES
from .store import MASTER_KEY
from .naming import resolve_alias
from .scheduler import next_run_of, next_runs, reload_jobs
from .service import service
from .digest import digest_group

# -------------------------------------------------------------------- 元数据

SECTION_TITLES = {
    "general": "通用设置",
    "window": "收集时间窗口",
    "playlist": "歌单与简介",
    "card": "音乐卡片策略",
    "render": "长图渲染",
    "cache": "缓存清理",
    "clear": "歌曲记录清理",
    "intro": "自我介绍",
    "reply": "收录回复模板",
    "netease": "网易云登录",
    "logs": "运行日志",
    "llm": "机器人设置",
    "memory": "群记忆",
    "persona": "人设修订",
}

#: 配置页分组的展示顺序。网易云登录紧跟「通用设置」，登录 / 重登 / 粘贴 cookie
#: 都在这一个分组里完成（它们原先散在独立的「网易云账号」页）。
SECTION_ORDER = [
    "general", "netease", "window", "playlist", "card", "render",
    "cache", "clear", "intro", "reply", "master", "game", "logs",
]

# dotted_key -> (label, hint, multiline?)
FIELD_META: dict[str, tuple[str, str, bool]] = {
    "enabled": ("总开关", "关掉后机器人不对群消息做任何反应", False),
    "collect_override": ("收集状态覆盖", "auto=按时间窗口自动判断 / on=强制收集中 / off=强制停止收集", False),
    "groups": ("生效群号", "留空=所有群，多个用逗号分隔", False),
    "reply_card": ("@回复并回发卡", "识别到音乐后是否 @ 分享者并回发歌曲名片", False),
    "notify_duplicate": ("重复提醒", "同一首歌被重复分享时是否提示首发者", False),
    "report_groups": ("播报群", "汇总/归档结果额外发到这些群，逗号分隔", False),
    "debug_detect": ("识别调试日志", "排查「分享了没反应」时打开", False),

    "window.mode": ("循环模式", "weekly=每周 / daily=每日 / once=单次", False),
    "window.timezone": ("时区", "服务器多为 UTC，务必设为 Asia/Shanghai，否则定时偏 8 小时", False),
    "window.archive_same_as_end": ("归档=结束", "开：结束收集即刻建歌单，只跑一个任务；关：分别触发", False),
    "window.weekly.start": ("每周·开始", "星期缩写 + 时间，如 MON 20:00", False),
    "window.weekly.summary": ("每周·汇总", "如 SUN 22:00", False),
    "window.weekly.end": ("每周·结束收集", "此时间点后不再收录新歌，如 SUN 22:30", False),
    "window.weekly.archive": ("每周·归档建歌单", "新建网易云歌单的时刻", False),
    "window.daily.start": ("每日·开始", "如 00:00", False),
    "window.daily.summary": ("每日·汇总", "如 23:00", False),
    "window.daily.end": ("每日·结束收集", "如 23:30", False),
    "window.daily.archive": ("每日·归档建歌单", "如 23:30", False),
    "window.once.start": ("单次·开始", "如 2026-08-10 00:00", False),
    "window.once.summary": ("单次·汇总", "如 2026-08-20 22:00", False),
    "window.once.end": ("单次·结束收集", "如 2026-08-20 22:30", False),
    "window.once.archive": ("单次·归档建歌单", "如 2026-08-20 22:30", False),

    "playlist.name_template": ("歌单名模板", "占位符 {seq}{slash}{yy}{m}{d}{window}{count}", True),
    "playlist.description_template": ("简介开头模板", "后续会自动接上「谁分享了什么歌」清单", True),
    "playlist.include_sharers": ("附分享清单", "是否在简介里附上分享者清单", False),
    "playlist.sharer_style": ("清单样式", "list=逐首列 / by_person=按人聚合 / by_name=只列分享者名 / none=不附", False),
    "playlist.seq": ("期号", "用于 Wk.86 这种编号", False),
    "playlist.seq_auto_increment": ("期号自增", "每次成功归档后自动 +1", False),
    "playlist.pending_name": ("一次性歌单名", "设置后仅下一次归档生效，用完自动清空", False),
    "playlist.privacy": ("歌单隐私", "建为隐私歌单", False),
    "playlist.cross_platform_match": ("跨平台匹配", "非网易云的歌曲也去网易云搜匹配后加入", False),
    "playlist.strict_match": ("严格匹配", "歌名+歌手都要对得上；关掉后只按歌名，命中率高但可能加错版本", False),
    "playlist.batch_size": ("加歌批大小", "单次 add 接口提交的歌曲数（网易云有限制）", False),
    "playlist.desc_retry": ("简介写入重试", "网易云对改简介有频控，失败会自动入队补写", False),
    "playlist.desc_retry_minutes": ("补写间隔(分)", "定时补写待写简介的间隔，<=0 关闭", False),
    "playlist.emoji_style": ("表情处理", "text=转中文词 / strip=直接删 / keep=原样（keep 大概率写不进网易云）", False),
    "playlist.desc_show_artist": ("清单带歌手", "简介清单条目是否带歌手名", False),
    "playlist.desc_blank_line": ("清单空行", "简介清单条目之间是否插空行", False),
    "playlist.sharer_aliases": ("分享者昵称映射", "每行 原昵称或QQ号码=显示名，如 菜老名=Jacksing 或 123456789=Jacksing；仅做展示层替换，入库仍保留原始昵称。建议在「昵称映射」独立页编辑", True),
    "playlist.one_per_sharer": ("每人本期只收一首", "同一窗口内同一用户只收录第一首；该用户首次分享即占位，之后再分享只回提示不入榜。占位那首若是因**重复**（总库已有 / 同窗口已有）而没进榜，名额会当场还给本人，他换歌照样能收；占位所指的歌被管理员删掉后名额也会自动还给该用户。提示文案见「同一用户重复提示」", False),
    "playlist.notify_unmatched": ("提示无法匹配", "非网易云歌曲在分享时就探测能否匹配到网易云，匹配不到则把提示附在收录消息里（文案见「无法匹配提示」）。关闭可省掉这次预搜索", False),
    "playlist.reply_match": ("引用回填匹配", "在群里**引用**机器人那条「没法匹配」的提示消息、并贴上正确的网易云歌曲链接，即把这首歌绑定到该链接（管理员或该歌分享者才可操作）；只对尚未匹配的歌生效，命中后立刻补进本期歌单。文案见「回填成功/失败/无权限提示」", False),

    "card.mode": ("卡片模式", "native=平台原生(依赖签名服务) / custom=自定义卡片 / off=只发文字+封面", False),
    "card.fallback_custom": ("失败后转自定义卡", "原生卡片失败是否自动再试自定义卡片", False),
    "card.fallback_text": ("卡片全失败补文字", "卡片全部失败是否补发一条文字", False),
    "card.fallback_cover": ("文字兜底附封面", "文字兜底里是否附上封面图", False),
    "card.failure_threshold": ("熔断阈值", "同平台连续失败多少次后熔断，<=0 关闭", False),
    "card.cooldown_minutes": ("熔断冷却(分)", "熔断后冷却多久自动恢复试探", False),

    "render.max_items_per_image": ("单页最多条目", "超出自动分页", False),
    "render.show_cover": ("绘制封面", "是否下载并绘制封面", False),
    "render.font_path": ("自定义字体路径", "留空则自动探测系统中文字体", False),
    "render.theme": ("图片主题", "light / dark", False),

    "cache.enabled": ("缓存清理总开关", "定期删除榜单长图/封面缓存", False),
    "cache.keep_days": ("保留天数", "超过就删，<=0 表示不按时间清", False),
    "cache.max_render_files": ("长图最多保留", "<=0 表示不限", False),
    "cache.max_cover_files": ("封面最多保留", "<=0 表示不限", False),
    "cache.clean_at": ("每日清理时刻", "如 04:30", False),
    "cache.clean_on_start": ("启动即清一次", "机器人启动时先清一遍缓存", False),
    "cache.clean_after_render": ("渲染后顺手清", "每次渲染完榜单顺手清一次", False),

    "clear.after_archive": ("归档后清空", "建歌单成功后自动清空本期已收集歌曲", False),
    "clear.scheduled_enabled": ("定时清理总开关", "按保留天数定期删除收集记录", False),
    "clear.keep_days": ("保留天数", "早于 now-keep_days 的记录会被删，<=0 不按时间清", False),
    "clear.prune_at": ("每日清理时刻", "如 05:00", False),

    "intro.enabled": ("自我介绍开关", "被 @ 时是否回发自我介绍", False),
    "intro.text": ("自我介绍文案", "支持占位符：{nick}分享者 {count}已收集数 {state}收集状态 {playlist}歌单(名+链接) {window}窗口文案，用 \\n 换行", True),
    "intro.cooldown": ("冷却(秒)", "同群多久内不再重复发，0=不限", False),
    "intro.at_sender": ("@提问者", "回复时是否 @ 对方", False),
    "intro.skip_commands": ("跳过命令", "消息带 /music 命令时不发自我介绍", False),
    "intro.skip_music": ("跳过音乐分享", "消息带音乐链接时不发（那是分享不是提问）", False),
    "intro.always_reply": ("始终回应", "收集关闭/不在收集期时也仍回自我介绍", False),

    "reply.enabled": ("启用自定义回复", "关闭则用内置格式（等同默认模板）", False),
    "reply.accept_text": ("收录回复文案", "识别到新歌入库后回发的消息。占位符：{index}本期序号 {nick}分享者 {title}歌名 {artists}歌手 {album}专辑 {platform}来源 {url}歌曲链接 {duration}时长 {artists_line}整行歌手(无则消失) {album_line}整行专辑(无则消失) {unmatched_line}整行「网易云搜不到」提示(匹配成功时消失) {song}详情块 {playlist}歌单(名+链接) {count}已收录数 {window}窗口文案；用 \\n 换行", True),
    "reply.playlist_empty_text": ("歌单未生成替代文案", "{playlist} 在本期还没归档时显示的替代文字", False),
    "reply.unmatched_text": ("无法匹配提示", "歌曲在网易云搜不到时，附在**收录消息同一条**里的提示（不额外刷屏）。占位符：{title}歌名 {artists}歌手 {platform}来源 {nick}分享者 {index}本期序号 {count}已收录数 {window}窗口文案；用 \\n 换行", True),
    "reply.sharer_limit_text": ("同一用户重复提示", "同一用户本期再次分享时的提示（见「每人本期只收一首」）。占位符：{nick}本次分享者 {title}其本期首发的歌名 {artists}首发歌歌手 {platform}首发歌来源 {index}首发歌本期序号 {count}已收录数 {window}窗口文案；用 \\n 换行", True),
    "reply.match_ok_text": ("回填成功提示", "群里引用「没法匹配」的提示消息并贴网易云链接、绑定成功后的回执（见「引用回填匹配」）。占位符：{title}原歌名 {artists}原歌手 {matched_title}匹配到的歌名 {matched_artists}匹配到的歌手 {nick}操作者 {window}窗口文案；用 \\n 换行", True),
    "reply.match_fail_text": ("回填失败提示", "回填匹配失败时的回执（链接解析不出 / 拉不到详情 / 该歌已匹配过）。占位符：{title}原歌名 {reason}失败原因；用 \\n 换行", True),
    "reply.match_deny_text": ("回填无权限提示", "非管理员、也非该歌分享者却引用指定匹配时的提示。占位符：{title}原歌名 {nick}操作者；用 \\n 换行", True),

    "playlist.name_template": ("歌单名模板", "占位符：{seq}期号 {slash}如26/8/7 {y}{yy}年 {m}{mm}月 {d}{dd}日 {ymd}{date}日期 {week}周数 {weekday}星期 {start}起始日 {end}结束日 {window}窗口文案 {count}收录数 {total}分享数 {sharers}人数 {group}群号", True),
    "playlist.description_template": ("简介开头模板", "后续自动接「谁分享了什么歌」清单。占位符同歌单名，另可用 {songlist}歌曲清单 {sharerlist}按人聚合清单；{group}群号 {window}窗口 {count}数", True),
}

# -------------------------------------------------------------------- 总库字段元信息

SECTION_TITLES["master"] = "总库"

# 总库顶层开关 / 提示模板
FIELD_META["master.enabled"] = (
    "总库开关", "开启后每首分享同时进入群级总库（跨窗口去重），并提供总库管理页", False,
)
FIELD_META["master.compare_on_share"] = (
    "分享对比总库", "开启后分享已存在于总库的歌会提示重复（仅跨窗口；同窗口重复走通用「重复提醒」）", False,
)
FIELD_META["master.notify_template"] = (
    "重复提示文案", "占位符：{title}歌名 {artists}歌手 {platform}来源 {sharer}本次分享者 {who}总库首发者 {index}总库序号 {count}总库总数 {window}当前窗口 {period}首发期号(来源窗口) {date}首发日期(YY/MM/DD)；用 \\n 换行", True,
)
FIELD_META["master.auto_archive"] = (
    "分享即归档总库", "每收到新分享立即把总库增量同步到总库歌单（静默执行，不刷屏）", False,
)

# 总库归档用 playlist 配置：复用 playlist.* 的字段元信息，文案改指总库。
for _k, _v in list(FIELD_META.items()):
    if _k.startswith("playlist."):
        _label, _hint, _multi = _v
        _label = _label.replace("歌单", "总库歌单")
        FIELD_META["master." + _k[len("playlist."):]] = (_label, _hint, _multi)


# -------------------------------------------------------------------- 猜歌字段元信息

SECTION_TITLES["game"] = "猜歌小游戏"

# 猜歌整段都是进程级设置：房间不按群划分，所以在「按群」视图里会被灰掉
# （config.GLOBAL_ONLY_PREFIXES 里已经加了 game.）。
FIELD_META["game.enabled"] = (
    "猜歌总开关", "关掉后 /guess 页面与接口都返回 404（已开的房间也会失效）", False,
)
FIELD_META["game.whitelist_only"] = (
    "仅限名单", "只允许「猜歌名单」里的 QQ 号登录；关掉后任何 QQ 号都能进（调试用）", False,
)
FIELD_META["game.allow_single"] = (
    "开放单人模式", "允许自己一个人刷题（从历史窗口取题，不需要组队）", False,
)
FIELD_META["game.max_players"] = ("单房上限", "一个房间最多几个人", False)
FIELD_META["game.room_idle_minutes"] = (
    "房间空闲回收(分)",
    "多久没有任何业务动作就回收房间。注意：WebSocket 心跳不算动作，否则房间永远不会过期",
    False,
)
FIELD_META["game.code_ttl_minutes"] = ("验证码有效期(分)", "登录验证码多久失效", False)
FIELD_META["game.session_days"] = ("登录态有效期(天)", "登录后多久不用要重新验证", False)
FIELD_META["game.dm_group"] = (
    "临时会话群号",
    "发验证码时用哪个群发起「群临时会话」（玩家不是机器人好友时靠这条送达）。0 = 自动：先用名单里记的来源群，再退回第一个有歌的群",
    False,
)
FIELD_META["game.guess_slots"] = ("每题排几人", "每题要按顺序排几个分享者，最多 3", False)
FIELD_META["game.scores"] = (
    "得分档位", "命中第 1/2/3 位各得多少分，逗号分隔，如 3,2,1", False,
)
FIELD_META["game.live_limit_per_min"] = (
    "现场贴歌限频", "现场收集时每人每分钟最多提交几次（防刷解析接口）", False,
)
FIELD_META["game.random_default_count"] = (
    "总库抽题默认数量", "「从总库抽题」默认抽几首（每个分享者最多出一首）", False,
)
FIELD_META["game.random_max_count"] = (
    "总库抽题上限", "「从总库抽题」一次最多抽几首，超出按上限截断", False,
)
FIELD_META["game.archive_by_host"] = (
    "允许房主归档", "房主能否把现场收集的歌归档成网易云歌单（复用收集机器人的建歌单/简介逻辑）", False,
)
FIELD_META["game.public_base_url"] = (
    "对外访问地址", "生成房间分享链接用，如 https://music.example.com；留空按请求头推断", False,
)


# -------------------------------------------------------------------- 运行日志字段元信息

FIELD_META["logs.lines"] = (
    "日志保留条数",
    "内存里保留多少条日志（50 ~ 200000），满了丢最旧的。约 1000 条 ≈ 200KB 内存",
    False,
)
FIELD_META["logs.level"] = (
    "日志记录等级",
    "记录的最低等级：TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL。排查问题时临时降到 DEBUG，看完记得调回 INFO",
    False,
)
FIELD_META["logs.file"] = (
    "日志落盘路径",
    "额外把日志写到该文件（相对路径基于项目根目录），按 5MB 轮转、保留 3 份，可跨重启留存；留空表示不落盘。内存缓冲不受影响",
    False,
)
FIELD_META["logs.trace_messages"] = (
    "记录收发消息",
    "把「收到的群消息」和「机器人发出的回复」都记一条摘要（含普通聊天）。想在日志页看清谁在什么时候发了什么就保持开启；嫌吵可关掉，收集开关 / 改设置 / 归档结果等事件日志仍会记录",
    False,
)


# -------------------------------------------------------------------- 网易云登录字段元信息

FIELD_META["netease.auto_relogin"] = (
    "掉登录自动重登",
    "写歌单简介/建歌单返回「需要登录」时，自动续期 cookie 或用手机号密码重新登录后重试（无需人工粘贴 cookie）",
    False,
)
FIELD_META["netease.phone"] = (
    "登录手机号",
    "留空则只做 cookie 续期，不做账密重登",
    False,
)
FIELD_META["netease.password"] = (
    "登录密码",
    "明文保存在 data/config.yaml；也可以只填下面的 password_md5。⚠️ 请确保配置文件只有自己可读（Linux: chmod 600 data/config.yaml）",
    False,
)
FIELD_META["netease.password_md5"] = (
    "密码 MD5",
    "32 位十六进制小写；填了它就不用填上面的明文密码",
    False,
)
FIELD_META["netease.countrycode"] = (
    "手机号国家码",
    "大陆填 86",
    False,
)
FIELD_META["netease.relogin_cooldown"] = (
    "重登冷却(秒)",
    "两次自动重登尝试的最小间隔，避免风控期疯狂重试；<=0 表示不限制",
    False,
)


# -------------------------------------------------------------------- 机器人（大模型）字段元信息
#
# 这一段独立成「机器人」页，**只在网页端修改**：群内没有任何命令能写 llm.*，
# 且 llm.* 被 config.GLOBAL_ONLY_PREFIXES 排除在「按群覆盖」之外。

FIELD_META["llm.enabled"] = (
    "启用大模型回复",
    "开启后，群里 @机器人 说一句话（不是音乐分享、也不是 /music 命令）就由大模型回答；关闭则仍然回内置自我介绍",
    False,
)
FIELD_META["llm.groups"] = (
    "生效群号",
    "只在这些群启用；留空 = 所有群。多个用逗号分隔",
    False,
)
FIELD_META["llm.base_url"] = (
    "接口地址",
    "OpenAI 兼容的 base url，填到 /v1 为止即可，如 https://api.deepseek.com/v1 。程序会自动补 /chat/completions",
    False,
)
FIELD_META["llm.api_key"] = (
    "API Key",
    "保存在 data/config.yaml（日志里打码）。支持 DeepSeek / 通义 / 智谱 / Moonshot / Ollama / one-api 等 OpenAI 兼容服务",
    False,
)
FIELD_META["llm.model"] = (
    "模型名",
    "如 deepseek-chat / gpt-4o-mini / qwen-plus / llama3.1（要与所填接口支持的模型一致）",
    False,
)
FIELD_META["llm.system_prompt"] = (
    "系统提示词",
    "机器人的人设与说话风格。想让回复更像群友闲聊就写得口语一点；用 \\n 换行",
    True,
)
FIELD_META["llm.temperature"] = (
    "温度",
    "0 最稳、2 最飘。闲聊建议 0.7~0.9。推理模型（o1/o3/gpt-5/reasoner）不认这个参数，会自动省掉；"
    "DeepSeek 等厂商在**思考模式下也会忽略它**（不报错、不生效）",
    False,
)
FIELD_META["llm.top_p"] = (
    "核采样 top_p",
    "只从累计概率最高的这部分词里挑，1.0 = 不启用。一般只调温度或只调 top_p，两个一起调会互相打架。"
    "注意 DeepSeek 的 deepseek-flash 只在思考模式下认它，且有效范围是 0.95~1.0，填更低会按 0.95 处理",
    False,
)
FIELD_META["llm.presence_penalty"] = (
    "话题新鲜度",
    "-2~2，0 = 不干预；调大更愿意换话题，调小更贴着上文说",
    False,
)
FIELD_META["llm.frequency_penalty"] = (
    "词频惩罚",
    "-2~2，0 = 不干预；调大少重复用词，调小允许复读（群聊刷梗可以给一点负值）",
    False,
)
FIELD_META["llm.stop"] = (
    "停止词",
    "生成到这些词就立刻收尾，逗号分隔。群聊一般用不到，留空即可",
    False,
)
FIELD_META["llm.seed"] = (
    "随机种子",
    "-1 = 不发送（每次随机）。填固定数字后同样的提问更容易得到同样的回复，调试用",
    False,
)
FIELD_META["llm.thinking"] = (
    "思考模式",
    "推理模型的“想多久”：自动 = 不干预、用模型默认；关闭 = 尽量关掉思考；低/中/高/最高 = 开思考并给强度。"
    "非推理模型选什么都不起作用。DeepSeek 侧的映射：低→low、中→high、高→high、最高→max",
    False,
)
FIELD_META["llm.thinking_style"] = (
    "思考参数写法",
    "各家 API 不统一：自动 = 按接口地址和模型名猜（通义→enable_thinking、GLM/Claude→thinking 对象、"
    "OpenAI→reasoning_effort、DeepSeek→thinking 开关 + reasoning_effort）。"
    "**用中转 / 本机地址时自动识别看不出厂商，要手动选 deepseek**，否则档位不生效。"
    "猜错了也可以在下面「额外请求参数」里手动覆盖",
    False,
)
FIELD_META["llm.thinking_budget"] = (
    "思考预算 token",
    "0 = 按档位默认（低 1024 / 中 4096 / 高 12000）。预算会占用最大回复 token 的额度，"
    "超过“最大回复 token - 256”时会被自动压下来——想真正高强度思考，先把最大回复 token 调大",
    False,
)
FIELD_META["llm.extra_body"] = (
    "额外请求参数 (JSON)",
    '原样合并进请求体，最万能的兜底。例如 {"top_k": 20} 或 {"chat_template_kwargs": {"enable_thinking": true}}。'
    "写在这里的键会覆盖上面所有同名参数",
    True,
)
FIELD_META["llm.max_tokens"] = (
    "最大回复 token",
    "单次生成的上限，群聊一般 256~800 足够。⚠️ 开了思考模式时、思考的 token 也算在这里，"
    "记得留出余量（否则会出现“只有思考没有回答”）",
    False,
)
FIELD_META["llm.timeout"] = (
    "超时(秒)",
    "单次请求最长等待时间，超时就失败（并按下方「失败退回自我介绍」处理）",
    False,
)
FIELD_META["llm.history_rounds"] = (
    "上下文轮数",
    "携带最近几轮对话（1 轮 = 1 问 + 1 答），最多 200 轮；0 = 每次都是全新对话。"
    "注意两者是「谁先到头谁生效」：轮数和下面的总字符上限，先到哪个就按哪个截——"
    "所以想记更久必须**同时把总字符调大**，只加轮数不会变长。上下文只在内存里，重启即清空",
    False,
)
FIELD_META["llm.history_chars"] = (
    "上下文总字符上限",
    "真正决定「一次请求带多少历史」的旋钮：超过这个总字数就从最旧的成对丢弃；0 = 不限（不推荐）。"
    "参考：中文实测约 2 字/token（即 0.5 token/字），3000 字 ≈ 1500 token；"
    "模型窗口 64k 的放到 20000~30000 仍安全，8k/16k 的建议 2000~4000。"
    "真超窗了程序也会自动丢历史重试一次",
    False,
)
FIELD_META["llm.context_window"] = (
    "上下文窗口(token)",
    "只用于「上下文用量」那条进度条的**分母**，不参与请求、不影响裁剪（裁剪看上面的字符上限）。"
    "实测当前中转 1,048,576；换成小窗口模型（8k/32k/128k）时记得改小，否则百分比会显得过于乐观",
    False,
)
FIELD_META["llm.cooldown"] = (
    "冷却(秒)",
    "同一用户在同一群的两次回复最小间隔，防止刷屏；0 = 不限",
    False,
)
FIELD_META["llm.read_reply"] = (
    "读取引用内容",
    "群友「引用一条消息 + 提问」时，把被引用的那句话一起给模型看（否则模型不知道他在问什么）",
    False,
)
FIELD_META["llm.reply_chars"] = (
    "引用内容字数上限",
    "被引用消息最多取多少字；0 = 不限。群里长消息很多，建议留个上限",
    False,
)
FIELD_META["llm.vision"] = (
    "让模型看图",
    "把图片发给模型。<b>开关只决定「发不发」，能不能看懂取决于模型</b>——"
    "需要 qwen-vl / glm-4v / gpt-4o 这类视觉模型；"
    "打开但模型不支持时会自动去掉图片重试，不会让群里看到报错。关闭时图片只留 [图片] 标记",
    False,
)
FIELD_META["llm.max_images"] = (
    "最多几张图",
    "一次消息里最多带几张图片给模型（多的只留 [图片] 标记）；0 = 都不带",
    False,
)
FIELD_META["llm.read_forward"] = (
    "读取转发聊天记录",
    "群友「转发聊天记录」时，把里面的内容展开给模型看。关掉时模型只看到 [聊天记录] 四个字，"
    "问「他们说的啥」只能瞎猜。嵌套转发只展开一层",
    False,
)
FIELD_META["llm.forward_limit"] = (
    "记录最多几条",
    "一次最多展开多少条转发消息（聊天记录可能上百条，不封顶会把上下文撑爆）",
    False,
)
FIELD_META["llm.forward_chars"] = (
    "记录最多多少字",
    "转发内容的总字数上限，超出会标出「还有 N 条未展开」；0 = 不限制（不推荐）",
    False,
)
FIELD_META["llm.bg_chat"] = (
    "带上群聊背景",
    "被动回答（@ / 戳一戳 / 发链接）时，先把「机器人上次说话之后」群里聊的那一段给它看。<br>"
    "为什么需要：群友闲聊存的是「旁听记录」、问答历史是另一条通道，两者分开——"
    "不加这个，它其实<b>不知道大家在聊什么</b>，问「刚才聊啥」会答不上来。<br>"
    "这段走参考资料传，<b>不进对话历史</b>，也不占「上下文总字符上限」的预算",
    False,
)
FIELD_META["llm.bg_chat_max"] = (
    "背景最多几条",
    "从上次说话之后算起，最多取几条群消息。群里刷屏时不给上限会一次带上近百条",
    False,
)
FIELD_META["llm.bg_chat_chars"] = (
    "背景最多多少字",
    "2000 字约 1000 token。太大不只费钱，还会把真正的提问挤到后面去",
    False,
)
FIELD_META["llm.bg_chat_minutes"] = (
    "背景只看多久以内",
    "只认这段时间内的群聊。它可能几小时没说话，中间的全捞进来也没用，"
    "而且容易把旧话题当成「刚才在聊」",
    False,
)
FIELD_META["llm.bg_chat_min_rows"] = (
    "背景至少补几条",
    "「上次说话之后」不足这么多条时，<b>往前补到这个数</b>。<br>"
    "它刚回完一句就被人 @ 时，界限之后只剩这句提问本身，等于<b>没有语境</b>——"
    "表现就是「上一句还答得好好的，下一句突然问我在说什么」。补一小段就能接上话题。<br>"
    "<code>0</code> = 不补，只看上次说话之后",
    False,
)
FIELD_META["llm.tools"] = (
    "启用工具",
    "总闸。开着模型才能在需要时调工具（抓网页、读技能）；关掉就纯聊天，"
    "也不再往请求里塞 tools 字段——接口不认 tools 时这里关掉最省事",
    False,
)
FIELD_META["llm.tool_rounds"] = (
    "最多调几轮工具",
    "一次回复里允许连续调几次工具（每轮可含多个调用）。模型每轮都要多花一次请求。"
    "默认 5：查「某事的后续 / 最新进展」时 3 轮常常差一口气，"
    "差的那口气会让它跑到最后一轮去「手写工具调用语法」（发出去就是乱码）。"
    "0 = 不许调工具",
    False,
)
FIELD_META["llm.tool_chars"] = (
    "工具结果上限（字）",
    "单次工具结果最多回填多少字。网页正文动辄几千字，全塞进去会挤掉对话和上下文预算",
    False,
)
FIELD_META["llm.web_tool"] = (
    "允许抓网页（fetch_url）",
    "给模型一个「抓这个链接」的工具，抓不抓由它自己判断。"
    "和以前的自动抓取不同：不会每条消息都去抓，也不会把无关页面塞进上下文",
    False,
)
FIELD_META["llm.fetch_chars"] = (
    "抓回正文上限（字）",
    "fetch_url 返回给模型的正文字数上限，超出截断。调大更完整，但每次调用更贵",
    False,
)
FIELD_META["llm.fetch_timeout"] = (
    "抓网页超时（秒）",
    "超过这个时间就放弃这次抓取，并把这个失败结果告诉模型（它会说打不开）",
    False,
)
FIELD_META["llm.fetch_cache_minutes"] = (
    "同一链接缓存多久（分钟）",
    "同一个网址在这段时间内只真抓一次。群里一个链接常被几个人先后贴上，"
    "模型也可能两轮里抓两次——缓存省的是重复请求和时间。填 0 = 每次都重新抓",
    False,
)
FIELD_META["llm.fetch_summarize"] = (
    "长网页按问题摘要",
    "模型抓网页时可以顺便说明「我想知道什么」。开了之后，正文超过下面的字数就"
    "**按这个问题摘要**，而不是把前 2000 字硬塞给它——群里贴一篇长评测问「散热怎么样」"
    "时差别很大。代价是每次抓取多一轮模型调用（更慢一点、花一点钱）",
    False,
)
FIELD_META["llm.fetch_summarize_chars"] = (
    "正文超过多少字才摘要",
    "低于这个字数直接给原文更准也更省。长文才会先摘要",
    False,
)
FIELD_META["llm.web_search"] = (
    "允许模型搜网页",
    "给它一个 web_search 工具：问的是新闻、价格、比分、谁的现任职位、软件最新版本这类"
    "会过时效的东西时，它会先搜再答，而不是凭记忆编。"
    "<br>走 Bing 的公开页面，不需要 API Key，但<b>搜得太密会被要求验证码</b>——"
    "先用两天看看日志里有没有「搜索失败」，再决定要不要一直开着。"
    "<br>只有「抓网页」也开着时这个工具才会出现（搜到网址却打不开没意义）",
    False,
)
FIELD_META["llm.search_max"] = (
    "一次搜几条",
    "条数越多越费 token。5 条够回答绝大多数问题，摘要通常已经把答案说清楚了",
    False,
)
FIELD_META["llm.search_snippet_chars"] = (
    "每条摘要最多多少字",
    "搜索结果里每条的摘要上限；0 = 不截。截短能省 token，太长会挤占历史和其他工具结果的空间",
    False,
)
FIELD_META["llm.search_timeout"] = (
    "搜索超时（秒）",
    "Bing 国内一般 1~3 秒就回；超过这个时间就放弃，把「搜索超时」告诉模型（它会说搜不到）",
    False,
)
FIELD_META["llm.search_interval"] = (
    "两次搜索的最小间隔（秒）",
    "防止被搜索引擎要求验证码：一次回复里模型可能连着搜好几次，间隔太小就会被拦。"
    "0 = 不限，不推荐",
    False,
)
FIELD_META["llm.skills"] = (
    "启用技能包",
    "扫描技能目录下的 SKILL.md：模型先看「名字 + 简介」，需要时才读全文照着做。"
    "加技能就是丢一个目录进去，不用改代码",
    False,
)
FIELD_META["llm.skills_dir"] = (
    "技能目录",
    "留空 = data/skills。相对路径按 data/ 解析；每个技能一个子目录，里面必须有 SKILL.md",
    False,
)
FIELD_META["llm.skill_body_chars"] = (
    "技能正文上限（字）",
    "use_skill 返回的技能正文最多多少字，超出截断。技能写得长可以调大",
    False,
)
FIELD_META["llm.skill_file_chars"] = (
    "附加文件上限（字）",
    "技能正文里让你「读取 references/xxx.md」时，read_skill_file 一次最多返回多少字。"
    "技能是渐进披露的：主线在 SKILL.md，细化的写法放到附属文件里按需读",
    False,
)
FIELD_META["llm.skill_scripts"] = (
    "允许执行技能脚本",
    "⚠️ 打开后模型可以运行技能目录里的 .py / .sh 脚本（只限已存在的文件，有超时和输出上限）。"
    "这等于「模型能让你服务器上跑代码」，只在完全信任技能来源时打开",
    False,
)
FIELD_META["llm.skill_timeout"] = (
    "脚本超时（秒）",
    "技能脚本跑超过这个时间直接被 kill，并把超时告诉模型",
    False,
)

# ---- 群记忆（单独一段，挂在「机器人」页）----
FIELD_META["memory.enabled"] = (
    "记忆总开关",
    "关掉后不旁听、不注入档案、也不给模型记忆工具；已有档案原样留在磁盘上",
    False,
)
FIELD_META["memory.dir"] = (
    "记忆目录",
    "留空 = data/memory；每人一份 md，可以直接编辑。相对路径按 data/ 解析",
    False,
)
FIELD_META["memory.inject_chars"] = (
    "单次注入字数",
    "每次请求最多带多少字的档案（按最近活跃取人，超出的会提示用 recall 查）",
    False,
)
FIELD_META["memory.note_chars"] = (
    "单人档案上限",
    "一个人的档案最多多少字；到顶后新记录会挤掉最早的条目",
    False,
)
FIELD_META["memory.listen"] = (
    "旁听群消息",
    "只记录、不回复。关掉的话机器人只记得 @ 它的那几句，基本等于记不住人",
    False,
)
FIELD_META["memory.listen_groups"] = (
    "旁听群号",
    "留空 = 跟「大模型生效群号」一致；多个用逗号分隔",
    False,
)
FIELD_META["memory.digest_hours"] = (
    "整理间隔（小时）",
    "多久把新群聊归纳进档案一次；0 = 不自动整理，只能在「群记忆」页手动点",
    False,
)
FIELD_META["memory.digest_msgs"] = (
    "单次整理条数",
    "一次最多看多少条新消息（新的优先，太旧的会被丢掉）",
    False,
)
FIELD_META["memory.digest_chars"] = (
    "整理素材字数",
    "喂给整理任务的聊天记录最多多少字",
    False,
)
FIELD_META["memory.digest_model"] = (
    "整理用模型",
    "留空 = 跟主模型一致；整理是读+归纳，用便宜的小模型也够",
    False,
)
FIELD_META["memory.digest_max_tokens"] = (
    "整理输出上限",
    "整理结果是 JSON，太小会被截断导致解析失败",
    False,
)
FIELD_META["memory.keep_days"] = (
    "记录保留天数",
    "群聊记录（上下文 + 整理素材）留多少天；0 = 永久保留",
    False,
)
FIELD_META["memory.style_enabled"] = (
    "学群说话方式",
    "从群聊里归纳句长、标点、口头禅，并挑几条原话当范例注入；"
    "这是「像人」的关键，和记「谁是谁」是两件事",
    False,
)
FIELD_META["memory.style_hours"] = (
    "重学间隔（小时）",
    "多久重学一次群说话方式；风格是慢变量，一天一次足够，0 = 只能手动点",
    False,
)
FIELD_META["memory.style_days"] = (
    "采样天数",
    "只看最近多少天的群消息，避免用半年前的口气；0 = 不限",
    False,
)
FIELD_META["memory.style_scan"] = (
    "采样条数上限",
    "一次最多从库里捞多少条消息参与采样（活跃群先到先得，取最新的这些）",
    False,
)
FIELD_META["memory.style_per_person"] = (
    "每人原话上限",
    "每人最多贡献几条原话。设小一点，避免风格卡变成某一个人的口癖合集",
    False,
)
FIELD_META["memory.style_chars"] = (
    "风格卡注入字数",
    "风格卡最多多少字进系统提示词；独立预算，不会挤掉成员档案和人设",
    False,
)
FIELD_META["memory.style_model"] = (
    "风格蒸馏用模型",
    "留空 = 跟主模型一致；这是「读一批话、写一段观察」，小模型通常够用",
    False,
)
FIELD_META["memory.style_max_tokens"] = (
    "风格输出上限",
    "蒸馏结果是 JSON（含原话），太小会被截断导致整轮作废",
    False,
)
FIELD_META["persona.enabled"] = (
    "人设修订总开关",
    "听群友抱怨「AI 味」，攒够了给出一条改人设的建议；**只出建议，不会自己改**",
    False,
)
FIELD_META["persona.detect"] = (
    "实时识别抱怨",
    "关掉后只保留手动提议。先用正则粗筛再让模型确认，不会每条消息都调模型",
    False,
)
FIELD_META["persona.window_minutes"] = (
    "对照窗口（分钟）",
    "只把「机器人刚说完话」这段时间内的抱怨算数，太久之后的可能与它无关",
    False,
)
FIELD_META["persona.min_feedback"] = (
    "提议门槛（条）",
    "攒够多少条新抱怨才提议。太小会让人设变成各次抱怨的缝合怪",
    False,
)
FIELD_META["persona.propose_hours"] = (
    "检查间隔（小时）",
    "多久看一次该不该提议，默认一周；0 = 只在网页上手动点",
    False,
)
FIELD_META["persona.model"] = (
    "提议用模型",
    "留空 = 跟主模型一致；这一步要看懂人设、又想出改法，别用太小的模型",
    False,
)
FIELD_META["persona.max_tokens"] = (
    "提议输出上限",
    "结果是 JSON，太小会被截断导致这一轮白跑",
    False,
)
FIELD_META["persona.persona_limit"] = (
    "人设总字数上限",
    "改完之后人设最多多少字；超出的修改一律驳回，避免人设越改越胖",
    False,
)
FIELD_META["llm.max_input_chars"] = (
    "提问字数上限",
    "单条提问最多取多少字送模型，超出截断；0 = 不限",
    False,
)
FIELD_META["llm.max_reply_chars"] = (
    "回复字数上限",
    "回复最多发多少字，超出截断并加省略号；0 = 不限（超长会自动分条发送）",
    False,
)
FIELD_META["llm.at_sender"] = (
    "回复时 @ 提问者",
    "关闭后直接发文字",
    False,
)
FIELD_META["llm.fallback_intro"] = (
    "失败退回自我介绍",
    "接口报错 / 超时 / 没配好时，退回内置自我介绍（关掉则静默，群里看不到任何回应）",
    False,
)
FIELD_META["llm.poke_reply"] = (
    "戳一戳也回复",
    "群友「双击头像 / 戳一戳」机器人时，让模型回一句（同样受开关、生效群与冷却限制）",
    False,
)
FIELD_META["llm.poke_prompt"] = (
    "戳一戳时说的话",
    "送给模型的那句话，默认「（戳了戳你）」；模型会看到「昵称：这句话」",
    False,
)
FIELD_META["llm.reply_link"] = (
    "非音乐链接也主动接话",
    "默认关：群友发的链接不是音乐平台时，机器人**不 @ 也会主动接一句**——群里容易显得抢话，"
    "所以默认关掉。@ 了机器人再发链接，照常会回答。真·音乐链接任何时候都不受此开关影响",
    False,
)
FIELD_META["llm.link_hint"] = (
    "链接接话的说明",
    "上面开关打开后，接非音乐链接时追加给模型的一句说明，免得它把网页链接当成点歌；留空 = 不加",
    False,
)
FIELD_META["llm.banter"] = (
    "主动搭话",
    "没人 @ 它时也自己冒个泡：每隔一段随机时间，把群里「上次搭话之后」聊的那一段接过去。"
    "素材来自「群记忆」的旁听记录，所以那边也要开着旁听；不 @ 人、不引用",
    False,
)
FIELD_META["llm.banter_minutes"] = (
    "搭话间隔（分钟）",
    "写法 30-60：每次都在这个区间里重新随机取一个时间点。想更安静就写 90-180",
    False,
)
FIELD_META["llm.banter_quiet"] = (
    "勿扰时段",
    "几段都行，用逗号分隔，每段写法 00:00-08:00、12:00-14:00（支持跨午夜 23:00-08:00）。"
    "落在这些时段里的那次会顺延到结束后再随机几分钟",
    False,
)
FIELD_META["llm.banter_groups"] = (
    "只在哪些群搭话",
    "留空 = 跟上面的「生效群」一致；再留空 = 所有有旁听记录的群",
    False,
)
FIELD_META["llm.banter_daily_max"] = (
    "每群每天上限",
    "控制吵不吵最直接的旋钮：0 = 不限（一天约 20 次），填 6 就一天最多 6 次。"
    "间隔决定节奏，这个决定总量",
    False,
)
FIELD_META["llm.banter_fresh_minutes"] = (
    "最后一条超过多久就不接",
    "判的是这批消息的**最后一条**：超过这么多分钟就说明话题已经凉了，这轮空过。"
    "半夜突然接一句两小时前的话很突兀",
    False,
)
FIELD_META["llm.banter_batch_max"] = (
    "一次最多看几条消息",
    "从上次搭话之后算起，一次最多读几条群消息。群里刷屏时不给上限会上百条，"
    "既慢又贵；超出的部分只取最近的",
    False,
)
FIELD_META["llm.banter_batch_chars"] = (
    "一次最多看多少字",
    "素材总字数上限，超了从**最旧的**丢——接话接的是眼下的话题，"
    "半小时前的开场白没什么用。6000 字约 3000 token",
    False,
)
FIELD_META["llm.banter_judge"] = (
    "先判断该不该插嘴",
    "打开后会先让模型当「判官」看一眼这批对话，吵架、严肃话题、别人正在对话、"
    "对话已收尾……说不该就闭嘴。判不出来（报错/回废话）也按不接处理。"
    "关掉就只剩「长度 / 命令 / 纯符号」三道硬规则，基本等于逢话必接",
    False,
)
FIELD_META["llm.banter_judge_model"] = (
    "判官用的模型",
    "留空 = 跟主模型。这活儿很简单，填个便宜快的更划算（如 deepseek-chat / gpt-4o-mini）",
    False,
)
FIELD_META["llm.banter_judge_hint"] = (
    "判官判定标准",
    "直接写「什么情况别接、什么情况可以接」，判官就按这个来。留空 = 用内置清单。"
    "不同群尺度不一样（有的能开玩笑、有的在聊工作），所以建议按自己的群改一版",
    False,
)
FIELD_META["llm.banter_judge_chars"] = (
    "判官最多看多少字",
    "判官不需要看全量，看个大概就够判断气氛。太多反而慢；只取**最近**的那部分",
    False,
)
FIELD_META["llm.banter_reset_on_reply"] = (
    "回复后重新计时",
    "它被动回完一句（@ / 戳一戳 / 发链接）之后，把主动搭话的计时<b>重新起算</b>。<br>"
    "不开的话，它刚答完人还可能被排到的那一轮捞起来自己冒泡，群里看着像抢话；"
    "而且刚说完这一两句，可接的素材也还没跟上。<br>"
    "间隔仍取上面那个区间——是「从现在起再等一个间隔」，不是把下一次提前",
    False,
)

#: 有**硬上限**的字段：值会被后端静默截断，所以表单里要在输入框下面显示
#: 「已用 x / 上限 字」。数字统一取自真正生效的那个常量，避免两处各写一份。
FIELD_LIMITS: dict[str, int] = {
    "llm.system_prompt": SYSTEM_CHAR_LIMIT,
}


# -------------------------------------------------------------------- schema

def _is_basemodel(tp: object) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _python_type(finfo) -> str:
    ann = finfo.annotation
    origin = typing.get_origin(ann)
    if origin is typing.Union:  # Optional[X]
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        ann = args[0] if args else ann
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if origin in (list, typing.List):
        inner = typing.get_args(ann)[0] if typing.get_args(ann) else str
        return "intlist" if inner is int else "strlist"
    if origin is dict:
        return "map"
    if typing.get_origin(ann) is typing.Literal:
        return "enum"
    if _is_basemodel(ann):
        return "model"
    return "str"


def _field_desc(parent: str, fname: str, finfo, dotted_parent: str) -> dict:
    dotted = fname if dotted_parent == "general" else f"{dotted_parent}.{fname}"
    ftype = _python_type(finfo)
    enum = list(typing.get_args(finfo.annotation)) if ftype == "enum" else None
    try:
        default = finfo.get_default()
    except Exception:
        default = None
    if default is PydanticUndefined:
        default = None
    meta = FIELD_META.get(dotted)
    if meta:
        label, hint, multiline = meta
    else:
        label = fname.replace("_", " ").title()
        hint = ""
        multiline = "template" in fname or fname == "text"
    return {
        "key": dotted,
        "name": fname,
        "type": ftype,
        "enum": enum,
        "default": default,
        "label": label,
        "hint": hint,
        "multiline": multiline,
        # 有硬上限的字段（目前只有系统提示词）带上上限，表单据此显示「已用 x / N 字」
        "max_chars": FIELD_LIMITS.get(dotted, 0),
        # 密码 / cookie / api_key 类字段在表单里用 password 输入框，避免旁窥
        "secret": any(w in fname for w in ("password", "cookie", "api_key", "secret")),
        # 进程级唯一的项（日志 / 缓存 / 网易云账号 / 字体 / 生效群号…）不能按群覆盖，
        # 前端在「按群」视图里要把它们灰掉，并注明「始终取全局值」
        "global_only": not is_group_scopable(dotted),
    }


def _collect_section_fields(section_key: str, model, prefix: str = "") -> list[dict]:
    """把某个配置段拍平成字段列表，**递归展开嵌套子模型**。

    ``window.weekly.start`` 这类三级键过去因为只展开一层而从未出现在表单里
    （``FIELD_META`` 里早就写好了它们的文案，只是没人渲染），结果真正决定收集
    时间的「每周·开始/汇总/结束/归档」在网页端改不了。按群覆盖窗口也需要它们。
    """
    out: list[dict] = []
    for sname, sinfo in model.model_fields.items():
        if _is_basemodel(sinfo.annotation):
            sub = f"{prefix}.{sname}" if prefix else sname
            out.extend(_collect_section_fields(section_key, sinfo.annotation, sub))
            continue
        dotted_parent = f"{section_key}.{prefix}" if prefix else section_key
        out.append(_field_desc(section_key, sname, sinfo, dotted_parent))
    return out


def build_schema() -> list[dict]:
    """由 ``AppConfig`` 自省出分组表单 schema。

    返回 ``[{key, title, fields:[...]}]``，每个 field 含 dotted key / 类型 /
    枚举选项 / 默认值 / 展示文案。新增配置项时无需改这里。

    分组顺序由 ``SECTION_ORDER`` 决定（网易云登录排到通用设置之后）。
    """
    sections: list[dict] = []
    general: list[dict] = []
    for fname, finfo in AppConfig.model_fields.items():
        if _is_basemodel(finfo.annotation):
            continue
        general.append(_field_desc("general", fname, finfo, "general"))
    if general:
        sections.append({"key": "general", "title": SECTION_TITLES["general"], "fields": general})

    for fname, finfo in AppConfig.model_fields.items():
        tp = finfo.annotation
        if not _is_basemodel(tp):
            continue
        fields = _collect_section_fields(fname, tp)
        sections.append({"key": fname, "title": SECTION_TITLES.get(fname, fname), "fields": fields})

    order = {key: i for i, key in enumerate(SECTION_ORDER)}
    sections.sort(key=lambda s: order.get(s["key"], len(order)))
    return sections


SCHEMA = build_schema()
KEY_INDEX: dict[str, dict] = {f["key"]: f for s in SCHEMA for f in s["fields"]}

#: ``llm`` / ``memory`` / ``persona`` 三段单独挂在「机器人」页，**不再出现在配置页**——
#: 避免同一个开关在两处都能改、改完互相覆盖。KEY_INDEX 仍然包含它们，所以 PATCH 接口
#: 照常能写。记忆跟大模型是一体的：没有模型就没有整理，分两页反而更难理解。
#: 人设修订同理——它动的就是 ``llm.system_prompt``，跟「机器人设置」放一页最省心。
BOT_SECTIONS = ("llm", "memory", "persona")
BOT_SCHEMA = [s for s in SCHEMA if s["key"] in BOT_SECTIONS]
CONFIG_SCHEMA = [s for s in SCHEMA if s["key"] not in BOT_SECTIONS]


# -------------------------------------------------------------------- 值转换

def coerce_value(ftype: str, enum_options, raw: object) -> object:
    """把 UI 提交的原始值按字段类型转成 Python 值，非法值抛 ``ValueError``。"""
    if ftype == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if ftype == "int":
        return int(raw)
    if ftype == "float":
        return float(raw)
    if ftype == "enum":
        val = str(raw)
        if enum_options and val not in enum_options:
            raise ValueError(f"取值必须是 {enum_options} 之一")
        return val
    if ftype == "intlist":
        items = [x.strip() for x in str(raw).split(",") if x.strip() != ""]
        return [int(x) for x in items]
    if ftype == "strlist":
        return [x.strip() for x in str(raw).split(",") if x.strip() != ""]
    if ftype == "map":
        # 接受 dict（前端直接传对象）或「k=v」多行文本（独立编辑页用）
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        out: dict[str, str] = {}
        for line in str(raw).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
        return out
    return str(raw)


def current_values(group_id: typing.Optional[int] = None) -> dict[str, object]:
    """把配置拍平成 dotted_key -> 值 的字典。

    ``group_id`` 非空时返回该群的**生效值**（全局默认叠加该群覆盖），这样配置页
    切到某个群时看到的每一格都是真正会生效的值。
    """

    def _walk(node, prefix):
        out = {}
        if isinstance(node, dict):
            for k, v in node.items():
                nk = f"{prefix}.{k}" if prefix else k
                # map 类型（如分享者昵称映射）整体作为一个值，不继续下钻，
                # 否则会把映射里每个昵称当成独立配置项拍平。
                if nk in KEY_INDEX and KEY_INDEX[nk]["type"] == "map":
                    out[nk] = v
                elif isinstance(v, dict):
                    out.update(_walk(v, nk))
                else:
                    out[nk] = v
        return out

    base = config_manager.config if group_id is None else effective_config(group_id)
    return _walk(base.model_dump(mode="json"), "")


#: 改到这些前缀的配置后必须重建定时任务。定时任务里有一类是「注册时就把下次时刻
#: 算死了」（主动搭话用 ``date`` 触发器、群记忆用 ``interval``），光改配置不重建，
#: 新值要等旧的那次跑完、甚至要等下次重启才生效。
_RELOAD_PREFIXES = (
    "window",
    "llm.banter",
    "memory.digest_hours",
    "memory.style_hours",
    "persona.propose_hours",
)


def needs_job_reload(values: dict[str, object]) -> bool:
    """这批改动里有没有影响定时任务的。

    注意要按**前缀**判断，不能只比对 ``llm.banter`` 这一个键——用户改的通常
    ``llm.banter`` 是 ``banter_minutes`` / ``banter_quiet`` 这些，「开关没动」就
    不重排，表现成「改了间隔没反应」。
    """
    return any(str(k).startswith(_RELOAD_PREFIXES) for k in values)


def apply_updates(
    values: dict[str, object], group_id: typing.Optional[int] = None
) -> tuple[bool, dict[str, str]]:
    """原子地应用一批配置更新；任一失败整体回滚，避免写到一半。

    ``group_id`` 非空时写进该群的覆盖层（进程级配置项仍会自动落到全局）。

    返回 ``(ok, errors)``。成功后若涉及时间窗口、主动搭话或整理间隔，
    重新注册定时任务（见 ``needs_job_reload``）。
    """
    if not values:
        return True, {}
    snapshot = config_manager.config.model_dump(mode="json")
    over_snapshot = {g: dict(v) for g, v in group_overrides._data.items()}
    errors: dict[str, str] = {}
    for key, val in values.items():
        try:
            apply_config_value(group_id, key, val)
        except Exception as exc:  # noqa: BLE001 — 配置写入失败需反馈给用户
            errors[key] = str(exc)
    if errors:
        # 回滚到更新前的状态（回滚写盘失败也不能抛 500，否则用户只会看到
        # 「Internal Server Error」而看不到真实的错误原因）
        config_manager._config = AppConfig.model_validate(snapshot)
        group_overrides._data = over_snapshot
        try:
            config_manager.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] 配置回滚写盘失败（配置已还原内存态）: {exc}")
        try:
            group_overrides.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] 按群配置回滚写盘失败: {exc}")
        return False, errors

    # 时间窗口（全局的或某个群单独的）改了都要重建定时任务，否则新时刻不生效。
    # 主动搭话同理：它是「注册了才有任务、注册时算死下次时刻」的，改间隔/勿扰/
    # 开关任何一项都得重排，否则要等旧的那次跑完才看得到新节奏。
    if needs_job_reload(values):
        try:
            reload_jobs()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] WebUI 更新后重载定时失败: {exc}")
    if any(k.startswith("logs.") for k in values):
        try:
            logbuffer.configure_from(config_manager.config.logs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] 更新后重配日志缓冲失败: {exc}")
    return True, {}


# -------------------------------------------------------------------- 鉴权

_TOKEN = ""


def _token_ok(request: Request) -> bool:
    if not _TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {_TOKEN}":
        return True
    if request.query_params.get("token") == _TOKEN:
        return True
    return False


# -------------------------------------------------------------------- 管理员 / 网易云账号

def read_superusers() -> list[str]:
    """从 .env 读取 SUPERUSERS（nonebot 启动时读取，改完需重启生效）。"""
    if not _ENV_PATH.is_file():
        return []
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("SUPERUSERS") and "=" in s:
            raw = s.split("=", 1)[1].strip()
            try:
                val = ast.literal_eval(raw)
            except Exception:
                return []
            return [str(x) for x in val]
    return []


def write_superusers(ids: list[str]) -> None:
    """把 SUPERUSERS 写回 .env（仅替换该行，保留其它配置）；不存在则追加。"""
    ids = [str(x).strip() for x in ids if str(x).strip()]
    line = "SUPERUSERS=[" + ",".join(ids) + "]"
    text = _ENV_PATH.read_text(encoding="utf-8") if _ENV_PATH.is_file() else ""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.strip().startswith("SUPERUSERS"):
            lines[i] = line
            break
    else:
        lines.append(line)
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def netease_account_status() -> dict:
    """网易云登录态快照：是否已登录 / 凭证是否有效 / 昵称与 userId。"""
    valid = False
    try:
        valid = await service.netease.session_valid()
    except Exception:
        valid = False
    profile = None
    if valid:
        try:
            profile = await service.netease.login_status()
        except Exception:
            profile = None
    return {
        "logged_in": service.netease.logged_in,
        "valid": valid,
        "nickname": (profile or {}).get("nickname") if profile else None,
        "userId": (profile or {}).get("userId") if profile else None,
        # MUSIC_U 的短指纹：点完「重新登录」后对比它就能看出 cookie 到底换没换
        "cookie_fp": _cookie_fingerprint(),
        # 最近一次账密登录撞到风控（code=8810）时网易云下发的人工验证链接。
        # 前端把它渲染成可点链接，操作员在浏览器过一遍验证即可放行本机 IP。
        "risk_url": _risk_url(),
    }


def _cookie_fingerprint() -> str:
    """取网易云 cookie 指纹（旧客户端对象没有该属性时退回空串）。"""
    try:
        return service.netease.cookie_fingerprint
    except Exception:
        return ""


def _risk_url() -> str:
    """取「账密登录被风控」时网易云给的人工验证链接（无则空串）。"""
    try:
        return service.netease.last_risk_url or ""
    except Exception:
        return ""


# -------------------------------------------------------------------- 路由

async def _dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


async def _aliases_page() -> HTMLResponse:
    return HTMLResponse(ALIASES_HTML)


def _group_id_param(request: Request) -> typing.Optional[int]:
    """从查询参数读 ``group_id``；缺省 / 非法 / ``"global"`` 都视为全局默认层。"""
    raw = request.query_params.get("group_id")
    if raw is None or raw == "" or raw == "global":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _api_schema(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    return JSONResponse(SCHEMA)


async def _api_config(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    gid = _group_id_param(request)
    return JSONResponse({
        "values": current_values(gid),
        "schema": CONFIG_SCHEMA,
        "group_id": gid,
        #: 该群被单独覆盖过的键（前端据此高亮 + 提供「恢复继承」）
        "overridden": group_overrides.overridden_keys(gid) if gid is not None else [],
        "groups": await service.known_groups(),
    })


async def _api_groups(request: Request):
    """按群配置的下拉数据源：有哪些群 + 各自覆盖了哪些键。"""
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    gids = await service.known_groups()
    return JSONResponse({
        "groups": gids,
        "overrides": {str(g): group_overrides.overridden_keys(g) for g in gids},
    })


async def _api_patch(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "errors": {"_": "请求体不是合法 JSON"}}, status_code=400)

    gid = body.get("group_id")
    if gid is not None:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "errors": {"_": "group_id 非法"}}, status_code=400)

    # 「恢复继承全局默认」：只对按群层有意义
    reset = body.get("reset")
    if reset:
        if gid is None:
            return JSONResponse({"ok": False, "errors": {"_": "全局层没有可恢复的覆盖"}}, status_code=400)
        if not isinstance(reset, list):
            return JSONResponse({"ok": False, "errors": {"_": "reset 必须是数组"}}, status_code=400)
        changed = False
        for key in reset:
            try:
                changed = reset_config_value(gid, str(key)) or changed
            except Exception as exc:  # noqa: BLE001
                return JSONResponse({"ok": False, "errors": {str(key): str(exc)}}, status_code=400)
        if changed and any(str(k).startswith("window") for k in reset):
            try:
                reload_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[music] 取消窗口覆盖后重载定时失败: {exc}")
        if not body.get("values"):
            return JSONResponse({"ok": True})

    values: dict = {}
    if isinstance(body.get("values"), dict):
        values = body["values"]
    elif "key" in body and "value" in body:
        values = {body["key"]: body["value"]}
    if not values:
        return JSONResponse({"ok": False, "errors": {"_": "没有可保存的字段"}}, status_code=400)

    coerced: dict[str, object] = {}
    perr: dict[str, str] = {}
    for k, raw in values.items():
        desc = KEY_INDEX.get(k)
        if not desc:
            perr[k] = "未知配置项"
            continue
        # 进程级唯一的项在按群视图里被前端灰掉了，但接口也要兜住：
        # 直接落到全局，避免存下一个永远不会生效的覆盖值
        try:
            value = coerce_value(desc["type"], desc.get("enum"), raw)
            # extra_body 是手写 JSON：保存时就校验，别等群里 @ 了才发现机器人坏了
            if k == "llm.extra_body":
                parse_extra_body(str(value))
            coerced[k] = value
        except Exception as exc:
            perr[k] = str(exc)
    if perr:
        return JSONResponse({"ok": False, "errors": perr}, status_code=400)

    ok, errs = apply_updates(coerced, group_id=gid)
    if not ok:
        return JSONResponse({"ok": False, "errors": errs}, status_code=400)
    return JSONResponse({
        "ok": True,
        "group_id": gid,
        "overridden": group_overrides.overridden_keys(gid) if gid is not None else [],
    })


async def _api_status(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    gid = _group_id_param(request)
    st = service.current_window(gid)
    cfg = service.cfg(gid)
    return JSONResponse({
        "window_label": st.label,
        "collecting": st.collecting,
        "collect_override": cfg.collect_override,
        "next_runs": next_runs(),
        "group_id": gid,
    })


async def _api_admin(request: Request):
    """读取 / 修改管理员（SUPERUSERS）。改完需重启 bot 生效。"""
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    if request.method == "GET":
        return JSONResponse({
            "superusers": read_superusers(),
            "note": "修改后需重启 bot 才能生效（nonebot 在启动时读取 SUPERUSERS）",
        })
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    ids = body.get("superusers")
    if not isinstance(ids, list):
        return JSONResponse({"ok": False, "message": "superusers 必须是数组"}, status_code=400)
    try:
        write_superusers(ids)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"写入失败: {exc}"}, status_code=400)
    return JSONResponse({
        "ok": True,
        "superusers": read_superusers(),
        "note": "已写入 .env，需重启 bot 生效",
    })


async def _api_account(request: Request):
    """网易云账号：GET 查登录态，POST {action:login|logout} 登录/退出。"""
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    if request.method == "GET":
        return JSONResponse(await netease_account_status())
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    action = body.get("action")
    if action == "login":
        cookie = (body.get("cookie") or "").strip()
        if not cookie:
            return JSONResponse({"ok": False, "message": "未提供 MUSIC_U"}, status_code=400)
        if "MUSIC_U" not in cookie:
            cookie = f"MUSIC_U={cookie}"
        service.netease.set_cookie_string(cookie)
        return JSONResponse({"ok": True, **await netease_account_status()})
    if action == "logout":
        service.netease.clear_session()
        return JSONResponse({"ok": True, **await netease_account_status()})
    if action == "relogin":
        # 手动触发：优先**账密换一套全新 cookie**（fresh=True）。
        # 不先走「原地续期」——实测（2026-10-10 线上）续期返回 code=200 却
        # **不下发新 cookie**（指纹前后一模一样），拿它当「重新登录」是自欺欺人。
        # 忽略冷却，方便排查时连点。
        before = _cookie_fingerprint()
        ok = await service.netease.ensure_logged_in(
            force=True, ignore_cooldown=True, fresh=True
        )
        after = _cookie_fingerprint()
        if ok and after and after != before:
            note = f"重新登录成功，cookie 已刷新（{before} → {after}）"
        elif ok:
            note = (
                f"登录态可用，但 cookie 没有变化（{after or '未知'}）。"
                "本环境账密登录可能被网易云判为风险网络（code=8810 网络环境存在安全风险），"
                "此时只有「续期」会成功、换不掉 cookie；"
                "若确实要换新 cookie，请在浏览器登录网易云后复制 MUSIC_U 粘贴保存。"
            )
        else:
            note = (
                "重新登录未成功：请检查 netease.phone / password（或 password_md5），"
                "或直接粘贴新的 MUSIC_U"
            )
        if _risk_url():
            note += (
                "　⚠️ 本机 IP 被网易云风控（code=8810），请先在下方「人工验证链接」"
                "的浏览器里完成验证，再点一次「重新登录」。"
            )
        return JSONResponse({"ok": bool(ok), "message": note, **await netease_account_status()})
    return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)


# -------------------------------------------------------------------- 预览 / 操作


def _song_item(song, index: int, aliases: typing.Optional[dict] = None) -> dict:
    """把一条 Song 序列化成前端展示用的字典。

    ``aliases`` 为该群的分享者昵称映射（按群配置可能不同），缺省取全局值。
    """
    if aliases is None:
        aliases = service.config.playlist.sharer_aliases
    return {
        "index": index + 1,
        "title": song.title,
        "artists": song.artists,
        "sharer_name": resolve_alias(song.sharer_name or "", song.sharer_id, aliases),
        "platform": song.platform,
        "platform_name": PLATFORM_NAMES.get(song.platform, song.platform),
        "url": song.url,
        "netease_id": song.netease_id,
        "matched": song.matched,
        "created_at": getattr(song, "created_at", None),
    }


async def build_overview(
    window_key: typing.Optional[str] = None, scope: str = "window",
    query: typing.Optional[str] = None, date: typing.Optional[str] = None,
) -> dict:
    """汇总当前收集情况：每个群收集了哪些歌，按窗口分桶。

    返回纯字典，便于前端渲染与单测。``window_key`` 省略时使用当前窗口。
    ``scope="master"`` 时改为汇总各群的**总库**（跨窗口去重后的群级歌曲库）。
    ``query`` / ``date`` 为可选的搜索条件，用于 WebUI 在库内检索歌曲；命中为空
    的群在搜索时直接隐藏，避免无意义的空卡片。
    """
    from .store import MASTER_KEY

    filtering = bool(query or date)
    state = service.current_window()
    if scope == "master":
        # 总库视角：每个有总库歌曲的群，列出其总库内容与已建的总库歌单链接
        gids = await service.store.groups_in_window(MASTER_KEY)
        groups: list[dict] = []
        for gid in gids:
            songs = await service.store.search_songs(
                gid, MASTER_KEY, query or "", date, newest_first=True
            )
            if filtering and not songs:
                continue
            arch = await service.store.get_archive(gid, MASTER_KEY)
            aliases = service.cfg(gid).playlist.sharer_aliases
            groups.append({
                "group_id": gid,
                "count": len(songs),
                "playlist_url": (arch or {}).get("playlist_url"),
                "songs": [_song_item(s, i, aliases) for i, s in enumerate(songs)],
            })
        import_groups = set(await service.store.distinct_group_ids())
        import_groups |= set(service.config.groups or [])
        import_history = await service.store.list_imports(
            window_key=MASTER_KEY, limit=20
        )
        return {
            "window": {"key": MASTER_KEY, "label": "总库", "collecting": True},
            "selected_window": MASTER_KEY,
            "scope": "master",
            "netease_logged_in": service.netease.logged_in,
            "windows": [],
            "groups": groups,
            "import_groups": sorted(import_groups),
            "import_history": import_history,
        }

    wk = window_key or state.key
    windows = await service.windows_with_counts()
    # 当前/选中窗口即使还没有收集记录，也要出现在下拉框里，避免切不到
    seen = {k for k, _ in windows}
    if wk not in seen:
        windows = [(wk, 0)] + list(windows)
    if service.config.groups:
        gids = list(service.config.groups)
    else:
        gids = await service.store.groups_in_window(wk)
    groups = []
    for gid in gids:
        songs = await service.store.search_songs(gid, wk, query or "", date)
        if filtering and not songs:
            continue
        arch = await service.store.get_archive(gid, wk)
        aliases = service.cfg(gid).playlist.sharer_aliases
        groups.append({
            "group_id": gid,
            "count": len(songs),
            "playlist_url": (arch or {}).get("playlist_url"),
            "songs": [_song_item(s, i, aliases) for i, s in enumerate(songs)],
        })
    return {
        "window": {"key": state.key, "label": state.label, "collecting": state.collecting},
        "selected_window": wk,
        "scope": "window",
        "netease_logged_in": service.netease.logged_in,
        "windows": [{"key": k, "count": n} for k, n in windows],
        "groups": groups,
    }


async def dispatch_action(body: dict) -> dict:
    """处理一次实时操作，返回 ``{"ok", "message", "data?"}`` 结构。

    所有写库 / 调网易云的操作都从这里进出，便于单测时塞入假 service。
    """
    action = body.get("action")
    # 兼容旧前端按钮的简写（pname/pdesc/del），统一映射到规范名
    action = {
        "pname": "preview_name",
        "pdesc": "preview_desc",
        "del": "delete",
    }.get(action, action)
    try:
        wk = body.get("window_key")
        if action in ("start", "stop", "auto"):
            value = {"start": "on", "stop": "off", "auto": "auto"}[action]
            note = service.set_collect_override(value, body.get("group_id"))
            return {"ok": True, "message": note}

        if action == "master_aggregate":
            gid = int(body.get("group_id"))
            n = await service.aggregate_to_master(gid)
            return {"ok": True, "message": f"已把 {n} 首历史歌曲汇总进总库"}

        if action == "aggregate_window_to_master":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            n = await service.aggregate_window_to_master(gid, wk)
            return {"ok": True, "message": f"已把当前窗口 {n} 首汇总进总库"}

        if action == "archive":
            gid = int(body.get("group_id"))
            name = (body.get("name_override") or "").strip()
            if wk == MASTER_KEY:
                rep = await service.run_master_archive(gid, name_override=name)
            else:
                rep = await service.run_archive(gid, name_override=name)
            if rep.ok:
                return {
                    "ok": True,
                    "message": f"已归档 {rep.added} 首 → {rep.playlist_url or rep.playlist_id}",
                    "data": {"playlist_url": rep.playlist_url},
                }
            return {"ok": False, "message": rep.message or "归档失败"}

        if action == "archive_all":
            wk = service.current_window().key
            gids = await service.target_groups(wk)
            results: list[str] = []
            ok_all = True
            for gid in gids:
                rep = await service.run_archive(gid)
                ok_all = ok_all and rep.ok
                results.append(f"群{gid}:{'✓'+str(rep.added)+'首' if rep.ok else '✗'+(rep.message or '失败')}")
            return {"ok": ok_all, "message": "; ".join(results) or "当前窗口无群需归档"}

        if action == "delete":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            indices = [int(x) for x in body.get("indices", [])]
            if not indices:
                return {"ok": False, "message": "未选择要删除的歌曲"}
            n = await service.clear_indices(gid, wk, indices)
            return {"ok": True, "message": f"已删除 {n} 首"}

        if action == "clear":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            n = await service.clear_window(gid, wk)
            return {"ok": True, "message": f"已清空 {n} 首"}

        if action == "preview":
            gid = int(body.get("group_id"))
            if wk == MASTER_KEY:
                name = await service.preview_master_name(gid)
                desc = await service.preview_master_description(gid)
                songs = await service.store.list_songs(gid, MASTER_KEY)
                return {
                    "ok": True,
                    "message": "预览已生成",
                    "data": {
                        "window_key": MASTER_KEY,
                        "window_label": "总库",
                        "name": name,
                        "description": desc,
                        "songs": [_song_item(s, i, service.cfg(gid).playlist.sharer_aliases)
                                  for i, s in enumerate(songs)],
                    },
                }
            state = service.current_window(gid)
            name = await service.preview_playlist_name(gid)
            desc = await service.rebuild_description(gid)
            songs = await service.store.list_songs(gid, state.key)
            return {
                "ok": True,
                "message": "预览已生成",
                "data": {
                    "window_key": state.key,
                    "window_label": state.label,
                    "name": name,
                    "description": desc,
                    "songs": [_song_item(s, i, service.cfg(gid).playlist.sharer_aliases)
                                  for i, s in enumerate(songs)],
                },
            }

        if action == "preview_name":
            gid = int(body.get("group_id"))
            if wk == MASTER_KEY:
                name = await service.preview_master_name(gid)
            else:
                name = await service.preview_playlist_name(gid)
            return {"ok": True, "message": "歌单名预览已生成", "data": {"name": name}}

        if action == "preview_desc":
            gid = int(body.get("group_id"))
            if wk == MASTER_KEY:
                desc = await service.preview_master_description(gid)
            else:
                desc = await service.rebuild_description(gid)
            return {"ok": True, "message": "简介预览已生成", "data": {"description": desc}}

        # ---- 网页端收集管理（需求 1）----
        if action == "add_song":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            return await service.manual_add_song(gid, wk, body.get("song", {}))

        if action == "edit_song":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            idx = int(body.get("index"))
            return await service.edit_song(gid, wk, idx, body.get("fields", {}))

        if action == "match":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            idx = int(body.get("index"))
            return await service.match_song(gid, wk, idx, body.get("link") or "")

        if action == "reorder":
            gid = int(body.get("group_id"))
            wk = (body.get("window_key") or service.current_window().key)
            indices = [int(x) for x in body.get("ordered_indices", [])]
            return await service.reorder_songs(gid, wk, indices)

        if action == "sync":
            gid = int(body.get("group_id"))
            if wk == MASTER_KEY:
                return await service.sync_master_playlist(gid)
            return await service.sync_playlist(gid)

        if action == "import_playlist_master":
            gid = int(body.get("group_id"))
            url = (body.get("url") or "").strip()
            if not url:
                return {"ok": False, "message": "请输入网易云歌单链接"}
            return await service.import_playlist_to_master(gid, url)

        if action == "undo_master_import":
            gid = int(body.get("group_id"))
            raw = body.get("history_id")
            hid = int(raw) if raw else None
            return await service.undo_master_import(gid, hid)

        return {"ok": False, "message": f"未知操作: {action}"}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "message": f"参数错误: {exc}"}


async def _api_overview(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    wk = request.query_params.get("window_key") or None
    scope = request.query_params.get("scope") or "window"
    query = request.query_params.get("q") or None
    date = request.query_params.get("date") or None
    return JSONResponse(await build_overview(wk, scope=scope, query=query, date=date))


async def _api_action(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict) or "action" not in body:
        return JSONResponse({"ok": False, "message": "缺少 action 字段"}, status_code=400)
    res = await dispatch_action(body)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


async def _api_logs(request: Request):
    """运行日志：GET 取（按等级 / 关键词过滤），POST 清空内存缓冲。

    数据源是进程内的环形缓冲（见 ``logbuffer``），所以只含**本次启动后**的日志。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = (body or {}).get("action") or "clear"
        if action != "clear":
            return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)
        removed = logbuffer.clear()
        return JSONResponse(
            {"ok": True, "message": f"已清空 {removed} 条日志", "removed": removed, **logbuffer.stats()}
        )

    qp = request.query_params
    raw_limit = qp.get("limit")
    try:
        limit = int(raw_limit) if raw_limit else None
    except (TypeError, ValueError):
        limit = None
    return JSONResponse({
        "ok": True,
        "logs": logbuffer.snapshot(
            level=qp.get("level"),
            query=qp.get("q"),
            limit=limit,
            source=qp.get("source"),
        ),
        "stats": logbuffer.stats(),
        "levels": list(logbuffer.LEVELS),
        "sources": logbuffer.sources(),
        "bot_logger": logbuffer.BOT_LOGGER,
    })


async def _api_llm_trace(request: Request):
    """「LLM 调用」记录：每次问答的**输入 / 经过 / 输出**。

    数据源是 :mod:`llmtrace`（内存环形缓冲，只含本次启动后）。日志页「来源」
    下拉里的 ``🧠 LLM 调用`` 走的就是这里——那页的普通日志是文本行，一次问答
    摊成十几行会淹没在 nonebot / uvicorn 的刷屏里，看不出「到底问了什么、回了什么」。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = (body or {}).get("action") or "clear"
        if action != "clear":
            return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)
        removed = llmtrace_mod.clear()
        return JSONResponse({
            "ok": True,
            "message": f"已清空 {removed} 条调用记录",
            "removed": removed,
            **llmtrace_mod.stats(),
        })

    qp = request.query_params

    def _int(key: str, default: int) -> int:
        try:
            return int(qp.get(key) or default)
        except (TypeError, ValueError):
            return default

    limit = max(1, min(500, _int("limit", 100)))
    return JSONResponse({
        "ok": True,
        "items": llmtrace_mod.snapshot(
            limit=limit,
            query=qp.get("q") or "",
            group_id=_int("group_id", 0),
            only_failed=str(qp.get("failed") or "") in ("1", "true", "yes"),
        ),
        "stats": llmtrace_mod.stats(),
        # 顺带把日志那边的基础信息带上：前端在两种来源间来回切时，
        # 「级别 / 来源」两个下拉不该因为换了接口就变空
        "levels": list(logbuffer.LEVELS),
        "sources": logbuffer.sources(),
        "bot_logger": logbuffer.BOT_LOGGER,
    })


# -------------------------------------------------------------------- 群记忆


def _memory_payload(person, full: bool = False) -> dict:
    """一份档案的 JSON 形态。列表页只要摘要，详情页才带全文。"""
    data = {
        "uid": person.uid,
        "nick": person.nick,
        "display": person.display,
        "aliases": list(person.aliases),
        "first_seen": person.first_seen,
        "last_seen": person.last_seen,
        "msgs": int(person.msgs),
        "notes": len(person.notes()),
        "chars": len(person.body),
        "summary": person.summary(300),
    }
    if full:
        data["body"] = person.body
    return data


async def _api_memory(request: Request):
    """群记忆：GET 列表 / 单人文档，POST 保存 / 删除 / 改群概况 / 立即整理。

    一个路由担四件事，是跟着这个 WebUI 里 ``/action`` 的既有风格——前端只用记
    一个地址，鉴权也只有一处。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")

    if request.method == "GET":
        try:
            gid = int(request.query_params.get("group") or 0)
        except (TypeError, ValueError):
            gid = 0
        if gid <= 0:
            # 不传群号 = 列一下「哪些群已经有记忆」，前端据此填下拉
            mem0 = service.config.memory
            ids = set(memory_mod.list_groups(mem0))
            try:
                ids |= set(await service.store.chat_groups())
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({
                "ok": True,
                "root": str(memory_mod.memory_root(mem0)),
                # 整批群都没选时也把「下次整理」给出来：页面上先看到节奏，才好判断
                # 「怎么半天没更新」是没到点还是坏了
                "next_digest": _fmt_when(next_run_of("memory_digest")),
                "groups": [
                    {"group_id": g, **memory_mod.stats(mem0, g)} for g in sorted(ids)
                ],
            })
        mem = service.cfg(gid).memory
        uid = (request.query_params.get("uid") or "").strip()
        if uid:
            person = memory_mod.load_person(mem, gid, uid)
            if person is None:
                return JSONResponse(
                    {"ok": False, "message": "没有这份档案"}, status_code=404
                )
            return JSONResponse({"ok": True, "person": _memory_payload(person, full=True)})
        people = memory_mod.list_people(mem, gid)
        return JSONResponse({
            "ok": True,
            "enabled": mem.enabled,
            "people": [_memory_payload(p) for p in people],
            "group_note": memory_mod.read_group_note(mem, gid),
            "stats": memory_mod.stats(mem, gid),
            "log": await service.store.chat_log_stats(gid),
            # 下次自动整理的时刻。它是 interval 任务（每 digest_hours 小时），
            # 光写「每 6 小时」看不出下一个点在哪，页面上直接给出来最省事
            "next_digest": _fmt_when(next_run_of("memory_digest")),
            "style": {
                "body": style_mod.read_style(mem, gid),
                **style_mod.stats(mem, gid),
                "next": _fmt_when(next_run_of("style")),
            },
            "limits": {
                "note_chars": int(mem.note_chars),
                "inject_chars": int(mem.inject_chars),
                "digest_hours": float(mem.digest_hours),
                "style_chars": int(mem.style_chars),
                "style_hours": float(mem.style_hours),
            },
        })

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式不对"}, status_code=400)
    action = str(body.get("action") or "save")
    try:
        gid = int(body.get("group") or 0)
    except (TypeError, ValueError):
        gid = 0
    if gid <= 0:
        return JSONResponse({"ok": False, "message": "缺少群号"}, status_code=400)
    mem = service.cfg(gid).memory

    if action == "save":
        uid = str(body.get("uid") or "").strip()
        ok, msg = memory_mod.rewrite_body(mem, gid, uid, str(body.get("body") or ""))
        nick = str(body.get("nick") or "").strip()
        if ok and nick:
            person = memory_mod.load_person(mem, gid, uid)
            if person is not None and person.nick != nick:
                person.nick = nick[:40]
                memory_mod.save_person(mem, gid, person)
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)

    if action == "delete":
        uid = str(body.get("uid") or "").strip()
        gone = memory_mod.delete_person(mem, gid, uid)
        return JSONResponse(
            {"ok": gone, "message": "已删除这份档案" if gone else "没有这份档案"},
            status_code=200 if gone else 404,
        )

    if action == "group":
        ok, msg = memory_mod.write_group_note(mem, gid, str(body.get("text") or ""))
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)

    if action == "digest":
        res = await digest_group(gid, mem=mem, force=True)
        status = res.get("status")
        if status == "ok":
            msg = (
                f"整理完成：{res.get('rows', 0)} 条消息 → "
                f"{res.get('facts', 0)} 条新记忆（{res.get('people', 0)} 人）"
            )
        elif status == "skipped":
            msg = res.get("why") or "没有新内容"
        else:
            msg = f"整理失败：{res.get('why') or '未知原因'}"
        return JSONResponse(
            {"ok": status == "ok", "message": msg, "detail": res},
            status_code=200 if status == "ok" else 400,
        )

    if action == "style":
        # 手改风格卡：rotate=False，人写的东西不必进历史版本
        ok, msg = style_mod.write_style(
            mem, gid, str(body.get("text") or ""), rotate=False
        )
        return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)

    if action == "style_distill":
        res = await style_mod.distill_group(gid, mem=mem, force=True)
        status = res.get("status")
        if status == "ok":
            msg = f"风格卡已更新：{res.get('rows', 0)} 条原话 → {res.get('chars', 0)} 字"
        elif status == "skipped":
            msg = res.get("why") or "没有新内容"
        else:
            msg = f"学习失败：{res.get('why') or '未知原因'}（旧卡保留）"
        return JSONResponse(
            {"ok": status == "ok", "message": msg, "detail": res},
            status_code=200 if status == "ok" else 400,
        )

    return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)


# -------------------------------------------------------------------- 人设修订


async def _api_persona(request: Request):
    """人设修订：GET 看抱怨与建议，POST 生成建议 / 删建议 / 清空抱怨。

    这里**没有「应用」动作**：采纳一条建议等于改 ``llm.system_prompt``，属于下一步的
    活（diff 审核 + 版本回滚）。目前只在页面上把 old/new 摆出来，人自己复制去改。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")

    if request.method == "GET":
        pcfg = service.config.persona
        rows = persona_mod.read_feedback(limit=30)
        feedback = [
            {
                "ts": _fmt_ts(row.get("ts")),
                "group_id": row.get("group_id"),
                "nick": row.get("nick") or str(row.get("uid") or ""),
                "text": row.get("text") or "",
                "reply": row.get("reply") or "",
            }
            for row in reversed(rows)
        ]
        return JSONResponse({
            "ok": True,
            "enabled": bool(pcfg.enabled),
            "detect": bool(pcfg.detect),
            "stats": persona_mod.stats(),
            "limits": {
                "min_feedback": int(pcfg.min_feedback),
                "window_minutes": float(pcfg.window_minutes),
                "propose_hours": float(pcfg.propose_hours),
                "persona_limit": int(pcfg.persona_limit),
            },
            "next_propose": _fmt_when(next_run_of("persona")),
            "persona": (service.config.llm.system_prompt or "").strip(),
            "feedback": feedback,
            "latest": persona_mod.read_latest(),
            "proposals": persona_mod.list_proposals(limit=8),
            "root": str(persona_mod.persona_root()),
        })

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "message": "请求体格式不对"}, status_code=400)
    action = str(body.get("action") or "")

    if action == "propose":
        res = await persona_mod.propose(force=True)
        status = res.get("status")
        if status == "ok":
            msg = f"生成了 {len(res.get('edits') or [])} 条修改建议，逐条看看要不要采纳"
        elif status == "skipped":
            msg = res.get("why") or "没有可提议的材料"
        else:
            msg = f"提议失败：{res.get('why') or '未知原因'}"
        return JSONResponse(
            {"ok": status == "ok", "message": msg, "detail": res},
            status_code=200 if status == "ok" else 400,
        )

    if action == "delete":
        gone = persona_mod.delete_proposal(str(body.get("name") or ""))
        return JSONResponse(
            {"ok": gone, "message": "已删除这份建议" if gone else "没有这份建议"},
            status_code=200 if gone else 404,
        )

    if action == "clear":
        try:
            path = persona_mod.feedback_path()
            if path.exists():
                path.unlink()
            return JSONResponse({"ok": True, "message": "抱怨记录已清空"})
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "message": f"清空失败：{exc}"}, status_code=400
            )

    return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)


# -------------------------------------------------------------------- 机器人（大模型）


def _thinking_status(cfg) -> dict:
    """思考设置的实际生效情况（网页端状态区与排查都用这一份）。

    抽出来是因为「档位」和「真正发出去的参数」不是一回事：DeepSeek 发送的是
    强度（low/high/max）而不是 token 预算，界面不该显示一个根本不存在的预算。
    """
    mode = (cfg.thinking or "auto").strip()
    style = resolve_thinking_style(cfg)
    return {
        "mode": mode,
        "style": style,
        "effort": (
            deepseek_effort(mode) if style == "deepseek" and mode not in ("auto", "off") else ""
        ),
        "budget": thinking_budget(cfg),
        "reasoning_model": is_reasoning_model(cfg.model),
    }


def _tools_status(cfg, mem=None) -> dict:
    """工具与技能的实际生效情况：哪些工具挂上了、扫到几个技能、抓取依赖装没装。

    只看开关不够——技能目录不存在、trafilatura 没装时，开关开着也等于没有。
    这里把**真实结果**摆出来，省得「明明开了却没动静」又要去翻日志。
    """
    names: list[str] = []
    skills: list[str] = []
    root = ""
    try:
        root = str(tools_mod.skills_root(cfg))
        names = [t.name for t in tools_mod.build(cfg, 0, 0, "", mem)[0]]
        if cfg.skills:
            skills = [s.name for s in skills_mod.discover(tools_mod.skills_root(cfg))]
    except Exception:  # noqa: BLE001 — 状态接口不该因为一个坏技能 500
        pass
    return {
        "enabled": bool(cfg.tools),
        "names": names,
        "rounds": int(cfg.tool_rounds),
        "skills": skills,
        "skills_dir": root,
        "scripts": bool(cfg.skill_scripts),
        "fetch_ready": webtool.available(),
        "memory_tools": [n for n in names if n in ("remember", "recall", "forget")],
    }


async def _api_bot(request: Request):
    """「机器人」页数据：llm 段的字段 + 当前值 + 运行状态。

    只走全局层——``llm`` 段本来就是进程级配置（见 config.GLOBAL_ONLY_PREFIXES），
    不存在「某个群单独一套模型」这种组合。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    app_cfg = effective_config(None)
    cfg = app_cfg.llm
    mem = app_cfg.memory
    values = {
        k: v
        for k, v in current_values(None).items()
        if k.startswith("llm.") or k.startswith("memory.") or k.startswith("persona.")
    }
    return JSONResponse({
        "schema": BOT_SCHEMA,
        "values": values,
        "groups": await service.known_groups(),
        "status": {
            "enabled": cfg.enabled,
            # 「填全了没」：三项必填，缺一项就算没配置好（此时 @ 也不会走大模型）
            "ready": bool(
                cfg.base_url.strip() and cfg.api_key.strip() and cfg.model.strip()
            ),
            "model": cfg.model,
            "endpoint": normalize_endpoint(cfg.base_url),
            "groups": cfg.groups,
            # 上下文只在内存里：重启清零，这里顺带把「哪个群还留着几轮 / 多少字」摆出来
            "history": {str(g): n for g, n in llm_bot.history_sizes().items()},
            "history_stats": {str(g): v for g, v in llm_bot.history_stats().items()},
            "history_chars_limit": cfg.history_chars,
            "last_error": llm_bot.last_error,
            # 思考模式：档位 + 实际采用的写法 + 真实强度/预算（可能被 max_tokens 压过）
            "thinking": _thinking_status(cfg),
            # 工具与技能：真的挂上了哪些、扫到几个技能、抓取依赖装没装
            "tools": _tools_status(cfg, mem),
            # 群记忆：开没开、醒了多少人、攒了多少群聊（重启后上下文从这张表恢复）
            "memory": {
                "enabled": bool(mem.enabled),
                "listen": bool(mem.listen),
                "digest_hours": float(mem.digest_hours),
                "inject_chars": int(mem.inject_chars),
                "note_chars": int(mem.note_chars),
                "root": str(memory_mod.memory_root(mem)),
                "groups": {
                    str(g): memory_mod.stats(mem, g)
                    for g in memory_mod.list_groups(mem)
                },
                "log": await service.store.chat_log_stats(),
            },
            # 「实际发出去的参数」预览：让「思考模式到底有没有生效」一眼可见
            "payload": payload_preview(cfg),
            # 主动搭话：自己排自己，``next`` 是已经排好的下一次（排查「开了没动静」用）
            "banter": {
                "enabled": bool(cfg.banter),
                "minutes": cfg.banter_minutes,
                "quiet": list(cfg.banter_quiet),
                "daily_max": int(cfg.banter_daily_max),
                "fresh_minutes": float(cfg.banter_fresh_minutes),
                "groups": list(cfg.banter_groups),
                "batch_max": int(cfg.banter_batch_max),
                "batch_chars": int(cfg.banter_batch_chars),
                # 判官：开没开、用哪个模型——「开了却没动静」多半是它一直在拦
                "judge": bool(cfg.banter_judge),
                "judge_model": (cfg.banter_judge_model or "").strip() or cfg.model,
                "next": _fmt_when(next_run_of("banter")),
            },
        },
    })


def _fmt_when(value: object) -> str:
    """把 APScheduler 的 ``next_run_time`` 渲染成 ``10-25 05:31``；没有就空串。"""
    try:
        return f"{value:%m-%d %H:%M}" if value else ""
    except Exception:  # noqa: BLE001
        return ""


def _fmt_ts(value: object) -> str:
    """Unix 时间戳 → ``10-29 22:40``（按群时区）。转不动就空串。"""
    try:
        return datetime.fromtimestamp(float(value), service.resolver.tz).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


async def _api_context(request: Request):
    """某个群「一次请求的上下文都花在哪儿」——网页那条用量条的数据源。

    ``group_id`` 缺省或为 0 时给的是**空会话基线**（没有任何历史时一条请求有多大），
    用来回答「机器人平时到底占多少」这个问题。
    """
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    raw = str(request.query_params.get("group_id") or "").strip()
    try:
        gid = int(raw) if raw else 0
    except (TypeError, ValueError):
        gid = 0
    app_cfg = effective_config(None)
    try:
        # 先把历史从库里预热进内存，否则刚重启时「对话消息」永远是 0
        if gid:
            await llm_bot.ensure_history(gid, app_cfg.llm)
        data = llm_bot.context_breakdown(gid, app_cfg.llm, app_cfg.memory)
    except Exception as exc:  # noqa: BLE001 — 统计失败不该让整页 500
        logger.warning(f"[music] 统计上下文用量失败: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    data["ok"] = True
    data["groups"] = await service.known_groups()
    return JSONResponse(data)


async def _api_bot_test(request: Request):
    """「测试连接」：按当前配置发一条最小请求，返回结果与耗时。"""
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    prompt = ""
    if isinstance(body, dict):
        prompt = str(body.get("prompt") or "").strip()
    cfg = effective_config(None).llm
    ok, message = await llm_bot.test(cfg, prompt or "用一句话介绍你自己")
    # 日志由 LLMBot.test 自己记（成功 INFO / 失败 WARNING，含 model 与耗时），
    # 这里不再重复记一条，否则每次测试连接都会出两行。
    return JSONResponse({"ok": ok, "message": message})


async def _api_bot_history(request: Request):
    """清空对话上下文。body 带 ``group_id`` 只清该群，不带则清全部。"""
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    gid = None
    if isinstance(body, dict) and body.get("group_id") not in (None, "", "global"):
        try:
            gid = int(body["group_id"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "message": "group_id 非法"}, status_code=400)
    n = llm_bot.clear_history(gid)
    where = f"群 {gid}" if gid is not None else "全部群"
    return JSONResponse({"ok": True, "message": f"已清空{where}的对话上下文（{n} 个群）"})


def register_webui() -> None:
    """在 NoneBot 的 FastAPI 应用上挂载管理界面。于插件启动钩子里调用。"""
    global _TOKEN
    # NoneBot 读 .env 不会注入 os.environ，这里补加载一次，让令牌生效。
    try:
        if _ENV_PATH.is_file():
            load_dotenv(_ENV_PATH)
    except Exception:
        pass
    _TOKEN = os.getenv("MUSIC_WEBUI_TOKEN") or ""
    if not _TOKEN:
        _TOKEN = secrets.token_hex(16)
        logger.warning(
            "[music] WebUI 未配置 MUSIC_WEBUI_TOKEN，本次已随机生成: "
            f"{_TOKEN}\n         如需固定，请在 .env 加 MUSIC_WEBUI_TOKEN=你的令牌"
        )
    try:
        app = get_app()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[music] 无法获取 FastAPI 应用，WebUI 未挂载: {exc}")
        return
    app.add_api_route("/music-admin", _dashboard, methods=["GET"])
    app.add_api_route("/music-admin/aliases", _aliases_page, methods=["GET"])
    app.add_api_route("/api/music-admin/schema", _api_schema, methods=["GET"])
    app.add_api_route("/api/music-admin/config", _api_config, methods=["GET"])
    app.add_api_route("/api/music-admin/bot", _api_bot, methods=["GET"])
    app.add_api_route("/api/music-admin/context", _api_context, methods=["GET"])
    app.add_api_route("/api/music-admin/bot/test", _api_bot_test, methods=["POST"])
    app.add_api_route("/api/music-admin/bot/history", _api_bot_history, methods=["POST"])
    app.add_api_route("/api/music-admin/config", _api_patch, methods=["PATCH"])
    app.add_api_route("/api/music-admin/groups", _api_groups, methods=["GET"])
    app.add_api_route("/api/music-admin/status", _api_status, methods=["GET"])
    app.add_api_route("/api/music-admin/overview", _api_overview, methods=["GET"])
    app.add_api_route("/api/music-admin/action", _api_action, methods=["POST"])
    app.add_api_route("/api/music-admin/admin", _api_admin, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/account", _api_account, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/logs", _api_logs, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/llm-trace", _api_llm_trace, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/memory", _api_memory, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/persona", _api_persona, methods=["GET", "POST"])
    logger.info("[music] WebUI 已挂载: http://<本机IP>:8080/music-admin  (需 token 访问)")


# -------------------------------------------------------------------- 前端

from .webui_frontend import DASHBOARD_HTML, ALIASES_HTML

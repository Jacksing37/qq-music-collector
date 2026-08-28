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

from .config import AppConfig, config_manager
from .models import PLATFORM_NAMES
from .naming import resolve_alias
from .scheduler import next_runs, reload_jobs
from .service import service

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
}

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
    "reply.accept_text": ("收录回复文案", "识别到新歌入库后回发的消息。占位符：{index}本期序号 {nick}分享者 {title}歌名 {artists}歌手 {album}专辑 {platform}来源 {url}歌曲链接 {duration}时长 {artists_line}整行歌手(无则消失) {album_line}整行专辑(无则消失) {song}详情块 {playlist}歌单(名+链接) {count}已收录数 {window}窗口文案；用 \\n 换行", True),
    "reply.playlist_empty_text": ("歌单未生成替代文案", "{playlist} 在本期还没归档时显示的替代文字", False),

    "playlist.name_template": ("歌单名模板", "占位符：{seq}期号 {slash}如26/8/7 {y}{yy}年 {m}{mm}月 {d}{dd}日 {ymd}{date}日期 {week}周数 {weekday}星期 {start}起始日 {end}结束日 {window}窗口文案 {count}收录数 {total}分享数 {sharers}人数 {group}群号", True),
    "playlist.description_template": ("简介开头模板", "后续自动接「谁分享了什么歌」清单。占位符同歌单名，另可用 {songlist}歌曲清单 {sharerlist}按人聚合清单；{group}群号 {window}窗口 {count}数", True),
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
    }


def build_schema() -> list[dict]:
    """由 ``AppConfig`` 自省出分组表单 schema。

    返回 ``[{key, title, fields:[...]}]``，每个 field 含 dotted key / 类型 /
    枚举选项 / 默认值 / 展示文案。新增配置项时无需改这里。
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
        fields = [_field_desc(fname, sname, sinfo, fname) for sname, sinfo in tp.model_fields.items()]
        sections.append({"key": fname, "title": SECTION_TITLES.get(fname, fname), "fields": fields})
    return sections


SCHEMA = build_schema()
KEY_INDEX: dict[str, dict] = {f["key"]: f for s in SCHEMA for f in s["fields"]}


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


def current_values() -> dict[str, object]:
    """把当前配置拍平成 dotted_key -> 值 的字典。"""

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

    return _walk(config_manager.config.model_dump(mode="json"), "")


def apply_updates(values: dict[str, object]) -> tuple[bool, dict[str, str]]:
    """原子地应用一批配置更新；任一失败整体回滚，避免写到一半。

    返回 ``(ok, errors)``。成功后若涉及 ``window.*``，重新注册定时任务。
    """
    if not values:
        return True, {}
    snapshot = config_manager.config.model_dump(mode="json")
    errors: dict[str, str] = {}
    for key, val in values.items():
        try:
            config_manager.update(key, val)
        except Exception as exc:  # noqa: BLE001 — 配置写入失败需反馈给用户
            errors[key] = str(exc)
    if errors:
        # 回滚到更新前的状态（回滚写盘失败也不能抛 500，否则用户只会看到
        # 「Internal Server Error」而看不到真实的错误原因）
        config_manager._config = AppConfig.model_validate(snapshot)
        try:
            config_manager.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] 配置回滚写盘失败（配置已还原内存态）: {exc}")
        return False, errors

    if any(k.startswith("window") for k in values):
        try:
            reload_jobs()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[music] WebUI 更新后重载定时失败: {exc}")
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
    }


# -------------------------------------------------------------------- 路由

async def _dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


async def _aliases_page() -> HTMLResponse:
    return HTMLResponse(ALIASES_HTML)


async def _api_schema(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    return JSONResponse(SCHEMA)


async def _api_config(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    return JSONResponse({"values": current_values(), "schema": SCHEMA})


async def _api_patch(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "errors": {"_": "请求体不是合法 JSON"}}, status_code=400)

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
        try:
            coerced[k] = coerce_value(desc["type"], desc.get("enum"), raw)
        except Exception as exc:
            perr[k] = str(exc)
    if perr:
        return JSONResponse({"ok": False, "errors": perr}, status_code=400)

    ok, errs = apply_updates(coerced)
    if not ok:
        return JSONResponse({"ok": False, "errors": errs}, status_code=400)
    return JSONResponse({"ok": True})


async def _api_status(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    st = service.current_window()
    return JSONResponse({
        "window_label": st.label,
        "collecting": st.collecting,
        "collect_override": service.config.collect_override,
        "next_runs": next_runs(),
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
    return JSONResponse({"ok": False, "message": f"未知操作: {action}"}, status_code=400)


# -------------------------------------------------------------------- 预览 / 操作


def _song_item(song, index: int) -> dict:
    """把一条 Song 序列化成前端展示用的字典。"""
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
    }


async def build_overview(window_key: typing.Optional[str] = None) -> dict:
    """汇总当前收集情况：每个群收集了哪些歌，按窗口分桶。

    返回纯字典，便于前端渲染与单测。``window_key`` 省略时使用当前窗口。
    """
    state = service.current_window()
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
    groups: list[dict] = []
    for gid in gids:
        songs = await service.store.list_songs(gid, wk)
        arch = await service.store.get_archive(gid, wk)
        groups.append({
            "group_id": gid,
            "count": len(songs),
            "playlist_url": (arch or {}).get("playlist_url"),
            "songs": [_song_item(s, i) for i, s in enumerate(songs)],
        })
    return {
        "window": {"key": state.key, "label": state.label, "collecting": state.collecting},
        "selected_window": wk,
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
        if action in ("start", "stop", "auto"):
            value = {"start": "on", "stop": "off", "auto": "auto"}[action]
            note = service.set_collect_override(value)
            return {"ok": True, "message": note}

        if action == "archive":
            gid = int(body.get("group_id"))
            name = (body.get("name_override") or "").strip()
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
            state = service.current_window()
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
                    "songs": [_song_item(s, i) for i, s in enumerate(songs)],
                },
            }

        if action == "preview_name":
            gid = int(body.get("group_id"))
            name = await service.preview_playlist_name(gid)
            return {"ok": True, "message": "歌单名预览已生成", "data": {"name": name}}

        if action == "preview_desc":
            gid = int(body.get("group_id"))
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
            return await service.sync_playlist(gid)

        return {"ok": False, "message": f"未知操作: {action}"}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "message": f"参数错误: {exc}"}


async def _api_overview(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    wk = request.query_params.get("window_key") or None
    return JSONResponse(await build_overview(wk))


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
    app.add_api_route("/api/music-admin/config", _api_patch, methods=["PATCH"])
    app.add_api_route("/api/music-admin/status", _api_status, methods=["GET"])
    app.add_api_route("/api/music-admin/overview", _api_overview, methods=["GET"])
    app.add_api_route("/api/music-admin/action", _api_action, methods=["POST"])
    app.add_api_route("/api/music-admin/admin", _api_admin, methods=["GET", "POST"])
    app.add_api_route("/api/music-admin/account", _api_account, methods=["GET", "POST"])
    logger.info("[music] WebUI 已挂载: http://<本机IP>:8080/music-admin  (需 token 访问)")


# -------------------------------------------------------------------- 前端

from .webui_frontend import DASHBOARD_HTML, ALIASES_HTML

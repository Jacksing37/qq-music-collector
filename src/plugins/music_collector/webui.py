"""配置管理 Web UI。

把 ``AppConfig`` 的 pydantic 结构自省成一份 schema，前端据此自动渲染表单——
以后在 ``config.py`` 里加字段，UI 会自动出现，无需改前端。

挂载在 NoneBot 的 FastAPI 应用上（与 OneBot 共用 8080 端口，不同路径），
用 ``MUSIC_WEBUI_TOKEN`` 做统一鉴权，避免暴露在公网时被改配置。

核心（schema 构建 / 值转换 / 原子更新）都是纯函数，方便单测，不依赖 HTTP。
"""

from __future__ import annotations

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
    "intro.text": ("自我介绍文案", "支持占位符 {nick}{count}{state}{playlist}，用 \\n 换行", True),
    "intro.cooldown": ("冷却(秒)", "同群多久内不再重复发，0=不限", False),
    "intro.at_sender": ("@提问者", "回复时是否 @ 对方", False),
    "intro.skip_commands": ("跳过命令", "消息带 /music 命令时不发自我介绍", False),
    "intro.skip_music": ("跳过音乐分享", "消息带音乐链接时不发（那是分享不是提问）", False),
    "intro.always_reply": ("始终回应", "收集关闭/不在收集期时也仍回自我介绍", False),
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
    return str(raw)


def current_values() -> dict[str, object]:
    """把当前配置拍平成 dotted_key -> 值 的字典。"""

    def _walk(node, prefix):
        out = {}
        if isinstance(node, dict):
            for k, v in node.items():
                nk = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
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
        # 回滚到更新前的状态
        config_manager._config = AppConfig.model_validate(snapshot)
        config_manager.save()
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


# -------------------------------------------------------------------- 路由

async def _dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


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


# -------------------------------------------------------------------- 预览 / 操作

PLATFORM_NAMES = {"163": "网易云", "qq": "QQ音乐", "migu": "咪咕", "kugou": "酷狗", "kuwo": "酷我"}


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
        groups.append({
            "group_id": gid,
            "count": len(songs),
            "songs": [
                {
                    "index": i + 1,
                    "title": s.title,
                    "artists": s.artists,
                    "sharer_name": s.sharer_name,
                    "platform": s.platform,
                    "platform_name": PLATFORM_NAMES.get(s.platform, s.platform),
                    "netease_id": s.netease_id,
                    "matched": s.matched,
                }
                for i, s in enumerate(songs)
            ],
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

        if action == "preview_name":
            gid = int(body.get("group_id"))
            name = await service.preview_playlist_name(gid)
            return {"ok": True, "message": "歌单名预览已生成", "data": {"name": name}}

        if action == "preview_desc":
            gid = int(body.get("group_id"))
            desc = await service.rebuild_description(gid)
            return {"ok": True, "message": "简介预览已生成", "data": {"description": desc}}

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
    app.add_api_route("/api/music-admin/schema", _api_schema, methods=["GET"])
    app.add_api_route("/api/music-admin/config", _api_config, methods=["GET"])
    app.add_api_route("/api/music-admin/config", _api_patch, methods=["PATCH"])
    app.add_api_route("/api/music-admin/status", _api_status, methods=["GET"])
    app.add_api_route("/api/music-admin/overview", _api_overview, methods=["GET"])
    app.add_api_route("/api/music-admin/action", _api_action, methods=["POST"])
    logger.info("[music] WebUI 已挂载: http://<本机IP>:8080/music-admin  (需 token 访问)")


# -------------------------------------------------------------------- 前端

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>群音乐收集 · 配置面板</title>
<style>
:root{
  --bg:#0b0f1a; --bg2:#111726; --card:rgba(255,255,255,.04); --card-bd:rgba(255,255,255,.08);
  --txt:#e8edf6; --muted:#8b97ad; --accent:#6ea8fe; --accent2:#a78bfa; --ok:#34d399; --bad:#f87171;
  --input:rgba(255,255,255,.06); --shadow:0 10px 30px rgba(0,0,0,.35);
}
[data-theme="light"]{
  --bg:#f4f6fb; --bg2:#ffffff; --card:rgba(20,30,60,.03); --card-bd:rgba(20,30,60,.1);
  --txt:#1a2233; --muted:#5b6678; --accent:#3b6fe0; --accent2:#7c5cf0; --ok:#0f9d63; --bad:#d8453b;
  --input:rgba(20,30,60,.05); --shadow:0 10px 30px rgba(20,30,60,.1);
}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:radial-gradient(1200px 600px at 80% -10%,rgba(110,168,254,.12),transparent),var(--bg);
  color:var(--txt);line-height:1.55;min-height:100vh}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 120px}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);
  background:linear-gradient(var(--bg),rgba(11,15,26,.6));padding:14px 0;margin-bottom:18px;
  border-bottom:1px solid var(--card-bd)}
[data-theme="light"] header{background:linear-gradient(#fff,rgba(255,255,255,.7))}
.hrow{display:flex;align-items:center;gap:14px}
.hrow h1{font-size:19px;margin:0;font-weight:700;letter-spacing:.3px}
.spacer{flex:1}
button{font:inherit;cursor:pointer;border:1px solid var(--card-bd);background:var(--input);color:var(--txt);
  border-radius:10px;padding:8px 14px;transition:.18s}
button:hover{border-color:var(--accent);transform:translateY(-1px)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
.status-pill{font-size:12px;padding:4px 10px;border-radius:999px;border:1px solid var(--card-bd);color:var(--muted)}
.status-pill.on{color:var(--ok);border-color:var(--ok)}
.card{background:var(--card);border:1px solid var(--card-bd);border-radius:18px;padding:18px 20px;
  margin-bottom:16px;box-shadow:var(--shadow);backdrop-filter:blur(8px)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.stat{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:12px 14px}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font-size:17px;font-weight:600;margin-top:2px}
pre.runs{margin:10px 0 0;font-size:12px;color:var(--muted);white-space:pre-wrap;font-family:ui-monospace,monospace}
section.card h2{font-size:16px;margin:0 0 14px;display:flex;align-items:center;gap:8px}
section.card h2 .dot{width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2))}
.field{display:grid;grid-template-columns:230px 1fr;gap:14px;padding:10px 0;border-top:1px dashed var(--card-bd)}
.field:first-of-type{border-top:none}
.flabel{font-size:14px}
.flabel .hint{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.fctrl input[type=text],.fctrl input[type=number],.fctrl select,textarea{
  width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);
  border-radius:10px;padding:9px 11px;font:inherit;transition:.18s}
.fctrl input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.18)}
textarea{resize:vertical;min-height:64px;font-family:ui-monospace,monospace;font-size:13px}
.fctrl.dirty input,.fctrl.dirty select,.fctrl.dirty textarea{border-color:var(--accent2)}
.fctrl label.chk{display:inline-flex;align-items:center;gap:10px;cursor:pointer;font-size:15px}
.fctrl input[type=checkbox]{width:20px;height:20px;accent-color:var(--accent)}
.footbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;align-items:center;gap:14px;
  justify-content:center;padding:14px;background:linear-gradient(transparent,var(--bg) 40%);}
.footbar .msg{font-size:13px;color:var(--muted)}
.footbar .msg.ok{color:var(--ok)}
.footbar .msg.bad{color:var(--bad)}
.footbar .count{font-size:13px;color:var(--accent2);font-weight:600}
.modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.55);backdrop-filter:blur(4px)}
.modal .box{background:var(--bg2);border:1px solid var(--card-bd);border-radius:18px;padding:26px;width:min(420px,92vw);box-shadow:var(--shadow)}
.modal h3{margin:0 0 6px}
.modal p{color:var(--muted);font-size:13px;margin:0 0 14px}
  .modal input{width:100%;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);border-radius:10px;padding:10px;font:inherit;margin-bottom:14px}
  .hidden{display:none!important}
  .ops{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 14px}
  .ops .danger{border-color:rgba(248,113,113,.5);color:var(--bad)}
  .ops button{padding:7px 12px;font-size:13px}
  .winrow{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
  .winrow select{background:var(--input);border:1px solid var(--card-bd);color:var(--txt);border-radius:10px;padding:7px 10px;font:inherit}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--card-bd)}
  .badge.ok{color:var(--ok);border-color:var(--ok)}
  .badge.bad{color:var(--bad);border-color:var(--bad)}
  .gcard{background:var(--input);border:1px solid var(--card-bd);border-radius:14px;padding:14px 16px;margin-bottom:12px}
  .grow{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px}
  .gtitle{font-size:15px;font-weight:600}
  .gtitle .cnt{font-size:12px;color:var(--muted);font-weight:400;margin-left:8px}
  .gtbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
  .gtbl th,.gtbl td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--card-bd)}
  .gtbl th{color:var(--muted);font-weight:500;font-size:12px}
  .gtbl tr:hover td{background:rgba(110,168,254,.06)}
  .gtbl input[type=checkbox]{width:16px;height:16px;accent-color:var(--accent)}
  .gtbl .idx{color:var(--accent2);font-weight:600;width:28px}
  .gtbl .plat{font-size:11px;color:var(--muted);width:64px}
  .gtbl .mt{color:var(--ok);font-size:12px}
  .gtbl .un{color:var(--muted);font-size:12px}
  .gprev{margin-top:10px}
  .gprev textarea{width:100%;min-height:70px;background:var(--input);border:1px solid var(--card-bd);color:var(--txt);border-radius:10px;padding:9px;font:13px/1.5 ui-monospace,monospace;resize:vertical}
  .empty{color:var(--muted);font-size:13px;padding:8px 2px}
</style>
</head>
<body>
<header><div class="wrap hrow" style="padding-bottom:0;margin-bottom:0">
  <h1>🎵 群音乐收集 · 配置面板</h1>
  <span id="statusPill" class="status-pill">—</span>
  <div class="spacer"></div>
  <button id="themeBtn" title="切换主题">🌓 主题</button>
  <button id="logoutBtn" title="清除本地令牌">退出</button>
</div></header>

<div class="wrap">
  <div class="card">
    <div class="stat-grid">
      <div class="stat"><div class="k">当前窗口</div><div class="v" id="stWindow">—</div></div>
      <div class="stat"><div class="k">收集状态</div><div class="v" id="stCollect">—</div></div>
      <div class="stat"><div class="k">收集模式</div><div class="v" id="stOverride">—</div></div>
    </div>
    <pre class="runs" id="stRuns">加载中…</pre>
  </div>

  <div class="card">
    <h2><span class="dot"></span>📊 收集预览与实时操作</h2>
    <div class="winrow">
      <label>窗口：
        <select id="winSel"></select>
      </label>
      <span id="neteaseBadge" class="badge">网易云：…</span>
      <button id="ovRefresh">刷新</button>
    </div>
    <div class="ops">
      <button id="opStart">▶ 强制开始收集</button>
      <button id="opStop">⏸ 强制停止收集</button>
      <button id="opAuto">↺ 恢复自动</button>
      <button id="opArchiveAll" class="btn-primary">📦 归档当前窗口全部</button>
    </div>
    <div id="groupList"><div class="empty">加载中…</div></div>
  </div>

  <div id="form"></div>
</div>

<div class="footbar">
  <span class="count" id="dirtyCount"></span>
  <span class="msg" id="saveMsg"></span>
  <button id="resetBtn">重置改动</button>
  <button id="saveBtn" class="btn-primary">保存更改</button>
</div>

<div class="modal hidden" id="tokenModal">
  <div class="box">
    <h3>需要访问令牌</h3>
    <p>在服务器 .env 里设置 <code>MUSIC_WEBUI_TOKEN</code> 的值填到这里（首次启动未设置时，令牌会打印在机器人启动日志里）。</p>
    <input id="tokenInput" placeholder="粘贴令牌…" autocomplete="off">
    <button class="btn-primary" id="tokenOk" style="width:100%">进入</button>
  </div>
</div>

<script>
const LS_KEY = "mwc_token";
let TOKEN = localStorage.getItem(LS_KEY) || "";
let ORIG = {};           // 原始值，用于脏检测
let DIRTY = {};          // key -> 新值

const $ = (s, r=document) => r.querySelector(s);
const csrf = {"Authorization": "Bearer " + TOKEN};

async function api(path, opts={}){
  opts.headers = Object.assign({}, (opts.headers||{}), csrf);
  const r = await fetch(path, opts);
  if (r.status === 401){ showToken(); throw new Error("unauthorized"); }
  return r;
}

function showToken(){ $("#tokenModal").classList.remove("hidden"); $("#tokenInput").focus(); }

function fieldControl(f, value){
  const wrap = document.createElement("div");
  wrap.className = "fctrl";
  if (f.type === "bool"){
    const id = "f_"+f.key.replace(/\./g,"_");
    const lbl = document.createElement("label");
    lbl.className = "chk";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.id = id; cb.checked = !!value;
    cb.onchange = () => markDirty(f.key, cb.checked);
    const span = document.createElement("span"); span.textContent = f.label;
    lbl.appendChild(cb); lbl.appendChild(span);
    wrap.appendChild(lbl);
  } else if (f.type === "enum"){
    const sel = document.createElement("select");
    (f.enum||[]).forEach(o => { const op=document.createElement("option"); op.value=o; op.textContent=o; sel.appendChild(op); });
    sel.value = value ?? "";
    sel.onchange = () => markDirty(f.key, sel.value);
    wrap.appendChild(sel);
  } else if (f.type === "int" || f.type === "float"){
    const inp = document.createElement("input"); inp.type="number";
    inp.value = value ?? "";
    if (f.type==="int") inp.step="1"; else inp.step="any";
    inp.oninput = () => markDirty(f.key, inp.value);
    wrap.appendChild(inp);
  } else if (f.type === "intlist" || f.type === "strlist"){
    const inp = document.createElement("input"); inp.type="text";
    inp.value = Array.isArray(value)? value.join(", ") : (value ?? "");
    inp.placeholder = "逗号分隔，如 123456, 654321";
    inp.oninput = () => markDirty(f.key, inp.value);
    wrap.appendChild(inp);
  } else {
    if (f.multiline){
      const ta = document.createElement("textarea");
      ta.value = value ?? "";
      ta.oninput = () => markDirty(f.key, ta.value);
      wrap.appendChild(ta);
    } else {
      const inp = document.createElement("input"); inp.type="text";
      inp.value = value ?? "";
      inp.oninput = () => markDirty(f.key, inp.value);
      wrap.appendChild(inp);
    }
  }
  return wrap;
}

function markDirty(key, val){
  const orig = ORIG[key];
  // 规范化比较：数组/布尔/数字
  let same = (orig === val);
  if (!same && typeof orig === "boolean") same = (String(orig)===String(val));
  if (same){ delete DIRTY[key]; }
  else { DIRTY[key] = val; }
  refreshDirty();
}
function refreshDirty(){
  const n = Object.keys(DIRTY).length;
  $("#dirtyCount").textContent = n? `● ${n} 项待保存` : "";
  $("#saveBtn").disabled = n===0;
  document.querySelectorAll(".field").forEach(fr=>{
    const k = fr.dataset.key;
    const ctrl = fr.querySelector(".fctrl");
    if (!ctrl) return;
    ctrl.classList.toggle("dirty", !!DIRTY[k]);
  });
}

function renderForm(schema, values){
  ORIG = Object.assign({}, values);
  DIRTY = {};
  const form = $("#form"); form.innerHTML = "";
  schema.forEach(sec => {
    const card = document.createElement("section");
    card.className = "card";
    const h = document.createElement("h2"); h.innerHTML = `<span class="dot"></span>${sec.title}`;
    card.appendChild(h);
    sec.fields.forEach(f => {
      const fr = document.createElement("div");
      fr.className = "field"; fr.dataset.key = f.key;
      const lab = document.createElement("div");
      lab.className = "flabel";
      lab.innerHTML = `${f.label}${f.hint? `<span class="hint">${f.hint}</span>`:""}`;
      const ctrl = fieldControl(f, values[f.key]);
      fr.appendChild(lab); fr.appendChild(ctrl);
      card.appendChild(fr);
    });
    form.appendChild(card);
  });
  refreshDirty();
}

async function loadAll(){
  try{
    const [c, s] = await Promise.all([
      api("/api/music-admin/config"),
      api("/api/music-admin/status"),
    ]);
    const cj = await c.json();
    const sj = await s.json();
    renderForm(cj.schema, cj.values);
    $("#stWindow").textContent = sj.window_label || "—";
    $("#stCollect").textContent = sj.collecting ? "收集中" : "未在收集期";
    $("#stCollect").style.color = sj.collecting ? "var(--ok)" : "var(--muted)";
    $("#stOverride").textContent = sj.collect_override || "—";
    $("#statusPill").textContent = sj.collecting ? "● 收集中" : "○ 空闲";
    $("#statusPill").className = "status-pill" + (sj.collecting? " on":"");
    $("#stRuns").textContent = sj.next_runs || "";
    setMsg("");
  }catch(e){ if (e.message!=="unauthorized") setMsg("加载失败: "+e.message, "bad"); }
}

function setMsg(t, kind=""){ const m=$("#saveMsg"); m.textContent=t; m.className="msg"+(kind?(" "+kind):""); }

async function save(){
  const payload = { values: DIRTY };
  setMsg("保存中…");
  try{
    const r = await api("/api/music-admin/config", {method:"PATCH", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    const j = await r.json();
    if (!j.ok){
      const msgs = Object.entries(j.errors||{}).map(([k,v])=>`${k}: ${v}`).join("；");
      setMsg("保存失败 — "+msgs, "bad");
      return;
    }
    setMsg("已保存 ✓", "ok");
    await loadAll();
  }catch(e){ setMsg("保存失败: "+e.message, "bad"); }
}

// 主题
const savedTheme = localStorage.getItem("mwc_theme");
if (savedTheme) document.documentElement.setAttribute("data-theme", savedTheme);
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur==="dark"?"light":"dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("mwc_theme", next);
};
// 退出
$("#logoutBtn").onclick = () => { localStorage.removeItem(LS_KEY); TOKEN=""; csrf.Authorization="Bearer "; showToken(); };
// token 弹窗
$("#tokenOk").onclick = () => {
  const v = $("#tokenInput").value.trim();
  if (!v) return;
  TOKEN = v; localStorage.setItem(LS_KEY, v); csrf.Authorization = "Bearer "+v;
  $("#tokenModal").classList.add("hidden");
  loadAll();
};
$("#tokenInput").addEventListener("keydown", e=>{ if(e.key==="Enter") $("#tokenOk").click(); });
// 保存/重置
$("#saveBtn").onclick = save;
$("#resetBtn").onclick = () => { DIRTY={}; document.querySelectorAll(".fctrl.dirty").forEach(c=>c.classList.remove("dirty")); refreshDirty(); setMsg("已重置本地改动"); };

// ---- 预览与实时操作 ----
let CUR_WIN = null;
let OV = null;

function esc(t){ return (t||"").replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

async function loadOverview(){
  try{
    const url = "/api/music-admin/overview" + (CUR_WIN ? ("?window_key="+encodeURIComponent(CUR_WIN)) : "");
    const r = await api(url);
    OV = await r.json();
    renderOverview(OV);
  }catch(e){ if(e.message!=="unauthorized") console.warn("overview 加载失败", e); }
}

function renderOverview(o){
  const sel = $("#winSel");
  sel.innerHTML = "";
  (o.windows||[]).forEach(w=>{
    const op = document.createElement("option");
    op.value = w.key; op.textContent = `${w.key} (${w.count}首)`;
    sel.appendChild(op);
  });
  if (o.windows.length){
    sel.value = o.selected_window || o.windows[0].key;
    CUR_WIN = sel.value;
  } else {
    sel.innerHTML = `<option value="">（暂无收集记录）</option>`;
  }
  const nb = $("#neteaseBadge");
  if (o.netease_logged_in){ nb.textContent="网易云：已登录 ✓"; nb.className="badge ok"; }
  else { nb.textContent="网易云：未登录 ✗"; nb.className="badge bad"; }

  const gl = $("#groupList");
  gl.innerHTML = "";
  const groups = o.groups || [];
  if (!groups.length){ gl.innerHTML = `<div class="empty">该窗口下暂无收集记录。</div>`; return; }
  groups.forEach(g=>{
    const card = document.createElement("div");
    card.className = "gcard";
    const ops = `<div class="ops">
      <button data-act="archive" data-gid="${g.group_id}">📦 归档本群</button>
      <button data-act="clear" data-gid="${g.group_id}">🗑 清空本窗口</button>
      <button data-act="del" data-gid="${g.group_id}" class="danger">删除选中</button>
      <button data-act="pname" data-gid="${g.group_id}">预览歌单名</button>
      <button data-act="pdesc" data-gid="${g.group_id}">预览简介</button>
    </div>`;
    let rows = "";
    if (!g.songs.length){
      rows = `<tr><td colspan="6" class="empty">本群该窗口暂无歌曲</td></tr>`;
    } else {
      g.songs.forEach(s=>{
        const mt = s.matched ? `<span class="mt">✓</span>` : `<span class="un">·</span>`;
        rows += `<tr>
          <td><input type="checkbox" class="songchk" data-gid="${g.group_id}" data-idx="${s.index}"></td>
          <td class="idx">${s.index}</td>
          <td><b>${esc(s.title)}</b><br><span style="color:var(--muted);font-size:12px">${esc(s.artists||"")}</span></td>
          <td>${esc(s.sharer_name||"")}</td>
          <td class="plat">${esc(s.platform_name||s.platform)}</td>
          <td>${mt}</td>
        </tr>`;
      });
    }
    const tbl = `<table class="gtbl"><thead><tr>
      <th></th><th>#</th><th>歌曲 / 歌手</th><th>分享者</th><th>平台</th><th>匹配</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
    card.innerHTML = `<div class="grow"><div class="gtitle">群 ${g.group_id}<span class="cnt">${g.count} 首</span></div></div>${ops}${tbl}<div class="gprev" id="prev_${g.group_id}"></div>`;
    gl.appendChild(card);
  });
  gl.querySelectorAll("button[data-act]").forEach(b=>{ b.onclick = ()=>groupAction(b.dataset.act, b.dataset.gid); });
}

async function groupAction(act, gid){
  gid = parseInt(gid,10);
  const wk = (OV && OV.selected_window) || "";
  let body = {action: act, group_id: gid, window_key: wk};
  if (act === "del"){
    const checked = document.querySelectorAll(`#groupList .songchk[data-gid="${gid}"]:checked`);
    const indices = Array.from(checked).map(c=>parseInt(c.dataset.idx,10));
    if (!indices.length){ flashOp("请先勾选要删除的歌曲"); return; }
    body.indices = indices;
  }
  await doAction(body);
}

async function doAction(body){
  try{
    const r = await api("/api/music-admin/action", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    flashOp(j.message || (j.ok?"操作成功":"操作失败"), j.ok?"ok":"bad");
    if (j.ok && j.data && (body.action==="preview_name"||body.action==="preview_desc") && body.group_id){
      const box = document.getElementById("prev_"+body.group_id);
      if (box){
        if (body.action==="preview_name") box.innerHTML = `<div style="font-size:12px;color:var(--muted)">歌单名预览：</div><div style="font-weight:600;margin-top:4px">${esc(j.data.name)}</div>`;
        else box.innerHTML = `<textarea readonly>${esc(j.data.description)}</textarea>`;
      }
    }
    if (j.ok) await loadOverview();
  }catch(e){ flashOp("操作失败: "+e.message, "bad"); }
}

function flashOp(msg, kind=""){
  const m = $("#saveMsg");
  m.textContent = msg;
  m.className = "msg"+(kind?(" "+kind):"");
}

$("#winSel").onchange = (e)=>{ CUR_WIN = e.target.value; loadOverview(); };
$("#ovRefresh").onclick = loadOverview;
$("#opStart").onclick = ()=>doAction({action:"start"});
$("#opStop").onclick = ()=>doAction({action:"stop"});
$("#opAuto").onclick = ()=>doAction({action:"auto"});
$("#opArchiveAll").onclick = ()=>doAction({action:"archive_all"});

loadAll();
loadOverview();
</script>
</body>
</html>"""

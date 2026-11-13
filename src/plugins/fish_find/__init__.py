"""鱼鱼找鸡 —— 引用一张图，反查 coser 的社媒账号。

底层是 ``vendor/fish-find-chicken``（Node 包 ``coser-image-search`` 1.0.0）。

**为什么不用它自带的 OneBot 适配器**：``adapters/onebot.mjs`` 是给 Node 机器人写的，
会自己调 OneBot 接口发消息（先「收到，开始找鸡」再发结果），和本项目唯一的发送出口
（``bot_utils.send_reply_parts``）冲突，拆条、记账、禁言都会被绕过。这里改成
「取图 → 存临时文件 → subprocess 跑 ``bin/fish.mjs --json`` → 只取 ``text`` 字段」，
发送仍由本插件自己调 bot，行为可控。

引擎（详见插件 README）：

- ``saucenao`` / ``tracemoe`` 直连即可，不需要外网；
- ``lens`` 最准，但要开无头 chromium，并且浏览器得走本地代理
  （``config.json`` 里的 ``proxy``，线上填 ``http://127.0.0.1:10809``）。

用法：引用一张图，发 ``/鱼鱼找鸡``（或「鱼鱼找鸡」「找鸡」）。
"""

import asyncio
import functools
import json
import os
import shutil
import subprocess
import tempfile
import time

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger

#: 插件本体所在目录。装了别处就设 ``FISH_FIND_DIR``
PLUGIN_DIR = os.environ.get(
    "FISH_FIND_DIR", "/opt/qq-music-collector/vendor/fish-find-chicken"
)
#: 触发词。都做子串匹配，``/`` 前缀可有可无
TRIGGERS = ("鱼鱼找鸡", "找鸡")
#: 单次查询总超时（秒）。插件内部每个引擎 60s，三个引擎串起来可能两分多钟
RUN_TIMEOUT = float(os.environ.get("FISH_FIND_TIMEOUT", "240"))
#: 同一个人两次查询的最小间隔（秒）。防止连点刷爆 SauceNAO 额度
USER_COOLDOWN = 30.0
#: 原图大小上限（字节）。反查要上传，太大不仅慢还容易被引擎拒
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 一次只跑一个：每个查询都要开 chromium（冷启动 2~4 秒、几百兆内存）
_run_lock = asyncio.Lock()
#: user_id -> 上次查询时间
_last_at: dict[int, float] = {}

_SIG = {"jpg": ".jpg", "jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}


async def _rule(event: MessageEvent) -> bool:
    text = event.get_plaintext()
    return any(t in text for t in TRIGGERS)


fish = on_message(rule=_rule, priority=4, block=False)


def _node() -> str:
    return shutil.which("node") or ""


@functools.lru_cache(maxsize=4)
def _node_major(node: str) -> int:
    """``node -v`` 的主版本号；读不出来按 20 处理（保守，会多传一个无害的 flag）。"""
    try:
        out = subprocess.run(
            [node, "-v"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return int(out.lstrip("v").split(".")[0])
    except Exception:  # noqa: BLE001
        return 20


def image_urls_of(raw: object) -> list[str]:
    """按出现顺序取出图片地址。

    ``raw`` 有三种形态，**都要吃得下**：

    - ``Message``（``event.get_message()``）——元素是 ``MessageSegment``；
    - ``list[dict]``（``bot.get_msg()`` 的原始返回，NapCat 就是这种）；
    - ``str``（CQ 码格式）。

    ⚠️ 2026-11-13 踩过：直接 ``Message(list_of_dict)`` 会抛
    ``ValueError: Unexpected type: <class 'dict'>``，而那句在 handler 里没兜住，
    表现就是「引用图片发 /鱼鱼找鸡，机器人一声不吭」。
    """
    urls: list[str] = []
    if isinstance(raw, str):
        raw = Message(raw)
    for item in raw or []:  # type: ignore[union-attr]
        if isinstance(item, dict):
            if item.get("type") != "image":
                continue
            data = item.get("data") or {}
        else:
            if getattr(item, "type", "") != "image":
                continue
            data = getattr(item, "data", None) or {}
        url = str(data.get("url") or "").strip()
        if not url:
            # 少数实现只给 file / 本地路径
            url = str(data.get("file") or "").strip()
        if url:
            urls.append(url)
    return urls


async def _collect(bot: Bot, event: MessageEvent) -> tuple[list[str], str]:
    """返回 ``(图片地址, 失败原因)``。本条消息自带的图优先，没有再看被引用的那条。

    ⚠️ **引用消息不在 ``event.message`` 里**。适配器的 ``_check_reply`` 已经调过
    ``get_msg``，把 reply 段从 message 里 ``del`` 掉、内容塞进 ``event.reply``
    （NoneBot v11 `bot.py` 的 41~46 行）。所以别再遍历 message 找 ``type=="reply"``
    —— 2026-11-13 就是栽在这里：循环永远进不去，图永远是 0 张，机器人一声不吭。
    """
    urls = image_urls_of(event.get_message())
    if urls:
        return urls, ""

    reply = getattr(event, "reply", None)
    if reply is not None:
        reply_msg = getattr(reply, "message", None)
        urls = image_urls_of(reply_msg or [])
        mid = getattr(reply, "message_id", "?")
        if urls:
            logger.info(f"[fish] 引用的消息 {mid} 里有 {len(urls)} 张图")
            return urls, ""
        logger.info(f"[fish] 引用的消息 {mid} 里没有图片：{str(reply_msg)[:120]!r}")
        return [], "被引用的那条消息里没有图片"

    # 兜底：个别实现没填 event.reply，reply 段可能还留在 message 里
    for seg in event.get_message():
        if seg.type != "reply":
            continue
        mid = str((seg.data or {}).get("id") or "")
        if not mid.isdigit():
            continue
        try:
            got = await bot.get_msg(message_id=int(mid))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[fish] 取引用消息失败 mid={mid}: {exc}")
            return [], f"取不到被引用的那条消息（{exc}）"
        urls = image_urls_of((got or {}).get("message") or [])
        logger.info(f"[fish] 引用消息 mid={mid} 里找到 {len(urls)} 张图")
        if urls:
            return urls, ""
        return [], "被引用的那条消息里没有图片"

    return [], ""


async def _download(url: str) -> str:
    """把图下到临时文件，返回路径。调用方负责删。"""
    async with httpx.AsyncClient(
        timeout=25.0, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"图片太大（{len(data) / 1048576:.1f}MB）")
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    suffix = _SIG.get(ctype.rsplit("/", 1)[-1], ".jpg")
    fd, path = tempfile.mkstemp(prefix="fish-", suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


async def _run(image_path: str) -> dict:
    """跑一次 node，返回解析后的 JSON（含 ``text`` / ``accounts`` / ``failures``）。"""
    node = _node()
    if not node:
        raise RuntimeError("服务器上没有 node")

    # Node 20 的全局 WebSocket 还在实验开关后面，不开就 ``WebSocket is not defined``
    # （插件用裸 CDP 驱动浏览器，全靠它）。Node ≥ 21 已默认可用，别再多传 flag。
    env = os.environ.copy()
    if _node_major(node) < 21:
        opts = env.get("NODE_OPTIONS", "")
        if "--experimental-websocket" not in opts:
            env["NODE_OPTIONS"] = f"{opts} --experimental-websocket".strip()

    proc = await asyncio.create_subprocess_exec(
        node,
        "bin/fish.mjs",
        image_path,
        "--json",
        cwd=PLUGIN_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), RUN_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"查询超时（{RUN_TIMEOUT:.0f} 秒）")
    text = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not text:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError("；".join(tail) or f"退出码 {proc.returncode}")
    # 插件在失败时也可能只往 stderr 写，stdout 为空即视为失败（上面已处理）
    return json.loads(text)


@fish.handle()
async def _handle(bot: Bot, event: MessageEvent) -> None:
    if not os.path.isdir(PLUGIN_DIR):
        logger.warning(f"[fish] 插件目录不存在：{PLUGIN_DIR}")
        await fish.finish("鱼鱼找鸡没装好（找不到插件目录），先别用。")

    # 兜底：任何没预料到的异常都要回一句话，别再「一声不吭」（2026-11-13 的教训）
    try:
        await _run_query(bot, event)
    except FinishedException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=exc).error("[fish] 处理失败")
        await fish.finish(f"出错了：{type(exc).__name__}: {exc}")


async def _run_query(bot: Bot, event: MessageEvent) -> None:
    urls, why = await _collect(bot, event)
    logger.info(
        f"[fish] 收到请求 群{getattr(event, 'group_id', '-')} "
        f"用户{getattr(event, 'user_id', '-')} 图 {len(urls)} 张"
        + (f"（{why}）" if why else "")
    )
    if not urls:
        await fish.finish(why or "引用一张图再发「鱼鱼找鸡」～")

    uid = int(getattr(event, "user_id", 0) or 0)
    now = time.time()
    if uid and now - _last_at.get(uid, 0.0) < USER_COOLDOWN:
        left = int(USER_COOLDOWN - (now - _last_at[uid])) + 1
        await fish.finish(f"刚查过啦，歇 {left} 秒再来。")
    _last_at[uid] = now

    if _run_lock.locked():
        await fish.finish("前面那张还在找，等它出结果再来。")

    await fish.send(MessageSegment.reply(event.message_id) + "收到，开始找鸡")

    async with _run_lock:
        path = ""
        try:
            path = await _download(urls[0])
            result = await _run(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[fish] 查询失败: {type(exc).__name__}: {exc}")
            await fish.finish(f"没查成：{exc}")
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    body = str(result.get("text") or "").strip()
    if not body:
        body = "引擎都没给出结果，换个图或者稍后再试。"
    logger.info(
        f"[fish] 群{getattr(event, 'group_id', '-')} 查询完成 "
        f"{len(result.get('accounts') or [])} 个账号，"
        f"失败引擎 {len(result.get('failures') or [])} 个"
    )
    await fish.finish(body)

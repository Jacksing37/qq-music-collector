"""给表情包**看图打标签**：让视觉模型看一眼图，写几个真能搜到的词。

为什么要单独一个模块：自动收藏（``llm.sticker_auto_collect``）收下来的图只有 QQ 给的
``summary``，而**图片型表情**（``image`` + ``sub_type=1``）压根没有 summary——落库就成了
「未分类」，``sticker_find`` 一个词也搜不到，等于一堆死库存（线上 72 张全这样）。
模型自己 ``sticker_add`` 收的图标签很准（「嘲讽 / 猫 / 丑猫 / 欠打」），差别就在「有没有
人看一眼图」——这里补的就是那一眼。

三点克制：

- 走 :meth:`LLMBot.complete`，**不占冷却、不写历史、不拼人设**——它不是在聊天，是在打标；
- 失败一律当「没打成」，**绝不抛异常**：标签是锦上添花，不能拖住「收表情包」这条主流程；
- 同一时刻只跑一个请求（:data:`_lock`）：自动收一天最多 30 张，串行就够了，并发只会
  让中转层排队超时。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Optional

from nonebot.log import logger

from . import stickers as sticker_mod
from .config import LLMConfig
from .llm import llm_bot

#: 任务说明。和 ``complete()`` 的 system 一样：只讲任务，不演人设
_SYSTEM = "你是给群聊表情包打标签的助手。只输出一行 JSON，不要解释、不要多余的话。"

#: 打标签的提示词。要点：**逼它给「画面 + 情绪」**，并明确禁止「表情包」这种零信息量的词——
#: 否则十个标签里有八个是「表情包 / 图片 / 搞笑」，搜起来还是等于没有
_PROMPT = (
    "这是一张群聊里发的表情包。看一下画面和它想表达的情绪，输出一行 JSON：\n"
    '{"desc": "20 字以内的画面描述", "tags": ["标签1", "标签2", "标签3"]}\n'
    "要求：\n"
    "1. tags 给 3~6 个中文词，要具体、能当搜索词：画的是什么（猫/狗/动漫少女/熊猫头）"
    "+ 什么情绪（无语/嘲讽/开心/委屈/敷衍）+ 什么场合用（打招呼/晚安/赞同/摸鱼）；\n"
    "2. **不要**写「表情包」「图片」「搞笑」这种每张图都成立、搜了等于没搜的词；\n"
    "3. 图里有人物/作品特征就写出来（发色、制服、角色名——认得出才写，认不出别编）；\n"
    "4. 只输出 JSON，不要代码块、不要解释。"
)

#: 一次最多等多久（秒）。打标是后台任务，久一点没事，但别挂死
_TIMEOUT_HINT = 60

#: 同一时刻只跑一个打标请求
_lock = asyncio.Lock()

#: 模型爱把 JSON 包在 ```json 围栏里
_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def data_url(data: bytes) -> str:
    """把图片字节变成 data URL（模型那边直接认，不用它再抓一次网络）。"""
    return f"data:{sticker_mod.mime_of(data)};base64,{base64.b64encode(data).decode()}"


def parse_reply(text: str) -> tuple[list[str], str]:
    """从模型回复里抠出 ``(tags, desc)``；抠不出来就返回空。

    容错到「只要回复里有个 JSON 对象就算」——不同模型对「只输出 JSON」的服从度差别很大，
    ```json 围栏、前后加一句「好的：」都见过。
    """
    raw = (text or "").strip()
    if not raw:
        return [], ""
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        obj = json.loads(raw)
    except ValueError:
        return [], ""
    if not isinstance(obj, dict):
        return [], ""
    # 标签统一走 stickers.parse_tags：去重、限 8 个、每个 16 字，和手工改标签同一套规矩
    tags = sticker_mod.parse_tags(obj.get("tags"))
    desc = " ".join(str(obj.get("desc") or "").split())[:80]
    return tags, desc


async def suggest(
    data: bytes, cfg: LLMConfig, *, model: str = ""
) -> tuple[list[str], str]:
    """看一眼图，返回 ``(tags, desc)``；失败返回 ``([], "")``，**不抛异常**。"""
    if not data:
        return [], ""
    try:
        async with _lock:
            text, err = await llm_bot.complete(
                _PROMPT,
                cfg,
                system=_SYSTEM,
                model=str(model or ""),
                # 一行 JSON 而已，给 160 已经宽裕；给太多它会开始解释
                max_tokens=160,
                temperature=0.2,
                images=[data_url(data)],
            )
    except Exception as exc:  # noqa: BLE001 — 打标失败绝不能影响收表情包
        logger.debug(f"[sticker] 看图打标签异常: {exc}")
        return [], ""
    if not text:
        logger.debug(f"[sticker] 看图打标签失败：{err or '空回复'}")
        return [], ""
    tags, desc = parse_reply(text)
    if not tags:
        logger.debug(f"[sticker] 打标签没解析出标签：{text[:120]}")
    return tags, desc


async def retag_one(
    sid: str,
    data: bytes,
    cfg: LLMConfig,
    *,
    sticker_dir: str = "",
    model: str = "",
) -> tuple[bool, str]:
    """给库里某一条补标签（失败时**保持原样**）。返回 ``(是否改成, 说明)``。"""
    tags, desc = await suggest(data, cfg, model=model)
    if not tags:
        return False, "没打出来"
    ok, msg = await sticker_mod.update(
        sid, tags=tags, note=desc or None, sticker_dir=sticker_dir
    )
    if ok:
        logger.info(f"[sticker] 给 {sid} 补了标签 {tags}")
    return ok, msg


async def retag_untagged(
    cfg: LLMConfig,
    *,
    sticker_dir: str = "",
    limit: int = 0,
    model: str = "",
    progress=None,
) -> dict:
    """把库里**只有「未分类」**的图逐张补标签。``limit=0`` = 全部。

    ``progress`` 是可选的同步回调 ``fn(done, total, sid)``，给网页那边报进度用
    （后台任务，前端要能看见「跑到第几张」）。
    """
    rows = sticker_mod.untagged(sticker_dir=sticker_dir)
    if limit and int(limit) > 0:
        rows = rows[: int(limit)]
    total = len(rows)
    done = failed = 0
    for index, item in enumerate(rows, 1):
        sid = str(item.get("id") or "")
        path = sticker_mod.path_of(item, sticker_dir=sticker_dir)
        if path is None:
            failed += 1
        else:
            try:
                data = path.read_bytes()
            except OSError as exc:
                logger.debug(f"[sticker] 补标签读图失败 {path}: {exc}")
                failed += 1
                data = b""
            if data:
                ok, _ = await retag_one(
                    sid, data, cfg, sticker_dir=sticker_dir, model=model
                )
                done += 1 if ok else 0
                failed += 0 if ok else 1
        if progress is not None:
            try:
                progress(index, total, sid)
            except Exception:  # noqa: BLE001 — 回调坏了不该中断批量
                pass
    logger.info(f"[sticker] 批量补标签结束：成功 {done} / 失败 {failed} / 共 {total}")
    return {"total": total, "done": done, "failed": failed}

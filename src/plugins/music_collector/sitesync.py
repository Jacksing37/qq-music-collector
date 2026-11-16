"""从公开表情清单站（如 ``fisharchive.cc/stickers/index.json``）拉取表情包入库。

目标站是「鲸鱼娘 DeepSeek 同人表情整理站」：纯静态、无密钥、允许跨域，直接 GET 一份
``index.json`` 就能拿到完整清单。每条有 ``alt``（画面描述）、``tags``（现成标签）、
``original``（原图，保留动画）/ ``large`` / ``preview``（webp 预览）。

三点克制，和 :mod:`stickertag` 同一个思路：

- **下载用 ``original`` 不用 ``large``**：large 是转过的 webp，动图会丢动画；original 才是
  作者上传的那一张。但 original 可能是几 MB 的 gif，要卡 ``stickers.MAX_BYTES``（8MB）。
- **按 source 去重跳过**：同一张图重复拉会 sha1 去重（不存两份），但会白白下载一遍。
  这里先用 ``source``（original URL）比对，库里已有 ``kind=site`` 且 source 相同的就跳过下载。
- **失败静默**：单张下载失败只计数跳过，不中断整批——清单里偶尔有一两张图床挂了很正常。
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import httpx
from nonebot.log import logger

from . import stickers as sticker_mod
from . import webtool

#: 单次拉取的上限（防手滑把几千张一次全下下来）。0 或超过它都按它封顶
MAX_PULL = 500
#: 拉清单 / 单张下载的超时（秒）
_FETCH_TIMEOUT = 30.0
#: 默认目标站
DEFAULT_SITE = "https://fisharchive.cc/stickers/index.json"
#: 默认并发下载数。串行下载 300 张原图要十几分钟，并发 8 能把时间压到一两分钟。
#: 别设太大——目标站是个人维护的静态站，礼貌点
DEFAULT_CONCURRENCY = 8

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


async def fetch_index(url: str) -> dict:
    """拉取清单 JSON 并校验结构。**失败抛 ValueError（中文原因）**。

    只认 ``stickers`` 是数组的清单；字段名和 fisharchive.cc 对齐（``alt``/``tags``/
    ``original``）。空清单也算错——没得拉就别让管理员以为成功了。
    """
    text = str(url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        raise ValueError("清单地址要是完整的 http/https 网址")
    blocked = webtool.url_block_reason(text)
    if blocked:
        raise ValueError(blocked)
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(text)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        raise ValueError("拉取清单超时") from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"拉取清单失败：HTTP {exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"拉取清单失败：{exc}") from None
    if not isinstance(data, dict):
        raise ValueError("清单不是 JSON 对象")
    stickers = data.get("stickers")
    if not isinstance(stickers, list) or not stickers:
        raise ValueError("清单里没有 stickers（或字段名对不上）")
    return data


def _entry_url(st: dict) -> str:
    """取一张的原图地址。``original`` 优先，没有才退 ``large``（至少是张能看的图）。"""
    for key in ("original", "large", "preview"):
        url = str((st or {}).get(key) or "").strip()
        if url:
            return url
    return ""


async def pull(
    url: str,
    *,
    bucket: str = "",
    limit: int = 0,
    sticker_dir: str = "",
    progress=None,
    cancel_check=None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict:
    """把站点清单里的表情包拉进库。返回 ``{total, imported, skipped, existing}``。

    ``limit=0`` = 全拉（但封顶 :data:`MAX_PULL`）。``progress`` 是可选回调
    ``fn(done, total, alt)``，给网页报进度。``cancel_check`` 是可选回调
    ``fn() -> bool``，返回 True 就尽快收手（已经发出的请求收不回，但不再发新的）。

    **并发下载**：共用一个 :class:`httpx.AsyncClient`（复用连接）+ :class:`asyncio.Semaphore`
    限流，默认 :data:`DEFAULT_CONCURRENCY` 路并发——串行下 300 张要十几分钟，并发能压到
    一两分钟。SSRF 校验逐张照做（复用 :mod:`webtool`）。

    **不抛异常**：拉清单失败在返回里体现（``error`` 字段），单张下载失败只算 ``skipped``。
    取消时返回里带 ``cancelled=True``。
    """
    try:
        data = await fetch_index(url)
    except ValueError as exc:
        return {"error": str(exc), "total": 0, "imported": 0, "skipped": 0, "existing": 0}

    stickers = [s for s in data.get("stickers") or [] if isinstance(s, dict)]
    total = min(len(stickers), MAX_PULL)
    if limit and 0 < int(limit) < total:
        total = int(limit)
    if total <= 0:
        return {"total": 0, "imported": 0, "skipped": 0, "existing": 0}

    # 库里已有的 kind=site 条目，按 source（original URL）建个集合，重复的直接跳过下载
    seen = {
        str(it.get("source") or "")
        for it in sticker_mod.find("", limit=sticker_mod.MAX_ITEMS, sticker_dir=sticker_dir)
        if str(it.get("kind") or "") == "site"
    }

    want = max(1, min(int(concurrency), 32))
    sem = asyncio.Semaphore(want)
    counters = {"imported": 0, "skipped": 0, "existing": 0, "done": 0}
    cancelled = False

    def _cancelled() -> bool:
        return bool(cancel_check is not None and cancel_check())

    def _bump(key: str, st: dict) -> None:
        counters[key] += 1
        counters["done"] += 1
        alt = " ".join(str(st.get("alt") or "").split())[:80]
        if progress is not None:
            try:
                progress(counters["done"], total, alt or _entry_url(st))
            except Exception:  # noqa: BLE001 — 回调坏了不该中断批量
                pass

    async def _download_raw(src: str) -> bytes:
        """共享 client 下载一张（绕过每次新建连接的 ``sticker_mod.download``）。

        SSRF 校验照搬 :func:`stickers.download` 的第一道闸；bytes 大小 / 格式校验交给
        调用方（``sniff`` / ``MAX_BYTES``），这里只负责把字节拿回来。
        """
        blocked = webtool.url_block_reason(src)
        if blocked:
            raise ValueError(blocked)
        resp = await client.get(src)
        resp.raise_for_status()
        return resp.content

    async def one(st: dict) -> None:
        async with sem:
            if _cancelled():
                return
            alt = " ".join(str(st.get("alt") or "").split())[:80]
            tags = sticker_mod.parse_tags(st.get("tags"))
            src = _entry_url(st)
            if not src:
                _bump("skipped", st)
                return
            if src in seen:
                _bump("existing", st)
                return
            if not tags:
                # 没标签的图收进来也搜不到，但 alt 还能当备注——这里直接跳过，别制造「未分类」库存
                _bump("skipped", st)
                return
            try:
                raw = await _download_raw(src)
                if len(raw) > sticker_mod.MAX_BYTES:
                    _bump("skipped", st)
                    return
                if not sticker_mod.sniff(raw)[0]:
                    _bump("skipped", st)
                    return
                entry, created = await sticker_mod.add(
                    raw, tags=tags, note=alt, source=src, kind="site", sticker_dir=sticker_dir
                )
                if entry is None:
                    _bump("skipped", st)
                elif not created:
                    _bump("existing", st)
                else:
                    if bucket:
                        await sticker_mod.set_bucket(
                            str(entry.get("id") or ""), bucket, sticker_dir=sticker_dir
                        )
                    _bump("imported", st)
            except (ValueError, httpx.HTTPError) as exc:
                logger.debug(f"[sticker] 站点拉取单张失败 {src}: {exc}")
                _bump("skipped", st)

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT, headers=_HEADERS, follow_redirects=True
    ) as client:
        await asyncio.gather(*(one(st) for st in stickers[:total]))
        cancelled = _cancelled()

    logger.info(
        f"[sticker] 站点拉取结束：新收 {counters['imported']} / 已有 {counters['existing']}"
        f" / 跳过 {counters['skipped']} / 共 {total}" + ("（已取消）" if cancelled else "")
    )
    return {
        "total": total,
        "imported": counters["imported"],
        "skipped": counters["skipped"],
        "existing": counters["existing"],
        "cancelled": cancelled,
    }

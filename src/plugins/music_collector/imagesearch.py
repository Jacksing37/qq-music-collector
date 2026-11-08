"""图片搜索：让模型按关键词找到**图片直链**，再发到群里。

为什么单独一个模块而不复用 :mod:`websearch`：文字搜索给的是标题 + 摘要，模型从中
拼不出任何一张能直接发的图。要发图得拿到**图片本体地址**（``murl`` 那类），
两个引擎的返回结构也完全不同。

两个后端，按顺序试：

- ``bing``：``cn.bing.com/images/search`` 的结果页，每条结果把 JSON 塞在
  ``<a class="iusc" m="{…}">`` 的属性里（``murl`` 原图 / ``turl`` 缩略图 /
  ``purl`` 来源页）。和 ``web_search`` 走同一个站，出网实测可用；
- ``baidu``：``image.baidu.com/search/acjson``，直接返回 JSON。bing 偶尔会给验证码
  页（HTML 里一条结果都解析不出来），这时换它兜底。

**返回的不保证都能发**：图床防盗链、链接过期、站点给的是网页而不是图，都会在
``send_image`` 那一步才暴露。所以这里明确告诉模型「发不出去就换一张」，
而不是让它以为整条链路坏了。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

#: 一次最多给模型几条候选（再多它也读不完，还白烧 token）
MAX_RESULTS = 12
#: 两次搜索之间的最小间隔（秒）。和 web_search 一样，防 Bing 反爬
MIN_INTERVAL = 2.0
#: 排队等超过这么多秒就放弃，让模型先说话
_MAX_WAIT = 5.0
#: 结果缓存（键 = 关键词 + 条数），同一轮里模型可能搜同一个词两次
CACHE_TTL = 600.0
CACHE_MAX = 40

_BING_URL = "https://cn.bing.com/images/search"
_BAIDU_URL = "https://image.baidu.com/search/acjson"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

#: Bing 每条结果的 JSON 塞在这个属性里；属性值里的引号是 HTML 转义过的
_BING_M = re.compile(r'<a[^>]+class="[^"]*iusc[^"]*"[^>]*\bm="([^"]+)"', re.I)
#: 兜底：有的版本把 m 写在 img 的 data 里
_BING_IMG = re.compile(r'<img[^>]+class="[^"]*mimg[^"]*"[^>]+src="([^"]+)"', re.I)


# ---------------------------------------------------------------- 限流与缓存

_lock = asyncio.Lock()
_next_at = 0.0
_cache: dict[str, tuple[float, list[dict]]] = {}


async def throttle(interval: float = MIN_INTERVAL) -> bool:
    """拿「现在可以搜」的许可；要等太久返回 ``False``（模型该先说话）。"""
    global _next_at
    gap = max(0.0, float(interval))
    loop = asyncio.get_running_loop()
    async with _lock:
        wait = _next_at - loop.time()
        if wait > _MAX_WAIT:
            return False
        if wait > 0:
            await asyncio.sleep(wait)
        _next_at = loop.time() + gap
        return True


def reset() -> None:
    """清掉限流与缓存（测试用）。"""
    global _next_at
    _next_at = 0.0
    _cache.clear()


def _cache_get(key: str) -> Optional[list[dict]]:
    hit = _cache.get(key)
    if not hit:
        return None
    at, rows = hit
    if time.monotonic() - at > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return rows


def _cache_put(key: str, rows: list[dict]) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)
    _cache[key] = (time.monotonic(), rows)


# ---------------------------------------------------------------- 解析

def _clean_url(raw: str) -> str:
    """只认公网 http(s) 直链；``data:`` / ``blob:`` / 空串一律丢掉。"""
    text = html_mod.unescape(str(raw or "")).strip()
    if not text.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    return text if parts.hostname else ""


def parse_bing(html: str, limit: int = MAX_RESULTS) -> list[dict]:
    """从 Bing 图片结果页里抠出 ``murl / turl / purl / title``。"""
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in _BING_M.findall(html or ""):
        try:
            meta = json.loads(html_mod.unescape(raw))
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        url = _clean_url(meta.get("murl") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append(
            {
                "url": url,
                "thumb": _clean_url(meta.get("turl") or ""),
                "page": _clean_url(meta.get("purl") or ""),
                "title": str(html_mod.unescape(str(meta.get("t") or "")))[:80],
            }
        )
        if len(rows) >= limit:
            break
    if rows:
        return rows
    # 兜底：整页只有 img 标签时，至少把缩略图地址交出去（模型能发，只是可能小）
    for raw in _BING_IMG.findall(html or ""):
        url = _clean_url(raw)
        if url and url not in seen:
            seen.add(url)
            rows.append({"url": url, "thumb": "", "page": "", "title": ""})
        if len(rows) >= limit:
            break
    return rows


def parse_baidu(payload: str, limit: int = MAX_RESULTS) -> list[dict]:
    """从百度图片的 acjson 里抠出直链。

    它同时给 ``middleURL``（约 300px）和 ``replaceUrl[0].ObjURL``（原图，现在常常
    是加密串），优先中图：**能直接下载**比「分辨率高」重要得多。
    """
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        url = _clean_url(item.get("middleURL") or item.get("hoverURL") or item.get("thumbURL") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(item.get("fromPageTitleEnc") or item.get("fromPageTitle") or "")
        rows.append(
            {
                "url": url,
                "thumb": _clean_url(item.get("thumbURL") or ""),
                "page": _clean_url(item.get("fromURL") or ""),
                "title": re.sub(r"<[^>]+>", "", title)[:80],
            }
        )
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------- 抓取

async def _search_bing(query: str, count: int, timeout: float) -> list[dict]:
    params = {"q": query, "form": "HDRSC2", "first": "1", "tsc": "ImageHoverTitle"}
    async with httpx.AsyncClient(
        timeout=float(timeout), headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(_BING_URL, params=params)
        resp.raise_for_status()
    return parse_bing(resp.text, count)


async def _search_baidu(query: str, count: int, timeout: float) -> list[dict]:
    params = {
        "tn": "resultjson_com",
        "ipn": "rj",
        "ct": "201326592",
        "fp": "result",
        "word": query,
        "queryWord": query,
        "cl": "2",
        "lm": "-1",
        "ie": "utf-8",
        "oe": "utf-8",
        "st": "-1",
        "ic": "0",
        "pn": "0",
        "rn": str(max(count, 10)),
    }
    async with httpx.AsyncClient(
        timeout=float(timeout), headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(_BAIDU_URL, params=params)
        resp.raise_for_status()
    return parse_baidu(resp.text, count)


async def search(
    query: str,
    count: int = 6,
    timeout: float = 20.0,
    *,
    engines: tuple[str, ...] = ("bing", "baidu"),
) -> tuple[list[dict], str]:
    """搜图，返回 ``(候选列表, 错误说明)``。**不抛异常**。

    两个引擎依次试，谁先给出非空结果就用谁；全空时把各自的错误拼成一句话
    交给工具层——让模型知道「是搜不到，还是这台机器根本连不上」。
    """
    text = " ".join(str(query or "").split())
    if not text:
        return [], "关键词是空的"
    want = max(1, min(int(count or 6), MAX_RESULTS))
    key = f"{text}|{want}"
    cached = _cache_get(key)
    if cached is not None:
        return cached, ""

    errors: list[str] = []
    rows: list[dict] = []
    for engine in engines:
        try:
            if engine == "bing":
                rows = await _search_bing(text, want, timeout)
            elif engine == "baidu":
                rows = await _search_baidu(text, want, timeout)
            else:
                continue
        except httpx.TimeoutException:
            errors.append(f"{engine} 超时（{timeout:g}s）")
            continue
        except httpx.HTTPStatusError as exc:
            errors.append(f"{engine} 返回 HTTP {exc.response.status_code}")
            continue
        except httpx.HTTPError as exc:
            errors.append(f"{engine} 网络错误：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — 解析/编码问题也算这个引擎挂了
            errors.append(f"{engine} 出错：{exc}")
            continue
        if rows:
            logger.info(f"[imagesearch] 「{text[:30]}」→ {len(rows)} 张（{engine}）")
            break
        errors.append(f"{engine} 没解析出任何图片（多半是被要求验证码或换了页面结构）")

    if rows:
        _cache_put(key, rows)
        return rows, ""
    logger.warning(f"[imagesearch] 「{text[:30]}」搜不到：{'；'.join(errors)}")
    return [], "；".join(errors) or "没有结果"


# ---------------------------------------------------------------- 给模型看的话

def format_results(query: str, rows: list[dict]) -> str:
    """拼成给模型看的一段：编号 + 直链 + 来源页 + 标题。"""
    if not rows:
        return ""
    lines = [
        f"图片搜索「{query}」共 {len(rows)} 条，下面是**图片直链**（可以直接发）："
    ]
    for index, row in enumerate(rows, 1):
        title = (row.get("title") or "").strip() or "（无标题）"
        line = f"{index}. {title}\n   {row.get('url') or ''}"
        if row.get("page"):
            line += f"\n   来源页：{row['page']}"
        lines.append(line)
    lines.append(
        "怎么用：挑一条最贴题的，用 send_image(url=…) 发到群里（一次一张，别连发）；"
        "想留着以后用，就 sticker_add(url=…, tags=…) 收进表情包库。"
        "**直链不一定都能下**：图床防盗链、链接过期、返回的其实是网页，"
        "这种情况 send_image 会告诉你原因——换下一条，别反复重试同一条。"
    )
    return "\n".join(lines)

"""``lookup_anime_image`` 的后端：以图搜番（trace.moe）。

**为什么非要一个独立工具**：「这张图里是谁」用文字搜索解不出来。实测（2026-11-05，
本机与服务器结果一模一样）把这些画面特征当关键词去搜，Bing 会塌缩成第一个能独立
成词的片段——``粉色长发 金丝圆框眼镜 动漫角色 蓝眼睛`` 返回的是「粉色」RGB 色值表，
``薰香花朵凛然绽放 粉发 眼镜 角色`` 返回微信读书，``长崎爽世 眼镜`` 截成「长崎市」。
要认图只能把**图本身**丢给以图搜图。这条链路是 ``vendor/workbuddy-tools`` 那套工具
里唯一在认图上管用的环节（``scripts/trace.mjs``），这里用纯 Python 重写，
省掉 node 依赖。

trace.moe 的约定（照 ``trace.mjs`` 对齐）：

- 端点 ``POST https://api.trace.moe/search?anilistInfo&cutBorders``，multipart 字段名 ``image``；
- ``cutBorders`` 让服务端先裁黑边（截图常带），``anilistInfo`` 带回作品名/集数/年份；
- 响应体里自带 ``quota`` / ``quotaUsed``（免费额度 100 次/天），顺手报给模型；
- ``similarity`` 是**同一张图的相似度**，不能跨图比较：正片截图一般 ≥0.85。

它只索引**动画正片**：插画、同人图、漫画分镜、游戏截图、真人照片都会返回空结果，
这种情况明确告诉模型「认不出来」，不要让它猜。
"""

import asyncio
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

#: 搜索端点。``anilistInfo`` / ``cutBorders`` 是无值开关，照原版拼在 query 里
SEARCH_URL = "https://api.trace.moe/search?anilistInfo&cutBorders"
#: trace.moe 只收 10MB 以内的图，留点余量
MAX_BYTES = 8 * 1024 * 1024
#: 两次查询的最小间隔（秒）。免费额度 100 次/天，别连着打
MIN_INTERVAL = 3.0
#: 排队等超过这么多秒就放弃，让模型先说话——别让一次回复干等
_MAX_WAIT = 5.0
#: 结果缓存（键 = 图片 URL）。同一条消息里模型可能连问两遍
CACHE_TTL = 900.0
CACHE_MAX = 50

#: 抓图用的 UA。QQ 的图片 CDN 对 UA 不挑，但空 UA 容易被挡
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

#: 相似度分档（只用于把话说清楚，不参与过滤）
_TIERS = ((0.90, "基本就是这部"), (0.80, "很可能是"), (0.0, "只能当线索"))


# ---------------------------------------------------------------- 限流与缓存

_lock = asyncio.Lock()
_next_at = 0.0
_cache: dict[str, tuple[float, dict]] = {}


async def throttle(interval: float = MIN_INTERVAL) -> bool:
    """拿到「现在可以查」的许可。要等太久返回 ``False``（模型该先说话）。

    ``interval`` 由 ``llm.image_lookup_interval`` 传入（``0`` = 不限）。
    """
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


def _cache_get(url: str) -> Optional[dict]:
    hit = _cache.get(url)
    if not hit:
        return None
    at, value = hit
    if time.monotonic() - at > CACHE_TTL:
        _cache.pop(url, None)
        return None
    return value


def _cache_put(url: str, value: dict) -> None:
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)
    _cache[url] = (time.monotonic(), value)


# ---------------------------------------------------------------- 抓图

async def _download(url: str, timeout: float) -> bytes:
    """把图下下来。非图片、太大、404 都翻成一句中文。"""
    async with httpx.AsyncClient(
        timeout=float(timeout), headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            raise ValueError(f"不是图片（{ctype}）")
        data = resp.content
    if len(data) > MAX_BYTES:
        raise ValueError(f"图片太大（{len(data) // 1024} KB，上限 {MAX_BYTES // 1024} KB）")
    if not data:
        raise ValueError("图片是空的")
    return data


async def _post_image(data: bytes, timeout: float) -> httpx.Response:
    """把图 POST 给 trace.moe。抽成函数是为了测试能打桩（不必真联网）。"""
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        return await client.post(SEARCH_URL, files={"image": ("image", data, mime)})


# ---------------------------------------------------------------- 解析

def _hhmmss(seconds: object) -> str:
    try:
        total = max(0, int(float(seconds)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _title_of(anilist: dict) -> str:
    """作品名。优先原名，其次罗马字/英文——模型搜后续资料时原名最好使。"""
    title = (anilist or {}).get("title") or {}
    native = str(title.get("native") or "").strip()
    romaji = str(title.get("romaji") or "").strip()
    english = str(title.get("english") or "").strip()
    if native:
        return f"{native}（{romaji}）" if romaji and romaji != native else native
    return romaji or english or "（无标题）"


def _episode_of(value: object) -> str:
    """集数。剧场版的 ``episode`` 会是 ``True``，别写成「第 True 话」。"""
    if isinstance(value, bool):
        return "剧场版" if value else "?"
    try:
        return f"第 {int(value)} 话"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"


def _normalize(row: dict) -> dict:
    anilist = row.get("anilist") or {}
    year = ((anilist.get("startDate") or {}).get("year")) or ""
    fmt = str(anilist.get("format") or "")
    # 剧场版的 episode 有时给的是 1（不是 true），照着写会变成「第 1 话」——按 format 纠正
    episode = "剧场版" if "MOVIE" in fmt.upper() else _episode_of(row.get("episode"))
    return {
        "similarity": float(row.get("similarity") or 0.0),
        "title": _title_of(anilist),
        "anilist_id": anilist.get("id"),
        "format": fmt,
        "year": year,
        "episode": episode,
        "at": _hhmmss(row.get("from")),
        "to": _hhmmss(row.get("to")),
        "filename": str(row.get("filename") or "")[:80],
        "preview": str(row.get("image") or ""),
    }


def tier_of(similarity: float) -> str:
    """相似度分档，给模型一句人话（决定它敢不敢下结论）。"""
    for floor, label in _TIERS:
        if similarity >= floor:
            return label
    return _TIERS[-1][1]


# ---------------------------------------------------------------- 对外

def _pack(url: str, error: str = "", quota: object = None,
          used: object = None, rows: Optional[list[dict]] = None) -> dict:
    rows = rows or []
    return {
        "url": url,
        "error": error,
        "quota": quota,
        "used": used,
        "results": rows,
        "best": rows[0] if rows else None,
    }


async def search(
    url: str,
    *,
    timeout: float = 30.0,
    min_similarity: float = 0.7,
) -> dict:
    """拿一张图的地址去 trace.moe 查。返回 ``{"url", "error", "quota", "used", "results", "best"}``。

    **不抛异常**：下载失败、额度用完、trace.moe 报错，全落进 ``error``，
    由工具层翻成一句人话——工具挂了不该让整轮对话崩掉。

    ``min_similarity`` 以下的候选直接丢掉：给模型一串 0.3 的噪声，它反而会挑一个错的。
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return _pack(text, error="图片地址不合法")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return _pack(text, error="图片地址不是 http/https")

    cached = _cache_get(text)
    if cached is not None:
        logger.debug(f"[image] 命中缓存: {text[:60]}")
        return cached

    try:
        data = await _download(text, timeout)
    except httpx.TimeoutException:
        return _pack(text, error=f"下载图片超时（{timeout:g} 秒）")
    except httpx.HTTPStatusError as exc:
        return _pack(text, error=f"下载图片失败：HTTP {exc.response.status_code}")
    except Exception as exc:  # noqa: BLE001 — 网络/格式问题一律「查不到」
        return _pack(text, error=f"下载图片失败：{exc}")

    try:
        resp = await _post_image(data, timeout)
    except httpx.TimeoutException:
        return _pack(text, error=f"以图搜番超时（{timeout:g} 秒）")
    except Exception as exc:  # noqa: BLE001
        return _pack(text, error=f"以图搜番请求失败：{exc}")

    if resp.status_code == 429:
        return _pack(text, error="今天的免费额度用完了（trace.moe 每天 100 次）")
    if resp.status_code >= 400:
        return _pack(text, error=f"trace.moe 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError:
        return _pack(text, error="trace.moe 返回的不是 JSON")

    if payload.get("error"):
        return _pack(text, error=f"trace.moe：{payload['error']}")

    quota, used = payload.get("quota"), payload.get("quotaUsed")
    rows = [
        _normalize(item)
        for item in (payload.get("result") or [])
        if isinstance(item, dict)
    ]
    rows = [row for row in rows if row["similarity"] >= float(min_similarity)]
    rows.sort(key=lambda row: -row["similarity"])
    found = _pack(text, quota=quota, used=used, rows=rows[:5])
    _cache_put(text, found)
    logger.info(
        f"[image] 以图搜番 {len(found['results'])} 条"
        + (f"，首条 {found['best']['title'][:24]} sim={found['best']['similarity']:.2f}"
           if found["best"] else "（都低于阈值）")
    )
    return found


def format_result(found: dict) -> str:
    """拼成给模型看的一段。**带判据和下一步**，不然它只会照抄第一个标题。"""
    best = found.get("best")
    if not best:
        return (
            "以图搜番没有匹配结果。trace.moe 只索引动画正片——插画、同人图、漫画分镜、"
            "游戏截图、真人照片本来就查不到。别猜作品名，直接告诉群友你认不出来。"
        )
    head = (
        f"以图搜番（trace.moe）最可能：《{best['title']}》"
        f"{('，' + str(best['year'])) if best.get('year') else ''}"
        f"{('，' + best['format']) if best.get('format') else ''}"
        f"，{best['episode']} {best['at']}（从 {best['at']} 到 {best['to']}），"
        f"相似度 {best['similarity']:.2f}（{tier_of(best['similarity'])}）。"
    )
    lines = [head]
    others = [row for row in found.get("results", [])[1:] if row["title"] != best["title"]]
    if others:
        lines.append("其它候选：")
        for index, row in enumerate(others[:3], 2):
            lines.append(
                f"{index}. 《{row['title']}》{row['episode']} {row['at']}"
                f"（相似度 {row['similarity']:.2f}）"
            )
    lines.append(
        "判据：相似度 ≥0.90 基本就是这部；0.80~0.90 很可能是；更低只能当线索，"
        "要跟群友说清楚是猜的。"
    )
    lines.append(
        "下一步：拿这个**作品名**去 web_search 查角色（例如「<作品名> 登场角色」），"
        "别再用「发色 + 眼镜 + 瞳色」这类形容词组合去搜——那种查询搜索引擎会拆成单个词，"
        "返回的通常是颜色表、词典之类无关内容。"
    )
    if found.get("quota") is not None:
        quota, used = found.get("quota"), found.get("used")
        left = quota
        try:
            left = int(quota) - int(used or 0)
        except (TypeError, ValueError):
            pass
        lines.append(f"额度：还剩 {left} 次（今天已用 {used} 次）。")
    return "\n".join(lines)

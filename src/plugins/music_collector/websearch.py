"""``web_search`` 工具的后端：搜一次，返回几条「标题 + 摘要 + 网址」。

移植自 ``standalone-websearch/websearch.mjs``（WorkBuddy CLI 的 WebSearch 契约还原版），
只保留 Bing HTML 端点——DuckDuckGo 的 ``html.duckduckgo.com`` 国内连不通（原项目实测超时），
留着只是多一条永远走不到的分支。

和原版比改了三处，都是为本项目的场景：

1. **不要 Node**：原版靠子进程跑 ``.mjs``，本项目是纯 Python，直接用 httpx + 正则，
   和 :mod:`webtool` 同一套网络栈（也不用赌服务器上有没有 node）；
2. **输出压成一行一条**：``tools.dispatch()`` 是**从尾部**截到 ``tool_chars`` 的，原版
   markdown（每条带 ``**URL:**`` 和 ``---``）5 条约 2000 字，很容易被砍掉后两条，
   模型会以为「只搜到 3 条」；
3. **加了限流**：Bing 对机房 IP 会返回验证页，一次对话里连搜五次基本就废了。全局最小
   间隔 :data:`MIN_INTERVAL` 秒，要等太久就直接让模型稍后再试（别让一次回复卡十几秒）。

解析规则（``<li class="b_algo">`` → ``h2 > a`` + ``p``）随时可能变——Bing 一改版这里就会
搜出 0 条。失效时只改 :func:`_parse_bing`，上游接口不用动。
"""

import asyncio
import base64
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import httpx
from nonebot.log import logger

#: 一次最多要几条（模型传超了按这个截）
MAX_RESULTS = 8
#: 默认条数
DEFAULT_RESULTS = 5
#: 引擎侧按「要的条数 × 这个倍数」抓，至少 :data:`_MIN_POOL` 条。
#: 抓少了不行：本地要做协议/域名清洗，清完可能只剩一两条
_POOL_FACTOR = 4
_MIN_POOL = 20
#: 两次搜索之间的最小间隔（秒）。防 Bing 反爬，也顺手压住「一次回复连搜五遍」
MIN_INTERVAL = 1.0
#: 排队等超过这么多秒就放弃，让模型自己说「稍后再搜」——别让一次回复干等
_MAX_WAIT = 3.0

#: 搜索请求的 UA 与语言。**和 ``webtool`` 分开写**：那边要抓的是普通网页，
#: 这边要装得像浏览器访问 Bing，多带 Accept 才不容易被给精简页
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

#: 引擎分发表。加新引擎只要写一个 ``async def _search_xxx(query, count, timeout)``
#: 再登记进来，调用方（工具层 / 配置 / 前端）一处都不用改
ENGINES = {"bing": "_search_bing"}

#: 加在查询里会让 Bing 改写成「新闻式」查询、结果反而完全不相干的词。
#: 实测（2026-10）：「尊界V800 刹车」能搜到刹车踏板断裂的报道，
#: 换成「尊界V800 刹车事件」就变成「尊」字的字典释义——这种素材喂给模型，
#: 它会一本正经地拿字典条目回答汽车事故。所以宁可重试或说没搜到。
_NOISE_WORDS = (
    "事件", "怎么回事", "什么情况", "怎么样了", "发生了什么",
    "新闻", "最新消息", "最新进展", "后续",
)

#: 取查询里的「实词」：2 字以上的中文片段 + 英文/数字词（用来判断结果是否跑偏）
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}")


# ---------------------------------------------------------------- 限流

_lock = asyncio.Lock()
_next_at = 0.0


async def throttle(interval: float = MIN_INTERVAL) -> bool:
    """拿到「现在可以搜」的许可。要等太久返回 ``False``。

    用事件循环的单调时钟，不受系统时间调整影响。等待期间只是 ``await sleep``，
    不会卡住别的群——真并发上来时它们会在这里排队，所以有 :data:`_MAX_WAIT` 兜底。

    ``interval`` 由 ``llm.search_interval`` 传入（``0`` = 不限）。
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


def reset_throttle() -> None:
    """清掉限流状态（测试用）。"""
    global _next_at
    _next_at = 0.0


# ---------------------------------------------------------------- 抓取

async def _search_bing(query: str, count: int, timeout: float) -> list[dict]:
    """问 Bing 要 HTML 结果页，解析成 ``[{title, url, snippet}]``。"""
    async with httpx.AsyncClient(
        timeout=float(timeout), headers=_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": str(count), "setlang": "zh-hans"},
        )
        resp.raise_for_status()
        html = resp.text
    return _parse_bing(html)


# ---------------------------------------------------------------- 解析

#: ``<li class="b_algo"`` 后面还跟着 data-id 等属性，**不能要求紧接着 ``>``**（原版注释里的坑）
_BLOCK_RE = re.compile(r'<li class="b_algo"[^>]*>(.*?)</li>', re.S)
_TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIP_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _parse_bing(html: str) -> list[dict]:
    rows: list[dict] = []
    for match in _BLOCK_RE.finditer(html or ""):
        block = match.group(1)
        anchor = _TITLE_RE.search(block)
        if not anchor:
            continue
        title = decode_entities(anchor.group(2))
        url = _real_url(decode_entities(anchor.group(1)))
        if not title or not _usable(url):
            continue
        para = _SNIP_RE.search(block)
        rows.append(
            {
                "title": title,
                "url": url,
                "snippet": decode_entities(para.group(1)) if para else "",
            }
        )
    return rows


def _usable(url: str) -> bool:
    """只留 http/https 且带主机名的地址。``javascript:`` / 相对路径全部丢掉。"""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


# ---------------------------------------------------------------- 跑偏检测

def query_tokens(query: str) -> list[str]:
    """从查询里取实词（2 字以上，纯数字不算）。

    用来判断「结果跟查询有没有关系」。纯数字排除掉是因为 ``17`` 这种太容易
    在别的数字里撞上，反而会把跑偏的结果当成相关的。
    """
    tokens = []
    for token in _TOKEN_RE.findall(query or ""):
        stripped = token.strip()
        if stripped.isdigit():
            continue
        if stripped not in tokens:
            tokens.append(stripped)
    return tokens


def looks_off_topic(rows: list[dict], tokens: list[str]) -> bool:
    """结果的标题/摘要里**一条都没提到**查询里的实词 → 判定跑偏。

    宁可判它跑偏（多说一句「没搜到」），也不要把字典释义、同名商品这类
    无关内容交给模型——它没法分辨，只会照着编。
    """
    if not rows or not tokens:
        return False
    blob = " ".join(
        f"{row.get('title') or ''} {row.get('snippet') or ''}" for row in rows
    ).lower()
    return not any(token.lower() in blob for token in tokens)


def simplify_query(query: str) -> str:
    """去掉 :data:`_NOISE_WORDS` 再试。没得可去就返回空串（表示「不用重试」）。"""
    out = query or ""
    for word in _NOISE_WORDS:
        out = out.replace(word, " ")
    out = " ".join(out.split()).strip(" 的了呢吗啊，,、")
    return out if out and out != (query or "").strip() else ""


def _real_url(href: str) -> str:
    """Bing 有时把结果包成 ``bing.com/ck/a?…&u=a1<base64url>``，这里解出真实地址。

    不解也能用（``fetch_url`` 会跟着跳），但把 ``ck/a`` 那种长链接丢给模型，它既看不懂
    也容易抄错；而且跳转链一断就彻底失效。
    """
    raw = (href or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.endswith("bing.com") and parts.path.startswith("/ck/"):
        values = parse_qs(parts.query).get("u") or []
        if values and values[0].startswith("a1"):
            token = values[0][2:]
            try:
                padded = token + "=" * (-len(token) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — 解不出来就用原链接，不值得报错
                return raw
            # ⚠️ base64 会**默默忽略**非法字符，解出空串而不报错；
            # 结果不像网址时也不能用，否则模型会拿到一个空地址
            return decoded if decoded.startswith(("http://", "https://")) else raw
    return raw


def decode_entities(text: str) -> str:
    """去标签 + 常见实体还原（移植自原版，足够应付 Bing 摘要里的 ``&amp;`` / ``&#39;``）。"""
    body = (
        re.sub(r"<[^>]+>", "", text or "")
        .replace("&amp;", "&")
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
        .replace("&ensp;", " ")
        .replace("&emsp;", " ")
        .replace("&hellip;", "…")
        .replace("&mdash;", "—")
        .replace("&ndash;", "–")
    )
    # 数字实体放最后：``&#65;`` 这种不止出现在摘要里，Bing 的标题也常有
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), body).strip()


# ---------------------------------------------------------------- 对外

async def _fetch(name: str, query: str, count: int, timeout: float) -> tuple[list[dict], str]:
    """抓一次，把各种失败翻成一句中文原因。返回 ``(结果, 错误原因)``。

    抽出来是因为「跑偏重试」还要再抓一次，而异常处理不该抄两遍。
    """
    func = globals().get(ENGINES.get(name, ""), None)
    if not callable(func):
        return [], f"不支持的引擎 {name}"
    try:
        return await func(query, count, timeout), ""
    except httpx.TimeoutException:
        logger.debug(f"[search] 超时（{timeout:g}s）: {query[:40]}")
        return [], f"搜索超时（{timeout:g} 秒）"
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.warning(f"[search] 搜索引擎返回 HTTP {code}: {query[:40]}")
        # 403/429 基本都是「搜得太密被限流」，给模型一句能转述的话
        hint = "（可能被搜索引擎限流了，过一会儿再试）" if code in (403, 429) else ""
        return [], f"搜索引擎返回 HTTP {code}{hint}"
    except Exception as exc:  # noqa: BLE001 — 网络错误 / 解析异常都算「搜不到」
        logger.warning(f"[search] 搜索失败: {exc}")
        return [], f"搜索请求失败：{exc}"


async def search(
    query: str,
    *,
    max_results: int = DEFAULT_RESULTS,
    timeout: float = 20.0,
    engine: str = "bing",
) -> dict:
    """搜一次。返回 ``{"query", "engine", "results", "error"}``。

    **不抛异常**：超时、被反爬、Bing 改版导致解析出 0 条，全部落进 ``error``，
    由工具层翻成一句人话回给模型——工具挂了不该让整轮对话崩掉。

    **跑偏也算失败**：Bing 偶尔会把查询改写成别的意思（例如加个「事件」就跑去
    查「尊」字的字典释义）。这时先去掉 :data:`_NOISE_WORDS` 重试一次，还不对就
    返回空——「没搜到」比「给你一堆无关内容」安全得多。
    """
    text = (query or "").strip()
    want = max(1, min(MAX_RESULTS, int(max_results or DEFAULT_RESULTS)))
    # 本地清洗会淘汰结果，所以引擎侧多要一批再截
    pool = max(want * _POOL_FACTOR, _MIN_POOL)
    name = (engine or "bing").strip().lower()

    def pack(rows: list[dict], error: str = "") -> dict:
        return {"query": text, "engine": name, "results": rows[:want], "error": error}

    if name not in ENGINES:
        return pack([], f"不支持的引擎 {name}")

    rows, error = await _fetch(name, text, pool, timeout)
    if error:
        return pack([], error)
    if not rows:
        logger.info(f"[search] 解析出 0 条: {text[:40]}（Bing 可能返回了验证页或改版了）")
        return pack([])

    tokens = query_tokens(text)
    if looks_off_topic(rows, tokens):
        simple = simplify_query(text)
        logger.info(
            f"[search] 「{text[:30]}」结果跑偏（首条是「{(rows[0].get('title') or '')[:24]}」）"
            + (f"→ 去掉口语词改用「{simple[:30]}」重试" if simple else "，且没有可简化的写法")
        )
        if simple:
            again, again_error = await _fetch(name, simple, pool, timeout)
            if not again_error and not looks_off_topic(again, tokens):
                logger.info(f"[search] 重试成功，拿到 {len(again)} 条")
                return pack(again)
        return pack([], "没搜到相关内容（搜索引擎返回的是无关结果），换个说法再试")

    return pack(rows)


def _clip(text: str, limit: int) -> str:
    body = " ".join((text or "").split())
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit].rstrip() + "…"


def format_results(query: str, rows: list[dict], snippet_chars: int = 200) -> str:
    """拼成给模型看的一段。一行一条，``标题 — 摘要（网址）``。

    不用原版的 markdown：``dispatch`` 从尾部截断，带 URL 小标题和分隔线时
    5 条能吃满 2000 字，被砍掉的正好是最后几条结果。
    """
    lines = [f"搜索「{query}」共 {len(rows)} 条："]
    for index, row in enumerate(rows, 1):
        snippet = _clip(str(row.get("snippet") or ""), int(snippet_chars))
        head = f"{index}. {row.get('title') or '（无标题）'}"
        if snippet:
            head += f" — {snippet}"
        lines.append(f"{head}（{row.get('url') or ''}）")
    lines.append("要看某条的具体内容，用 fetch_url 打开它的网址。")
    return "\n".join(lines)


def first_url(rows: list[dict]) -> Optional[str]:
    """第一条结果的网址（测试与调试用）。"""
    return rows[0]["url"] if rows else None

"""``web_search`` 工具的后端：搜一次，返回几条「标题 + 摘要 + 网址」。

两个引擎，由 ``llm.search_engine`` 选：

- ``wb``（默认）：跑仓库里 vendored 的 ``vendor/workbuddy-tools``（node CLI），
  见那边的 ``PATCHES.md``。它自己带本地 Bing provider，**不需要任何凭据**；
- ``bing``：以前那套纯 Python 的 Bing HTML 抓取，留着以便这台机器没有 node 时兜底。

两个引擎共用同一条流水线：限流 → 抓 → 一行一条格式化。**相关性判断交给模型**——
这里只负责「把搜索结果原样拿回来」，不猜哪条有用、也不替模型拦结果。理由：本地没有
查询改写能力，靠「标题里有没有查询词」这种机械规则判相关性误杀率极高（实测 2026-11-05：
38 次搜索里 29 次被旧规则判跑偏，其中「350234 是什么编号」返回的三条全对却被整批丢弃）。
改成由提示词要求模型自己核对：结果照给，模型负责判断相关性、必要时用 ``fetch_url``
打开原网页验证。

最早的来源是 ``standalone-websearch/websearch.mjs``（WorkBuddy CLI 的 WebSearch 契约还原版），
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
import json
import os
import re
from pathlib import Path
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

#: ``vendor/workbuddy-tools`` 的相对位置（相对仓库根）
_WB_HOME = Path("vendor") / "workbuddy-tools"
#: 仓库根：``src/plugins/music_collector/websearch.py`` 往上四级
_ROOT = Path(__file__).resolve().parents[3]
#: ``wb`` 引擎用的 node 可执行文件（留空 = PATH 里的 ``node``）
DEFAULT_NODE = "node"

#: 引擎分发表。加新引擎只要写一个 ``async def _search_xxx(query, count, timeout)``
#: 再登记进来，调用方（工具层 / 配置 / 前端）一处都不用改
ENGINES = {"wb": "_search_wb", "bing": "_search_bing"}

#: ``freshness`` 的合法值：``d1``~``d30`` / ``m1``~``m12`` / ``y1``~``y5``，数字可省。
#: 契约来自 vendored 工具（``WB_SEARCH_MONTH_RE`` 那套），只有 ``wb`` 引擎真支持
FRESHNESS_RE = re.compile(r"^(?:d(?:[1-9]|[12]\d|30)?|m(?:[1-9]|1[0-2])?|y[1-5]?)$")


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


class WbError(Exception):
    """``wb`` 引擎自己报的错：node 不在、工具目录不对、CLI 退出码非 0、输出不是 JSON。

    单独一个类型是为了让 :func:`_fetch` 把中文原因原样交给模型，
    不被兜底成「搜索请求失败：...」。
    """


def resolve_tools_dir(explicit: str = "") -> Path:
    """``vendor/workbuddy-tools`` 在哪。留空用仓库里那份；相对路径按仓库根解析。"""
    text = (explicit or "").strip()
    if not text:
        return _ROOT / _WB_HOME
    path = Path(text)
    return path if path.is_absolute() else _ROOT / path


async def _search_wb(
    query: str,
    count: int,
    timeout: float,
    *,
    node: str = "",
    tools_dir: str = "",
    freshness: str = "",
) -> list[dict]:
    """跑 vendored 的 ``bin/wb.mjs search --json``，把 JSON 结果换成 ``[{title, url, snippet}]``。

    **只要 JSON、不要它的文本**：那边是 markdown（URL 写两遍、每条另起 ``**URL:**``），
    5 条就 1500 字，比项目一行一条多一半，而 ``tools.dispatch()`` 是从尾部截
    ``tool_chars`` 的——直接用它的文本会把最后几条吃掉。

    ``WB_MAX_RESULTS`` 传的是「引擎侧多要几条」，由 :func:`search` 决定（本地还要清洗）。
    ``freshness``（``d7`` / ``m6`` / ``y1``）原样交给 CLI，只有它支持时间过滤。
    """
    exe = (node or "").strip() or DEFAULT_NODE
    cli = resolve_tools_dir(tools_dir) / "bin" / "wb.mjs"
    if not cli.is_file():
        raise WbError(f"找不到搜索工具（{cli}），检查 llm.search_tools_dir")
    env = {
        **os.environ,
        "WB_SEARCH_BACKEND": "bing",
        "WB_MAX_RESULTS": str(int(count)),
    }
    argv = [exe, str(cli), "search", query, "--json"]
    if freshness:
        argv += ["--freshness", str(freshness)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise WbError(
            f"找不到 node（{exe}）：装一个，或把 llm.search_node 指到现成的，或切回 bing 引擎"
        )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WbError(f"搜索超时（{timeout:g} 秒）")

    if proc.returncode != 0:
        reason = err.decode("utf-8", "replace").strip().splitlines()
        raise WbError(reason[-1] if reason else f"搜索工具退出码 {proc.returncode}")
    text = out.decode("utf-8", "replace").strip()
    if not text:
        raise WbError("搜索工具没有输出")
    try:
        payload = json.loads(text)
    except ValueError:
        raise WbError("搜索工具输出的不是 JSON")

    rows = (payload.get("rawResponse") or {}).get("results") or []
    found: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "")
        url = str(row.get("url") or "")
        if not title or not _usable(url):
            continue
        found.append({"title": title, "url": url, "snippet": str(row.get("snippet") or "")})
    return found


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

async def _fetch(
    name: str, query: str, count: int, timeout: float, extra: Optional[dict] = None
) -> tuple[list[dict], str]:
    """抓一次，把各种失败翻成一句中文原因。返回 ``(结果, 错误原因)``。

    ``extra`` 是引擎私有参数（``wb`` 要 node 路径、工具目录和 freshness），原样透传。
    """
    func = globals().get(ENGINES.get(name, ""), None)
    if not callable(func):
        return [], f"不支持的引擎 {name}"
    try:
        return await func(query, count, timeout, **(extra or {})), ""
    except WbError as exc:
        logger.warning(f"[search] wb 引擎失败: {exc}（{query[:40]}）")
        return [], str(exc)
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
    node: str = "",
    tools_dir: str = "",
    freshness: str = "",
) -> dict:
    """搜一次。返回 ``{"query", "engine", "results", "error"}``。

    ``engine`` 的**函数默认**是 ``bing``（纯 Python，测试和兜底用）；线上由
    ``llm.search_engine`` 传进来，缺省 ``wb``。加了新引擎记得在这儿登记两份。

    ``node`` / ``tools_dir`` / ``freshness`` 只有 ``wb`` 用得到，其它引擎忽略。

    **不抛异常**：超时、被反爬、Bing 改版导致解析出 0 条，全部落进 ``error``，
    由工具层翻成一句人话回给模型——工具挂了不该让整轮对话崩掉。

    **不做相关性过滤**：引擎返回什么就交什么（清洗掉 ``javascript:`` 这类坏链接而已）。
    「这条结果跟问题有没有关系」由模型判断，配合 ``format_results`` 末尾的提醒
    和 ``fetch_url`` 核对。本地判相关性的机械规则误杀率太高（实测 76%），已删除。
    """
    text = (query or "").strip()
    want = max(1, min(MAX_RESULTS, int(max_results or DEFAULT_RESULTS)))
    # 引擎侧多要一批：本地还要剔掉没标题/坏链接的行
    pool = max(want * _POOL_FACTOR, _MIN_POOL)
    name = (engine or "bing").strip().lower()
    extra = (
        {"node": node, "tools_dir": tools_dir, "freshness": freshness}
        if name == "wb" else {}
    )

    def pack(rows: list[dict], error: str = "") -> dict:
        return {"query": text, "engine": name, "results": rows[:want], "error": error}

    if name not in ENGINES:
        return pack([], f"不支持的引擎 {name}")

    rows, error = await _fetch(name, text, pool, timeout, extra)
    if error:
        return pack([], error)
    if not rows:
        logger.info(f"[search] 解析出 0 条: {text[:40]}（搜索引擎可能返回了验证页或改版了）")
        return pack([])

    logger.info(f"[search] 「{text[:30]}」→ {len(rows[:want])} 条")
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
    lines.append(
        "这些是搜索引擎原样给的结果，**相关性你自己判断**：标题/摘要明显跟问题无关"
        "（例如搜「某车 刹车」返回「某」字的字典释义、搜角色名返回颜色表），就不要当答案用——"
        "换更具体的实体词（作品名/人名/官方全名）重搜，或直接说搜不到。"
        "摘要也可能不全或过时：关键数字、日期、结论用 fetch_url 打开那条网址核对后再答。"
    )
    return "\n".join(lines)


def first_url(rows: list[dict]) -> Optional[str]:
    """第一条结果的网址（测试与调试用）。"""
    return rows[0]["url"] if rows else None

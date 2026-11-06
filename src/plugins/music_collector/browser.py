"""``browse`` 工具的后端：开一个**真实 Chromium** 读动态渲染的网页。

## 为什么要它

``webtool.fetch_page``（httpx + trafilatura）对静态页又快又省，但有两类页面它天生
打不开，实测都验过：

1. **JS 渲染的空壳**：知乎会跳 ``/account/unhuman`` 安全验证、微博几乎全靠前端渲染，
   裸 HTTP 拿到的是空 div；
2. **机房 IP 直接吃 412**：B 站视频页从服务器出口无论换 UA / Referer / 设备 cookie
   都是 412（``webtool`` 里为此专门绕去 ``api.bilibili.com``）。

换成真浏览器之后这两类都好了：B 站视频页能拿到 1400 字正文、``m.weibo.cn`` 能拿到
1000 字。代价是要在服务器上跑一个常驻 Chromium（见 README「真实浏览器」节）。

## 为什么不用 playwright / crawl4ai

- 这台机器上 **pip 装不上 playwright**（索引能访问，但解析出 0 个版本），而且引入它
  就要赌 Playwright 版本和系统 Chromium 133 的 CDP 兼容性；
- 项目 venv 里**本来就有 ``websockets``**（uvicorn 的依赖），直接连 CDP 零新依赖；
- 裸 CDP 还有个附带好处：**不带任何自动化标志**，``navigator.webdriver`` 是 false，
  指纹比 puppeteer/playwright 的默认启动更正常。

## 安全：SSRF 的形态变了

以前（``webtool``）是"下载前查一次 DNS + 每跳重定向再查"。**浏览器不行**——它自己
跟重定向、自己加载子资源，模型只要让它打开一个构造过的 URL，就能读到
``169.254.169.254``（阿里云元数据）或 ``127.0.0.1:8765``（宝塔面板）。

所以这里用 CDP 的 ``Fetch`` 域**拦每一个请求**：取 URL 的 host → 域名白名单 →
公网校验（内网/环回/链路本地/保留段一律拒）→ 只放行 http/https。字体和音视频直接
拦掉（省内存也省流量，验证码图片仍然放行，否则人工过验证时看不到图）。

## 人工接管

检测到验证页不是故障，是设计行为：返回 ``needs_human`` + 一句人话 + 截图路径，
模型会告诉用户"点 VNC 链接过一下"。用户过完再让模型读一次同一 URL——``--user-data-dir``
是持久化的，cookie 留得住，同一站点几周内不用再过。
"""

import asyncio
import base64
import contextlib
import ipaddress
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx
from nonebot.log import logger

from . import webtool
from .config import DATA_DIR

#: 默认 CDP 地址（只该是回环，见模块文档）
CDP_DEFAULT = "http://127.0.0.1:9222"

#: 浏览器视口。和 x11vnc 里看到的窗口尺寸对齐，人工过验证时截图才对得上
VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 900

#: ``document.readyState`` 轮询间隔（秒）
_POLL_INTERVAL = 0.5
#: 正文长度连续几次不变才算"渲染完"（动态页会分好几批塞内容）
_STABLE_ROUNDS = 2

#: 渲染后的 HTML 用 trafilatura 抽正文，抽出来至少这么多字才信它（否则退回 innerText）。
#: 门槛给低一点：论坛/列表页抽不出"文章"，但那正是 innerText 表现更好的场景
_MIN_EXTRACT_CHARS = 200

#: 这些资源类型直接拦掉：字体和音视频对"读文字"没用，但很吃内存和带宽。
#: **图片不能拦**——有些验证码本身就是一张图，拦了人工过验证时就看不见了
_BLOCK_RESOURCE_TYPES = frozenset({"Font", "Media"})

#: DNS 结论缓存多久（秒）。一次浏览会发几十上百个请求，同一 host 不能每次都解析
_HOST_CACHE_TTL = 300.0
#: host → (过期时刻, 是否公网)
_HOST_CACHE: dict[str, tuple[float, bool]] = {}

#: 一次调用里允许的动作类型
_ALLOWED_STEPS = frozenset({"click", "type", "scroll", "wait"})


# ---------------------------------------------------------------- URL / 域名


def parse_allow_hosts(raw: str) -> list[str]:
    """``"a.com, .b.com ,,c.com"`` → ``['a.com', '.b.com', 'c.com']``（小写、去空、去重）。"""
    hosts: list[str] = []
    for item in str(raw or "").replace(";", ",").replace("\n", ",").split(","):
        host = item.strip().lower().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def host_matches(host: str, patterns: list[str]) -> bool:
    """``host`` 是否命中白名单。**空列表 = 不限制**（仍会做公网校验）。

    - ``example.com`` 只匹配它自己（不含子域）；
    - ``.example.com`` 匹配 ``example.com`` 与所有子域；
    - ``*.example.com`` 等同于 ``.example.com``（省得用户猜写法）。
    """
    if not patterns:
        return True
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    for raw in patterns:
        pat = str(raw or "").strip().lower().rstrip(".")
        if not pat:
            continue
        if pat.startswith("*."):
            pat = "." + pat[2:]
        if pat.startswith("."):
            base = pat[1:]
            if name == base or name.endswith(pat):
                return True
        elif name == pat:
            return True
    return False


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _usable_url(url: str) -> bool:
    """只认 http/https + 有主机名。``data:`` / ``about:`` / ``javascript:`` 一律不要。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def is_public_ip(addr: str) -> bool:
    """这个 IP 字面量是不是公网地址（内网/环回/链路本地/保留段/组播全拒）。"""
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_is_public(host: str) -> bool:
    """同步解析 + 判定（丢线程池里跑）。解析出来的**每一个**地址都必须是公网。"""
    import socket

    name = (host or "").strip().strip("[]")
    if not name:
        return False
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        if not is_public_ip(str(info[4][0])):
            return False
    return True


async def host_is_public(host: str) -> bool:
    """带缓存的公网校验：一次浏览几十个请求，不能每个都去解析一次 DNS。"""
    name = (host or "").strip().lower()
    if not name:
        return False
    hit = _HOST_CACHE.get(name)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, _resolve_is_public, name)
    _HOST_CACHE[name] = (now + _HOST_CACHE_TTL, ok)
    return ok


def clear_host_cache() -> None:
    """清 DNS 结论缓存（测试与排障用）。"""
    _HOST_CACHE.clear()


# ---------------------------------------------------------------- 验证页识别

#: URL 里出现这些片段 = 安全验证 / 反爬拦截页
_CHALLENGE_URL = (
    "wappass.baidu.com",
    "/account/unhuman",
    "/unhuman",
    "/captcha",
    "/verify",
    "/challenge",
    "antispider",
    "/security-verify",
    "sec.douban.com",
    "//verify.",
)

#: 标题里出现这些 = 验证页（小写比较）
_CHALLENGE_TITLE = (
    "安全验证",
    "验证码",
    "人机验证",
    "滑动验证",
    "验证中心",
    "just a moment",
    "attention required",
    "access denied",
    "security check",
    "are you a robot",
    "verify you are human",
    "checking your browser",
)

#: 正文里出现这些 = 验证控件（小写比较）
_CHALLENGE_TEXT = (
    "请完成安全验证",
    "请完成验证",
    "拖动滑块",
    "请按住滑块",
    "滑动至最右",
    "点击进行验证",
    "请输入验证码",
    "请填写验证码",
    "环境异常",
    "异常访问",
    "完成下方验证",
    "verify you are human",
    "checking your browser",
)

#: 登录/注册页：**按 URL 判**，不看正文长短。
#: 实测知乎会把未登录用户 302 到 ``/signin``，那一页正文有 467 字（"验证码登录 /
#: 密码登录 / 获取短信验证码 …"），靠"正文很短"根本判不出来，模型会把登录向导
#: 当成文章内容回答。所以这类路径一律当墙。
_AUTH_URL = (
    "/signin",
    "/login",
    "/passport",
    "/sign_up",
    "/signup",
    "/register",
    "/account/unhuman",
    "/account/login",
    "/auth/",
    "/oauth/",
)

#: 登录墙（兜底）：标题像登录页且正文短得离谱。真登录墙不会给你正文
_LOGIN_TITLE = ("登录", "sign in", "log in")
_LOGIN_MAX_CHARS = 200


def detect_challenge(title: str, url: str, text: str) -> str:
    """判断这一页是不是"模型自己过不去"的页面。返回 ``''`` 表示正常。

    返回的字符串直接给模型看，所以要写成人话，并暗示「这事得人来做」。

    宁可漏判也不要误判：正常文章里出现「登录」「验证」这类词很常见，一旦误判，
    模型会拿着一张能读的页面告诉用户「需要验证」，等于白抓一次。**但登录页例外**——
    知乎实测会被 302 到 ``/signin``，那一页有近 500 字全是登录向导，不按 URL 拦
    就会把登录页当正文交出去。
    """
    low_url = (url or "").lower()
    low_title = (title or "").lower()
    body = (text or "").strip()
    low_text = body.lower()

    for mark in _AUTH_URL:
        if mark in low_url:
            return "这一页被重定向到登录/注册页面（%s），登录后才能看真实内容" % mark

    for mark in _CHALLENGE_URL:
        if mark in low_url:
            return "这一页是安全验证/反爬拦截页（网址里带 %s），你自己过不去" % mark.strip("/")
    for mark in _CHALLENGE_TITLE:
        if mark in low_title:
            return "页面标题是「%s」，是人机验证页，你自己过不去" % (title or "").strip()[:40]
    for mark in _CHALLENGE_TEXT:
        if mark in low_text:
            return "页面里出现了验证控件（%s），需要人工操作" % mark

    if any(m in low_title for m in _LOGIN_TITLE) and len(body) < _LOGIN_MAX_CHARS:
        return "这一页要求登录后才能看（正文只有 %d 字），需要人工登录一次" % len(body)
    return ""


# ---------------------------------------------------------------- 文本处理


def clip(text: str, max_chars: int) -> str:
    """按字数截断并加省略号（``max_chars <= 0`` 表示不截）。"""
    body = text or ""
    if max_chars and max_chars > 0 and len(body) > max_chars:
        return body[:max_chars].rstrip() + "…"
    return body


def normalize_text(raw: str) -> str:
    """把 innerText 压成"有意义的行"：去行尾空白、连续空行合一、整行空白丢掉。"""
    lines: list[str] = []
    blank = False
    for line in (raw or "").splitlines():
        item = line.strip()
        if not item:
            if not blank and lines:
                blank = True
                lines.append("")
            continue
        blank = False
        lines.append(item)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------- CDP


class CdpError(Exception):
    """连不上浏览器 / CDP 调用失败。单独一个类型，好翻成中文原因。"""


class Cdp:
    """最小 CDP 客户端：一条 WebSocket + 按 id 匹配响应 + 并发处理事件。

    **只要最小的那部分**：开页、导航、取值、点、输入、截图、拦请求。
    够用就行，不追求把协议包完。
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._reader: Optional[asyncio.Task] = None
        self._event_tasks: set[asyncio.Task] = set()
        self._on_event = None

    async def start(self, on_event=None) -> None:
        self._on_event = on_event
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif self._on_event is not None:
                    task = asyncio.create_task(self._safe_event(msg))
                    self._event_tasks.add(task)
                    task.add_done_callback(self._event_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 连接断了就是断了，让调用方超时
            logger.debug(f"[browse] CDP 读循环结束: {exc}")
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CdpError("浏览器连接已断开"))

    async def _safe_event(self, msg: dict) -> None:
        try:
            await self._on_event(msg)
        except Exception as exc:  # noqa: BLE001 — 事件处理失败不能带崩读循环
            logger.debug(f"[browse] 事件处理失败: {exc}")

    async def call(self, method: str, params: Optional[dict] = None, timeout: float = 25.0):
        if self._ws is None:
            raise CdpError("浏览器连接已断开")
        async with self._send_lock:
            self._next_id += 1
            mid = self._next_id
            fut = asyncio.get_running_loop().create_future()
            self._pending[mid] = fut
            await self._ws.send(
                json.dumps({"id": mid, "method": method, "params": params or {}})
            )
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise CdpError(f"{method} 超时（{timeout:g} 秒没有回应）")
        if "error" in msg:
            raise CdpError(f"{method} 失败：{msg['error']}")
        return msg.get("result") or {}

    async def evaluate(self, expression: str, timeout: float = 15.0):
        res = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            raise CdpError(f"脚本执行出错：{res['exceptionDetails'].get('text') or ''}")
        return (res.get("result") or {}).get("value")

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(BaseException):
                await self._reader
        for task in list(self._event_tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()


async def _open_target(cdp_url: str, timeout: float) -> dict:
    """``PUT /json/new`` 开一个后台页，返回它的 target 信息（含 webSocketDebuggerUrl）。"""
    url = cdp_url.rstrip("/") + "/json/new?about:blank"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url)
        resp.raise_for_status()
        return resp.json()


async def _close_target(cdp_url: str, target_id: str, timeout: float = 5.0) -> None:
    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(f"{cdp_url.rstrip('/')}/json/close/{target_id}")


async def health(cdp_url: str = "", timeout: float = 5.0) -> tuple[bool, str]:
    """浏览器服务在不在。返回 ``(可用, 中文原因)``。只打 ``/json/version``，不开页。"""
    base = (cdp_url or CDP_DEFAULT).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base}/json/version")
            resp.raise_for_status()
            data = resp.json() or {}
    except httpx.HTTPError as exc:
        return False, (
            f"连不上浏览器（{base}）：{type(exc).__name__}。"
            "服务器上要先起 qqmusic-browser 服务，见 README「真实浏览器」节"
        )
    except ValueError:
        return False, f"浏览器接口返回的不是 JSON（{base}），检查 browser_cdp 配置"
    version = str(data.get("Browser") or "")
    return True, version or "ok"


# ---------------------------------------------------------------- 单页操作


def _is_challenge_event(msg: dict) -> bool:
    return msg.get("method") == "Fetch.requestPaused"


class _Guard:
    """按请求做的 SSRF / 白名单 / 资源类型闸。

    ``fetch_enabled`` 之后每个请求都会以 ``Fetch.requestPaused`` 事件送过来，
    这里决定放行还是拦掉。**这就是浏览器的"每跳校验"**——比 webtool 那套更严，
    因为浏览器连 <img> <script> iframe 都会自己去拿。
    """

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = patterns
        self.blocked: list[str] = []

    async def handle(self, cdp: Cdp, msg: dict) -> None:
        params = msg.get("params") or {}
        request_id = params.get("requestId") or ""
        request = params.get("request") or {}
        url = str(request.get("url") or "")
        kind = str(params.get("resourceType") or "")
        if not request_id:
            return

        reason = ""
        if _usable_url(url):
            host = _host_of(url)
            if not host_matches(host, self.patterns):
                reason = "not-allowed"
            elif kind in _BLOCK_RESOURCE_TYPES:
                reason = "skip-heavy"
            elif not await host_is_public(host):
                reason = "private-or-invalid"
        else:
            # data:/blob:/about: 这些走不到网络，直接放行
            reason = ""

        if reason in ("not-allowed", "private-or-invalid"):
            logger.warning(f"[browse] 拦掉请求（{reason}）: {url[:120]}")
            self.blocked.append(f"{reason}:{url[:120]}")
        with contextlib.suppress(CdpError):
            if reason in ("not-allowed", "private-or-invalid", "skip-heavy"):
                await cdp.call(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                    timeout=10.0,
                )
            else:
                await cdp.call("Fetch.continueRequest", {"requestId": request_id}, timeout=10.0)


async def _wait_rendered(cdp: Cdp, timeout: float) -> None:
    """等页面"真的渲染完"：readyState=complete 且正文长度连续两轮不变。

    只看 ``readyState`` 不够——动态页在 complete 之后还会继续塞内容，
    早读一步就是半页。
    """
    deadline = time.monotonic() + max(1.0, timeout)
    last = -1
    stable = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL)
        with contextlib.suppress(CdpError):
            state = await cdp.evaluate("document.readyState")
            raw = await cdp.evaluate("document.body ? document.body.innerText : ''") or ""
            size = len(raw)
            if state == "complete" and size > 0 and size == last:
                stable += 1
                if stable >= _STABLE_ROUNDS:
                    return
            else:
                stable = 0
            last = size


async def _run_step(cdp: Cdp, step: dict) -> str:
    """执行一个动作。返回空串表示成功，否则返回中文失败原因。"""
    if not isinstance(step, dict):
        return "动作格式不对（要对象）"
    kind = str(step.get("type") or "").strip().lower()
    if kind not in _ALLOWED_STEPS:
        return f"不支持的动作类型 {kind or '(空)'}（只支持 click / type / scroll / wait）"
    selector = str(step.get("selector") or "").strip()

    if kind == "wait":
        ms = int(step.get("ms") or 1000)
        await asyncio.sleep(max(0, min(ms, 10000)) / 1000.0)
        return ""

    if kind == "scroll":
        pixels = int(step.get("y") or 800)
        await cdp.evaluate(f"window.scrollBy(0, {max(-8000, min(8000, pixels))})")
        await asyncio.sleep(0.4)
        return ""

    if not selector:
        return f"{kind} 需要 selector（CSS 选择器）"

    # 只允许 CSS：``javascript:`` 这类串不是选择器，直接拒
    if selector.lower().startswith(("javascript:", "data:")):
        return "selector 必须是 CSS 选择器"

    if kind == "click":
        box = await cdp.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return null; el.scrollIntoView({block:'center'});"
            " const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2, w: r.width, h: r.height}; })()"
            % json.dumps(selector)
        )
        if not box:
            return f"找不到元素 {selector}"
        x, y = float(box.get("x") or 0), float(box.get("y") or 0)
        if x <= 0 and y <= 0:
            # 元素没尺寸（隐藏/零宽）：退回 JS 点击，聊胜于无
            await cdp.evaluate(f"document.querySelector({json.dumps(selector)}).click()")
            await asyncio.sleep(0.5)
            return ""
        # ⚠️ 必须用真鼠标事件：滑块/点选类验证对 el.click() 合成点击是能识别出来的
        for kind_ev in ("mousePressed", "mouseReleased"):
            await cdp.call(
                "Input.dispatchMouseEvent",
                {
                    "type": kind_ev,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                    "buttons": 1 if kind_ev == "mousePressed" else 0,
                },
            )
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        return ""

    # type：先聚焦，再插文本（insertText 会正常触发 input 事件）
    text = str(step.get("text") or "")
    focused = await cdp.evaluate(
        "(() => { const el = document.querySelector(%s); if (!el) return false;"
        " el.focus(); return true; })()" % json.dumps(selector)
    )
    if not focused:
        return f"找不到输入框 {selector}"
    if text:
        await cdp.call("Input.insertText", {"text": text})
    await asyncio.sleep(0.3)
    return ""


async def _screenshot(cdp: Cdp, shots_dir: Path, tag: str = "") -> str:
    """截一张 PNG 存到本地，返回路径（失败返回空串，绝不因此报错）。"""
    try:
        result = await cdp.call("Page.captureScreenshot", {"format": "png"})
        data = result.get("data") or ""
        if not data:
            return ""
        shots_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = re.sub(r"[^0-9A-Za-z_.-]+", "_", (tag or "page"))[:40] or "page"
        path = shots_dir / f"{stamp}-{name}.png"
        path.write_bytes(base64.b64decode(data))
        return str(path)
    except Exception as exc:  # noqa: BLE001 — 截图是辅助，失败不影响主流程
        logger.debug(f"[browse] 截图失败: {exc}")
        return ""


async def browse(
    url: str,
    *,
    steps: Optional[list] = None,
    timeout: float = 30.0,
    max_chars: int = 3000,
    allow_hosts: str = "",
    max_steps: int = 3,
    cdp_url: str = "",
    shots_dir: Optional[Path] = None,
) -> dict:
    """打开一个网页，返回 :func:`format_report` 要的那份字典。**不抛异常。**

    返回键：``ok`` / ``url`` / ``title`` / ``text`` / ``chars`` /
    ``needs_human`` / ``reason`` / ``screenshot`` / ``steps_done`` / ``error``。
    """
    base = (cdp_url or CDP_DEFAULT).rstrip("/")
    out = {
        "ok": False,
        "url": str(url or ""),
        "title": "",
        "text": "",
        "chars": 0,
        "needs_human": False,
        "reason": "",
        "screenshot": "",
        "steps_done": 0,
        "error": "",
        "source": "",
    }
    target = str(url or "").strip()
    if not _usable_url(target):
        out["error"] = "需要一个完整的 http/https 网址"
        return out

    patterns = parse_allow_hosts(allow_hosts)
    host = _host_of(target)
    if not host_matches(host, patterns):
        parts = host.split(".")
        parent = "." + ".".join(parts[-2:]) if len(parts) >= 2 else host
        out["error"] = (
            f"域名 {host} 不在允许列表里（llm.browser_allow_hosts）。"
            f"白名单逐条生效：只放行这一个就填 {host}，要放开整站填 {parent}（带点的写法含子域）"
        )
        return out
    if not await host_is_public(host):
        logger.warning(f"[browse] 拒绝打开内网/非法地址: {target[:100]}")
        out["error"] = "拒绝访问内网 / 环回 / 云元数据之类的地址（SSRF 防护）"
        return out

    ok, why = await health(base, timeout=min(5.0, timeout))
    if not ok:
        out["error"] = why
        return out

    shots = Path(shots_dir) if shots_dir else (DATA_DIR / "browser" / "shots")
    guard = _Guard(patterns)
    cdp: Optional[Cdp] = None
    target_id = ""
    try:
        info = await _open_target(base, timeout=min(10.0, timeout))
        target_id = str(info.get("id") or "")
        ws_url = str(info.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            out["error"] = "浏览器没给出调试地址（CDP 版本不匹配？）"
            return out

        from websockets.asyncio.client import connect as ws_connect

        ws = await asyncio.wait_for(ws_connect(ws_url, max_size=None), timeout=timeout)
        cdp = Cdp(ws)

        async def _on_event(msg: dict) -> None:
            # 只有 Fetch.requestPaused 需要处理：放行 / 拦掉一个请求
            if _is_challenge_event(msg):
                await guard.handle(cdp, msg)

        await cdp.start(on_event=_on_event)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT_WIDTH,
                "height": VIEWPORT_HEIGHT,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        await cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})

        try:
            nav = await cdp.call("Page.navigate", {"url": target}, timeout=timeout)
            if nav.get("errorText"):
                out["error"] = (
                    "页面里的请求被拦了（内网地址或不在允许的域名里）"
                    if guard.blocked
                    else f"浏览器打不开这个地址：{nav['errorText']}"
                )
                return out
        except CdpError as exc:
            if guard.blocked:
                out["error"] = "页面里的请求被拦了（内网地址或不在白名单），没能加载"
                return out
            raise exc

        await _wait_rendered(cdp, timeout)

        wanted = [s for s in (steps or []) if isinstance(s, dict)][: max(0, int(max_steps))]
        for step in wanted:
            reason = await _run_step(cdp, step)
            if reason:
                logger.debug(f"[browse] 动作失败: {reason}")
                break
            out["steps_done"] += 1
        if wanted:
            await _wait_rendered(cdp, min(10.0, timeout))

        title = str(await cdp.evaluate("document.title") or "").strip()
        final_url = str(await cdp.evaluate("location.href") or target)
        raw = await cdp.evaluate("document.body ? document.body.innerText : ''") or ""
        visible = normalize_text(str(raw))

        # 渲染后的 HTML 再交给 trafilatura 抽一遍正文：innerText 里混着导航、
        # 侧栏、"接下来播放"这类东西（B 站实测 1388 字里有三分之一是导航）。
        # 抽得出来就用干净的，抽不出来（列表页/论坛/SPA）才退回 innerText。
        text = visible
        source = "innerText"
        with contextlib.suppress(CdpError):
            html = await cdp.evaluate("document.documentElement.outerHTML") or ""
            if html:
                meta_title, clean = webtool.extract_content(str(html), final_url)
                clean = normalize_text(clean)
                if len(clean) >= _MIN_EXTRACT_CHARS:
                    text = clean
                    source = "extract"
                    if not title and meta_title:
                        title = meta_title

        out.update(
            {
                "url": final_url,
                "title": title[:120],
                "chars": len(text),
                "text": clip(text, max_chars),
                "source": source,
            }
        )

        # 验证页判定用**屏幕上真实显示的内容**（innerText），不用抽干净之后的正文：
        # 验证控件常常在导航/侧栏里，trafilatura 会把它丢掉
        reason = detect_challenge(title, final_url, visible)
        if reason:
            out["needs_human"] = True
            out["reason"] = reason
            out["screenshot"] = await _screenshot(cdp, shots, _host_of(final_url) or "page")
            logger.info(f"[browse] 需要人工处理（{reason}）: {final_url[:80]}")
            return out

        if not text:
            out["reason"] = "页面打开了但没有可读文字（可能是纯图片页，或者还在加载）"
            out["screenshot"] = await _screenshot(cdp, shots, host or "page")
            return out

        out["ok"] = True
        logger.info(f"[browse] 「{title[:30]}」→ {out['chars']} 字（{final_url[:60]}）")
        return out
    except CdpError as exc:
        out["error"] = str(exc)
        logger.warning(f"[browse] CDP 出错: {exc}（{target[:80]}）")
        return out
    except asyncio.TimeoutError:
        out["error"] = f"打开超时（{timeout:g} 秒）"
        return out
    except Exception as exc:  # noqa: BLE001 — 工具永远不能把对话带崩
        out["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[browse] 打开失败: {exc}（{target[:80]}）")
        return out
    finally:
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.call("Fetch.disable", timeout=3.0)
            await cdp.close()
        if target_id:
            await _close_target(base, target_id)


# ---------------------------------------------------------------- 给模型看的话


def format_report(res: dict, *, max_chars: int = 3000) -> str:
    """把 :func:`browse` 的结果拼成给模型看的中文文本。"""
    url = str(res.get("url") or "")
    title = str(res.get("title") or "").strip()
    error = str(res.get("error") or "").strip()
    reason = str(res.get("reason") or "").strip()
    shot = str(res.get("screenshot") or "").strip()

    if error:
        return (
            f"没能用浏览器打开这个网址：{error}。\n"
            "不要编造里面的内容。如果是页面本身的问题，可以告诉用户你打不开；"
            "如果是「连不上浏览器」，那是服务器上的浏览器服务没起来，跟用户说一声就行，别反复重试。"
        )

    if res.get("needs_human"):
        lines = [f"这一页需要人工处理：{reason}。", f"网址：{url}"]
        if title:
            lines.insert(0, f"标题：{title}")
        if shot:
            lines.append(f"截图：{shot}")
        lines.append(
            "**你自己过不去**（验证码 / 登录都要真人操作），所以别反复重试同一个地址。\n"
            "请让用户在服务器浏览器的远程画面里手动过一下验证（README「真实浏览器」节有 VNC 入口），"
            "过完让用户说一声，你再读一次这个网址——浏览器的登录态是留着的，不用从头再来。"
        )
        return "\n".join(lines)

    body = str(res.get("text") or "")
    chars = int(res.get("chars") or len(body))
    head = f"标题：{title or '（无标题）'}\n网址：{url}"
    if res.get("steps_done"):
        head += f"\n（先在页面上操作了 {res['steps_done']} 步，下面是操作后的内容）"
    if chars > len(body):
        head += f"\n（正文 {chars} 字，下面是前 {len(body)} 字）"
    body = clip(body, max_chars)
    if not res.get("ok"):
        return (
            f"{head}\n{reason or '没读到正文'}\n"
            "不要编造里面的内容；可以告诉用户这个页面没读到东西。"
        )
    return f"{head}\n正文：\n{body}"

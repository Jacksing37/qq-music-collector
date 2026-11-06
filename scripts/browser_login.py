"""人工接管登录/验证页：把服务器浏览器里的页面截出来给你看，你登录（扫码）后脚本自动等结果。

**不需要 SSH、不需要开放端口、不需要 VNC**——只用 `workbench download` 把 PNG 取下来：

1. 在服务器上跑：
       cd /opt/qq-music-collector
       ./.venv/bin/python scripts/browser_login.py https://www.zhihu.com/signin
   脚本打开那一页、截图，并打印一行可以直接复制的下载命令。
2. 在你自己电脑上跑它打印的那条：
       workbench download /tmp/qcmc-login/<时间>.png G:\\下载\\qr.png -i i-wz9gjz252sxd6n8uj8c1
3. 打开那张图 —— 知乎/微博/小红书/B站 的登录页都有**二维码**，用手机 App 扫一下即可。
   扫完服务器浏览器就登录了，脚本会在几秒内报「登录成功」。
4. 之后 `browse` 读那些站就不再是登录墙了（cookie 存在 profile 里，通常能留几周）。

验证页里如果是滑块/点选这类没有二维码的，用「你看着截图报坐标、脚本去点」：
       --click 850,420                 # 点一下那个坐标
       --drag 300,500,600,500          # 从 (300,500) 按住拖到 (600,500)，滑块用这个

安全：脚本只认 http/https，并且复用 `browser.py` 的逐请求 SSRF 闸（内网/环回/云元数据
一律拒）。它打开的 tab 在结束时关闭，登录态留在 profile 里。
"""

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import browser as b  # noqa: E402

#: 截图落在这里——/tmp 好下载，也不掺进 data/
SHOTS = Path("/tmp/qcmc-login")


def parse_point(text: str) -> tuple[float, float]:
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("坐标要写成 x,y，例如 850,420")
    return float(parts[0]), float(parts[1])


def parse_drag(text: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("拖动要写成 x1,y1,x2,y2")
    x1, y1, x2, y2 = (float(p) for p in parts)
    return x1, y1, x2, y2


async def click(cdp, x: float, y: float) -> None:
    for kind in ("mousePressed", "mouseReleased"):
        await cdp.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1,
             "buttons": 1 if kind == "mousePressed" else 0},
        )
        await asyncio.sleep(0.06)


async def drag(cdp, x1: float, y1: float, x2: float, y2: float, steps: int = 24) -> None:
    """按住拖动。滑块类验证吃的是「连续移动 + 时间间隔」，所以分步发而不是一步到位。"""
    await cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x1, "y": y1, "button": "left", "clickCount": 1, "buttons": 1},
    )
    for i in range(1, steps + 1):
        await cdp.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x1 + (x2 - x1) * i / steps,
             "y": y1 + (y2 - y1) * i / steps, "button": "left", "buttons": 1},
        )
        await asyncio.sleep(0.02)
    await cdp.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x2, "y": y2, "button": "left",
         "clickCount": 1, "buttons": 0},
    )


def show(path: str, instance: str = "") -> None:
    """打印截图路径 + 一条可以直接复制的下载命令（在你自己的电脑上跑）。"""
    print(f"  截图：{path}")
    print("  取下来看（在你自己电脑上跑）：")
    suffix = f" -i {instance}" if instance else " -i <你的实例ID>"
    print(f"    workbench download {path} <你本机的保存路径>.png{suffix}")
    print()


async def main() -> int:
    ap = argparse.ArgumentParser(description="人工接管：截图 + 等待登录完成")
    ap.add_argument("url", help="登录页/验证页地址")
    ap.add_argument("--wait-seconds", type=int, default=300, help="最多等多少秒（默认 300）")
    ap.add_argument("--interval", type=float, default=3.0, help="每几秒看一次")
    ap.add_argument("--click", type=parse_point, action="append", default=[],
                    help="打开后先点这个坐标（可重复），格式 x,y")
    ap.add_argument("--drag", type=parse_drag, action="append", default=[],
                    help="打开后先拖一次（滑块用），格式 x1,y1,x2,y2")
    ap.add_argument("--cdp", default=b.CDP_DEFAULT)
    ap.add_argument("--instance-id", default="",
                    help="只用来拼打印里那条 workbench download 命令")
    opts = ap.parse_args()

    if not b._usable_url(opts.url):
        print("错误：需要一个完整的 http/https 网址")
        return 2
    ok, why = await b.health(opts.cdp)
    if not ok:
        print(f"错误：{why}")
        return 1
    print(f"浏览器：{why}")

    host = b._host_of(opts.url)
    if not await b.host_is_public(host):
        print("错误：拒绝访问内网 / 环回 / 云元数据之类的地址（SSRF 防护）")
        return 2

    SHOTS.mkdir(parents=True, exist_ok=True)
    guard = b._Guard([])
    cdp = None
    target_id = ""
    start_url = opts.url
    try:
        info = await b._open_target(opts.cdp, timeout=15.0)
        target_id = str(info.get("id") or "")
        ws_url = str(info.get("webSocketDebuggerUrl") or "")
        from websockets.asyncio.client import connect as ws_connect

        ws = await asyncio.wait_for(ws_connect(ws_url, max_size=None), timeout=20.0)
        cdp = b.Cdp(ws)

        async def on_event(msg):
            if b._is_challenge_event(msg):
                await guard.handle(cdp, msg)

        await cdp.start(on_event=on_event)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": b.VIEWPORT_WIDTH, "height": b.VIEWPORT_HEIGHT,
                        "deviceScaleFactor": 1, "mobile": False})
        await cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        await cdp.call("Page.navigate", {"url": opts.url}, timeout=30.0)
        await b._wait_rendered(cdp, 30.0)

        for point in opts.click:
            print(f"  点击 {point}")
            await click(cdp, *point)
            await asyncio.sleep(0.8)
        for box in opts.drag:
            print(f"  拖动 {box}")
            await drag(cdp, *box)
            await asyncio.sleep(1.5)

        shot = await b._screenshot(cdp, SHOTS, f"login-{host}")
        print("=" * 72)
        print("① 把这张图取下去，用手机扫码（或按下面的坐标告诉我要点哪里）")
        show(shot, opts.instance_id)

        print(f"② 等你操作……最多等 {opts.wait_seconds} 秒，每 {opts.interval:g} 秒看一次")
        deadline = time.monotonic() + max(10, opts.wait_seconds)
        last_url = start_url
        while time.monotonic() < deadline:
            await asyncio.sleep(max(1.0, opts.interval))
            url = (await b._try_eval(cdp, "location.href", timeout=8.0, default="",
                                     attempts=1)) or await b._history_url(cdp) or last_url
            state = await b._read_state(cdp, 10.0)
            title = state["title"].strip()
            need = b.detect_challenge(title, url, state["text"])
            if not need and url != start_url:
                print("=" * 72)
                print(f"③ 登录成功（页面已跳到 {url[:80]}）")
                shot = await b._screenshot(cdp, SHOTS, f"done-{host}")
                show(shot, opts.instance_id)
                print("   现在可以读这些站了。复验：")
                print("     ./.venv/bin/python tests/manual_browser.py")
                return 0
            if url != last_url:
                print(f"  … 页面变了：{url[:80]}（仍在等正文/验证消失）")
                last_url = url
            else:
                print(f"  … 还没好（{need[:40] or '页面没变'}）")
        print("=" * 72)
        print("③ 超时：还没登录成功。可能二维码过期了，重跑一次本脚本；")
        print("   或者加 --click x,y / --drag x1,y1,x2,y2 让我替你点/拖（坐标从截图上量）。")
        shot = await b._screenshot(cdp, SHOTS, f"timeout-{host}")
        show(shot, opts.instance_id)
        return 1
    except Exception as exc:  # noqa: BLE001 — 运维脚本，报错要看得懂
        print(f"出错：{type(exc).__name__}: {exc}")
        return 1
    finally:
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.call("Fetch.disable", timeout=3.0)
            await cdp.close()
        if target_id:
            await b._close_target(opts.cdp, target_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

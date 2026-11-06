"""真连服务器浏览器验收 `browse`：健康检查 → 代表性站点 → SSRF → 会话持久化 → 并发/ soak。

**不进回归**（需要服务器上 Chromium 服务在跑、真联网、跑一轮一两分钟）。
部署完、重启机器人后、或怀疑浏览器服务坏了时手动跑一次：

    python tests/manual_browser.py            # 标准一轮（约 60~90 秒）
    python tests/manual_browser.py --soak 6   # 再加 N 次连续调用看有没有内存/tab 泄漏
    python tests/manual_browser.py --quick    # 只跑健康检查 + 两个站

判定标准（脚本自己会打 PASS/FAIL）：
- 三个 systemd 服务在跑、CDP 通；
- JS 渲染页能读到正文（B 站视频页、微博）；
- 静态文章走 `trafilatura`（`source=extract`）而不是 `innerText`；
- 知乎这类登录墙/验证页判成 `needs_human` 而不是「打不开」；
- 内网 / 云元数据地址被拒（SSRF）；
- 上一次调用留下的 cookie 下一次调用还在（**人工过完验证「再读一次」就靠这个**）。
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

# 测试用独立数据目录：默认的 data/ 里是真配置，别让验收脚本碰它
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="qcmc-manual-"))

import httpx  # noqa: E402
import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import browser as b  # noqa: E402

CDP = "http://127.0.0.1:9222"

#: 代表性站点：(说明, URL, 期望)
SITES = [
    ("B 站视频页（httpx 抓是 412）", "https://www.bilibili.com/video/BV1GJ411x7h7", "text"),
    ("微博移动端（前端渲染）", "https://m.weibo.cn/", "text"),
    ("静态文章（应走 trafilatura）", "https://www.ruanyifeng.com/blog/2019/09/curl-reference.html",
     "extract"),
    ("知乎（未登录 → 登录墙）", "https://www.zhihu.com/question/19550225", "human"),
]

#: 必须被拒的地址（SSRF）
BLOCKED = [
    ("云元数据", "http://169.254.169.254/latest/meta-data/"),
    ("宝塔面板", "http://127.0.0.1:8765/"),
    ("CDP 自己", "http://127.0.0.1:9222/json/version"),
]

fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + ("" if cond else f"   ← {extra}"))
    if not cond:
        fails.append(label)


def brief(res: dict) -> str:
    if res.get("needs_human"):
        return f"needs_human（{res['reason'][:40]}）"
    if res.get("error"):
        return f"error（{res['error'][:50]}）"
    return f"{res['chars']} 字 / source={res.get('source')} / {res['url'][:50]}"


async def page_count() -> int:
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(f"{CDP}/json/list")
            return len([t for t in resp.json() if t.get("type") == "page"])
    except Exception:  # noqa: BLE001 — 验收脚本，量不到就是 0
        return 0


def service_mem_mb() -> float:
    """qqmusic-browser 的 cgroup 实际占用（MB）。取不到返回 -1。"""
    try:
        out = subprocess.run(
            ["systemctl", "show", "qqmusic-browser", "-p", "MemoryCurrent", "--value"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return int(out) / 1024 / 1024
    except Exception:  # noqa: BLE001
        return -1.0


def service_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for unit in ("qqmusic-xvfb", "qqmusic-browser", "qqmusic-vnc", "qq-music-collector"):
        try:
            out = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            out = "unknown"
        states[unit] = out
    return states


async def main() -> int:
    args = argparse.ArgumentParser(description="服务器浏览器 browse 验收")
    args.add_argument("--soak", type=int, default=0, help="额外连续调用多少次")
    args.add_argument("--quick", action="store_true", help="只跑健康检查 + 两个站")
    opts = args.parse_args()

    print("=" * 72)
    print("① 服务与健康检查")
    for unit, state in service_states().items():
        check(f"{unit} 是 active", state == "active", state)
    ok, why = await b.health(CDP)
    check(f"CDP 可达（{why}）", ok, why)
    if not ok:
        print("\n浏览器服务没起来，先看 README「真实浏览器 · 故障排查」。")
        return 1
    print(f"  page 数 {await page_count()}，cgroup {service_mem_mb():.0f} MB / 限额 800 MB")

    print("=" * 72)
    print("② 代表性站点")
    sites = SITES[:2] if opts.quick else SITES
    for label, url, want in sites:
        res = await b.browse(url, timeout=45.0, max_chars=2000)
        if want == "human":
            check(f"{label} → 判成需要人工", res["needs_human"], brief(res))
            if res["needs_human"]:
                print(f"        截图: {res['screenshot'] or '（没截到）'}")
        elif want == "extract":
            check(f"{label} → 抽到正文", res["ok"] and res["chars"] > 200, brief(res))
            if res["ok"]:
                check("        source=extract（trafilatura 生效）",
                      res.get("source") == "extract", str(res.get("source")))
        else:
            check(f"{label} → 读到正文", res["ok"] and res["chars"] > 100, brief(res))
        print(f"        {brief(res)}")

    if opts.quick:
        return report()

    print("=" * 72)
    print("③ SSRF：这些地址必须被拒")
    for label, url in BLOCKED:
        res = await b.browse(url, timeout=20.0)
        check(f"{label} 被拒", bool(res["error"]) and not res["ok"], brief(res))

    print("=" * 72)
    print("④ 会话持久化（人工过完验证「再读一次」的前提）")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            reachable = (await client.get("https://httpbin.org/status/200")).status_code == 200
    except Exception:  # noqa: BLE001
        reachable = False
    if reachable:
        await b.browse("https://httpbin.org/cookies/set?qcmc_probe=hello", timeout=35.0)
        res = await b.browse("https://httpbin.org/cookies", timeout=35.0)
        check("第二次调用（新 tab）看得到第一次设的 cookie",
              "qcmc_probe" in res["text"] and "hello" in res["text"],
              " ".join(res["text"].split())[:120])
    else:
        print("  httpbin 不可达 → 跳过（这条只是便利验证，不影响功能）")

    print("=" * 72)
    print(f"⑤ 并发 3 个不同站")
    urls = [url for _, url, want in SITES if want == "text"]
    results = await asyncio.gather(*[b.browse(u, timeout=45.0, max_chars=1200) for u in urls])
    for u, res in zip(urls, results):
        check(f"并发成功 {u[:45]}", res["ok"] and res["chars"] > 100, brief(res))

    if opts.soak > 0:
        print("=" * 72)
        print(f"⑥ 连续调用 {opts.soak} 次（看稳定性与泄漏）")
        ok_count = 0
        for i in range(opts.soak):
            res = await b.browse(urls[i % len(urls)], timeout=45.0, max_chars=1200)
            ok_count += 1 if res["ok"] else 0
            print(f"  #{i + 1} {brief(res)}")
        check(f"{opts.soak} 次全部成功", ok_count == opts.soak, f"{ok_count}/{opts.soak}")

    print("=" * 72)
    print("⑦ 收尾审计")
    await asyncio.sleep(2)
    n = await page_count()
    mb = service_mem_mb()
    check(f"没有 tab 泄漏（剩 {n} 个）", n <= 2, str(n))
    check(f"内存在限额内（{mb:.0f} MB）", 0 < mb < 800, f"{mb}MB")
    return report()


def report() -> int:
    print("=" * 72)
    if fails:
        print(f"失败 {len(fails)} 项：")
        for item in fails:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

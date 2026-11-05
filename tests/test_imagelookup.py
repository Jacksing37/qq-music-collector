"""``lookup_anime_image``（以图搜番）：解析、阈值、缓存、失败路径、开关接线。

**不联网**：图片下载（``_download``）和 trace.moe 请求（``_post_image``）全部打桩，
样本照真实响应手写（2026-11-05 那次真机查询的返回）。

运行: MUSIC_DATA_DIR=$(mktemp -d) python tests/test_imagelookup.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import imagelookup as il  # noqa: E402
from music_collector import tools as tl  # noqa: E402
from music_collector.config import LLMConfig, MemoryConfig  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


#: 最小可用 PNG 头（只需要过 ``_download`` 与 mime 判断）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

#: 照真实 trace.moe 响应裁的样本：第一条是剧场版（``episode`` 为 true）
#: 、第二条是 TV 某集、第三条相似度太低（应被阈值丢掉）
SAMPLE = {
    "frameCount": 15223,
    "error": "",
    "quota": 92,
    "quotaUsed": 8,
    "result": [
        {
            "anilist": {
                "id": 173174,
                "title": {
                    "native": "BanG Dream! It's MyGO!!!!! 春の陽だまり、迷い猫",
                    "romaji": "BanG Dream! It's MyGO!!!!!: Haru no Hidamari, Mayoi Neko",
                },
                "format": "MOVIE",
                "startDate": {"year": 2024},
            },
            "filename": "[Shiniori-Raws] Bang Dream! It's MyGo!!!!! The Movie - Part 01 (BD 1280x720 x265)",
            "episode": True,
            "from": 2389.0,
            "to": 2398.0,
            "similarity": 0.938,
            "image": "https://api.trace.moe/image/aaa",
        },
        {
            "anilist": {
                "id": 1914,
                "title": {"native": "彩雲国物語 第2シリーズ", "romaji": "Saiunkoku Monogatari 2nd Season"},
                "format": "TV",
                "startDate": {"year": 2007},
            },
            "filename": "[TXXZ][The_Story_of_Saiunkoku][22].mp4",
            "episode": 22,
            "from": 422.0,
            "to": 426.0,
            "similarity": 0.598,
            "image": "https://api.trace.moe/image/bbb",
        },
        {
            "anilist": {"id": 1, "title": {"native": "噪声作品"}, "format": "TV"},
            "filename": "noise.mp4",
            "episode": 5,
            "from": 60.0,
            "to": 61.0,
            "similarity": 0.42,
            "image": "https://api.trace.moe/image/ccc",
        },
    ],
}

IMG = "https://multimedia.nt.qq.com.cn/download?fileid=abc"


class _Resp:
    """假的 httpx.Response：只要 ``status_code`` 和 ``json()``。"""

    def __init__(self, status: int = 200, payload: object = None) -> None:
        self.status_code = status
        self._payload = payload if payload is not None else SAMPLE

    def json(self) -> object:
        return self._payload


class _Stub:
    """把 ``_download`` / ``_post_image`` 换成可控的桩，并记下调用次数。"""

    def __init__(self, *, data: bytes = PNG, resp=None, download_error=None,
                 post_error=None):
        self.downloads = 0
        self.posts = 0
        self._data = data
        self._resp = resp
        self._download_error = download_error
        self._post_error = post_error

    def install(self) -> "_Stub":
        async def fake_download(url, timeout):
            self.downloads += 1
            if self._download_error is not None:
                raise self._download_error
            return self._data

        async def fake_post(data, timeout):
            self.posts += 1
            if self._post_error is not None:
                raise self._post_error
            return self._resp if self._resp is not None else _Resp()

        self._old = (il._download, il._post_image)  # type: ignore[attr-defined]
        il._download = fake_download  # type: ignore[assignment]
        il._post_image = fake_post  # type: ignore[assignment]
        return self

    def restore(self) -> None:
        il._download, il._post_image = self._old  # type: ignore[assignment]


def test_params() -> None:
    section("1. 地址校验")
    r = asyncio.run(il.search(""))
    check("空地址 → 报错", "不是 http" in r["error"], r["error"])
    r = asyncio.run(il.search("file:///etc/passwd"))
    check("非 http(s) → 报错", "不是 http" in r["error"], r["error"])


def test_parse() -> None:
    section("2. 解析 trace.moe 响应")
    il.reset()
    stub = _Stub().install()
    try:
        found = asyncio.run(il.search(IMG, min_similarity=0.5))
    finally:
        stub.restore()

    check("阈值 0.5 → 保留 2 条（0.42 的噪声被丢）", len(found["results"]) == 2,
          str([r["similarity"] for r in found["results"]]))
    best = found["best"] or {}
    check("首条按相似度排序", best.get("similarity") == 0.938, str(best.get("similarity")))
    check("作品名带原名与罗马字", "春の陽だまり" in best.get("title", "")
          and "Haru no Hidamari" in best.get("title", ""), best.get("title", ""))
    check("剧场版 episode=true → 「剧场版」（不是「第 True 话」）",
          best.get("episode") == "剧场版", str(best.get("episode")))
    check("时间点转成 hh:mm:ss", best.get("at") == "00:39:49", str(best.get("at")))
    check("TV 那一集是「第 22 话」", found["results"][1]["episode"] == "第 22 话",
          str(found["results"][1]["episode"]))
    check("额度与已用一起带回来",
          found["quota"] == 92 and found["used"] == 8, f"{found['quota']}/{found['used']}")
    check("分档：0.94 → 基本就是这部", il.tier_of(0.94) == "基本就是这部")
    check("分档：0.85 → 很可能是", il.tier_of(0.85) == "很可能是")
    check("分档：0.64 → 只能当线索", il.tier_of(0.64) == "只能当线索")


def test_threshold() -> None:
    section("3. 阈值：剔噪声，但**别把正确答案一起砍掉**")
    # 线上真实情形：截图被裁过 + 带字幕，trace.moe 只给 0.638（关键帧是 0.99）
    low = {
        "quota": 92, "quotaUsed": 8,
        "result": [
            {"anilist": {"id": 173174,
                         "title": {"native": "BanG Dream! It's MyGO!!!!! 春の陽だまり、迷い猫"},
                         "format": "MOVIE", "startDate": {"year": 2024}},
             "filename": "[Shiniori-Raws] ...", "episode": 1,
             "from": 2389.0, "to": 2398.0, "similarity": 0.638, "image": ""},
            {"anilist": {"id": 1914, "title": {"native": "彩雲国物語"}},
             "filename": "[TXXZ]...", "episode": 22,
             "from": 422.0, "to": 426.0, "similarity": 0.42, "image": ""},
        ],
    }
    il.reset()
    stub = _Stub(resp=_Resp(payload=low)).install()
    try:
        found = asyncio.run(il.search(IMG, min_similarity=0.7))
        text = il.format_result(found)
    finally:
        stub.restore()
    check("全都低于阈值 → 仍保留最高那条（上线前就是这么丢的正确答案）",
          len(found["results"]) == 1, str(found["results"]))
    check("保留的正是最高分那条", found["best"]["similarity"] == 0.638,
          str(found["best"]["similarity"]))
    check("文案写明只能当线索", "只能当线索" in text, text[:220])
    check("文案解释低分原因（裁切/字幕）", "裁过" in text or "字幕" in text, text[:500])

    # trace.moe 一条都没给：才是真的「认不出来」
    il.reset()
    stub = _Stub(resp=_Resp(payload={"quota": 90, "quotaUsed": 10, "result": []})).install()
    try:
        found = asyncio.run(il.search(IMG))
        text = il.format_result(found)
    finally:
        stub.restore()
    check("trace.moe 没有结果 → 0 条", found["results"] == [], str(found["results"]))
    check("这时才说「认不出来」", "没有匹配" in text and "认不出来" in text, text)


def test_failures() -> None:
    section("4. 失败路径（一律不抛异常）")
    il.reset()
    stub = _Stub(download_error=ValueError("不是图片（text/html）")).install()
    try:
        r = asyncio.run(il.search(IMG))
    finally:
        stub.restore()
    check("下载失败 → 翻成中文", "下载图片失败" in r["error"], r["error"])

    il.reset()
    stub = _Stub(post_error=RuntimeError("dns 挂了")).install()
    try:
        r = asyncio.run(il.search(IMG))
    finally:
        stub.restore()
    check("查询请求炸了 → 翻成中文", "以图搜番请求失败" in r["error"], r["error"])

    il.reset()
    stub = _Stub(resp=_Resp(status=429)).install()
    try:
        r = asyncio.run(il.search(IMG))
    finally:
        stub.restore()
    check("429 → 明说额度用完", "额度" in r["error"], r["error"])

    il.reset()
    stub = _Stub(resp=_Resp(status=502)).install()
    try:
        r = asyncio.run(il.search(IMG))
    finally:
        stub.restore()
    check("5xx → 带状态码", "HTTP 502" in r["error"], r["error"])

    il.reset()
    stub = _Stub(resp=_Resp(payload={"error": "Invalid image", "result": []})).install()
    try:
        r = asyncio.run(il.search(IMG))
    finally:
        stub.restore()
    check("trace.moe 自带 error → 原样转达", "Invalid image" in r["error"], r["error"])


def test_cache() -> None:
    section("5. 缓存（同一张图不重复烧额度）")
    il.reset()
    stub = _Stub().install()
    try:
        first = asyncio.run(il.search(IMG))
        second = asyncio.run(il.search(IMG))
        posts_after_two = stub.posts
        asyncio.run(il.search(IMG + "&other=1"))
        posts_after_third = stub.posts
    finally:
        stub.restore()
    check("同一 URL 第二次命中缓存（只 POST 一次）", posts_after_two == 1, str(posts_after_two))
    check("换 URL 会重新查", posts_after_third == 2, str(posts_after_third))
    check("缓存返回的是同一份结果", first["best"]["title"] == second["best"]["title"])


def test_format() -> None:
    section("6. 给模型的文案（要带判据和下一步）")
    il.reset()
    stub = _Stub().install()
    try:
        text = il.format_result(asyncio.run(il.search(IMG, min_similarity=0.5)))
    finally:
        stub.restore()
    check("有作品名", "春の陽だまり" in text, text[:120])
    check("有集数与时间点", "剧场版" in text and "00:39:49" in text, text[:160])
    check("有相似度分档", "基本就是这部" in text, text[:160])
    check("有判据说明", "≥0.90" in text, text[:200])
    check("提醒用作品名去搜角色", "登场角色" in text or "作品名" in text, text[-200:])
    check("明确禁止形容词堆叠", "形容词" in text, text[-260:])
    check("报额度（quota 是总量，要减去已用）", "还剩 84 次" in text, text[-120:])


def test_build() -> None:
    section("7. 开关接线（有图才注册）")
    mem = MemoryConfig(enabled=False)
    cfg = LLMConfig(tools=True, web_tool=True, image_lookup=True, skills=False)

    tools, ctx = tl.build(cfg, mem=mem, images=[IMG])
    names = [t.name for t in tools]
    check("有图 + 开关开 → 注册 lookup_anime_image",
          "lookup_anime_image" in names, str(names))
    check("图片地址进了 ToolContext", ctx.images == [IMG], str(ctx.images))

    tools, ctx = tl.build(cfg, mem=mem)
    names = [t.name for t in tools]
    check("没图 → 不注册（省 schema 额度）", "lookup_anime_image" not in names, str(names))

    tools, _ = tl.build(
        LLMConfig(tools=True, web_tool=True, image_lookup=False, skills=False),
        mem=mem, images=[IMG],
    )
    check("开关关掉 → 不注册",
          "lookup_anime_image" not in [t.name for t in tools], str([t.name for t in tools]))

    tools, _ = tl.build(
        LLMConfig(tools=False, image_lookup=True, skills=False), mem=mem, images=[IMG]
    )
    check("工具总闸关掉 → 一个都没有", tools == [], str([t.name for t in tools]))

    check("在白名单里", "lookup_anime_image" in tl.ALLOWED_TOOL_NAMES,
          str(sorted(tl.ALLOWED_TOOL_NAMES)))


def test_handler() -> None:
    section("8. 工具 handler")
    mem = MemoryConfig(enabled=False)
    cfg = LLMConfig(tools=True, image_lookup=True, image_lookup_interval=0, skills=False)

    async def case() -> None:
        il.reset()
        stub = _Stub().install()
        try:
            tools, ctx = tl.build(cfg, mem=mem, images=[IMG])
            out = await tl.dispatch(tools, "lookup_anime_image", {}, ctx)
            check("拿到结果 → 拼成给模型的文本", "春の陽だまり" in out and "相似度" in out,
                  out[:120])

            tools, ctx = tl.build(cfg, mem=mem, images=[IMG])
            out = await tl.dispatch(tools, "lookup_anime_image", {"index": 3}, ctx)
            check("index 越界 → 明说只有几张", "只有 1 张图" in out, out)

            # 这条走 handler 本身：工具在「没图」时压根不会注册，dispatch 会回「没有这个工具」
            _, ctx = tl.build(cfg, mem=mem)
            out = await tl._handle_image_lookup({}, ctx)
            check("没图时调用 → 提示发图再来", "没有图片" in out, out)

            # 限流拒绝：把下次可用时间推到很远
            il._next_at = asyncio.get_running_loop().time() + 100
            tools, ctx = tl.build(
                LLMConfig(tools=True, image_lookup=True, image_lookup_interval=3.0,
                          skills=False),
                mem=mem, images=[IMG],
            )
            out = await tl.dispatch(tools, "lookup_anime_image", {}, ctx)
            check("限流 → 让模型等几秒（不硬等）", "等几秒" in out, out)
        finally:
            stub.restore()
            il.reset()

    asyncio.run(case())


def test_defaults() -> None:
    section("9. 配置默认值（改默认值时要同步改这里）")
    cfg = LLMConfig()
    check("以图搜番默认开（认图靠它）", cfg.image_lookup is True)
    check("默认超时 30 秒", cfg.image_lookup_timeout == 30.0, str(cfg.image_lookup_timeout))
    check("默认间隔 3 秒（护额度）", cfg.image_lookup_interval == 3.0,
          str(cfg.image_lookup_interval))
    check("默认最低相似度 0.45（别设太高，会把裁切过的截图砍掉）",
          cfg.image_lookup_min_similarity == 0.45, str(cfg.image_lookup_min_similarity))

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("image_lookup", "image_lookup_timeout", "image_lookup_interval",
                "image_lookup_min_similarity"):
        check(f"示例配置里有 {key}", key in example)


def main() -> None:
    test_params()
    test_parse()
    test_threshold()
    test_failures()
    test_cache()
    test_format()
    test_build()
    test_handler()
    test_defaults()
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

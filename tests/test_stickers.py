"""表情包库 / 图片搜索 / 发图工具的离线测试：不联网、不发真消息。

覆盖四层：
1. :mod:`stickers` —— 标签规整、格式嗅探、去重入库、按情绪找；
2. :mod:`imagesearch` —— 两个引擎的结果解析（用真实返回结构的样本）；
3. 工具层的注册契约与行为（``sticker_add`` / ``sticker_find`` / ``sticker_send`` /
   ``send_image`` / ``search_images``），走 ``dispatch`` 这条线上同一个入口；
4. 消息侧的两个小件：``bot_utils.collect_stickers``（哪些图能收）和
   ``browser`` 的图片直链提取（``browse`` 顺带带回页面图片）。
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["MUSIC_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="qcmc-sticker-")))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from music_collector import browser as br  # noqa: E402
from music_collector import imagesearch as isr  # noqa: E402
from music_collector import stickers as st  # noqa: E402
from music_collector import tools as tl  # noqa: E402
from music_collector.bot_utils import collect_stickers  # noqa: E402
from music_collector.config import LLMConfig  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    checks.append((label, bool(cond), extra))
    print(f"  {'[PASS]' if cond else '[FAIL]'} {label}" + ("" if cond else f"   ← {extra}"))


#: 1×1 透明 PNG（够让 ``sniff`` 认出格式）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+3Q1WAAAAAElFTkSuQmCC"
)
GIF = b"GIF89a" + b"\x00" * 32


def names(cfg, **kwargs) -> list[str]:
    return [t.name for t in tl.build(cfg, **kwargs)[0]]


async def main() -> int:
    print("== 标签规整 ==")
    check("空格/逗号/顿号都能分开", st.parse_tags("无语, 猫　笑") == ["无语", "猫", "笑"],
          str(st.parse_tags("无语, 猫　笑")))
    check("数组形式也认", st.parse_tags(["无语", "猫"]) == ["无语", "猫"], "数组")
    check("去重（不区分大小写）", st.parse_tags("Cat cat CAT") == ["Cat"], str(st.parse_tags("Cat cat CAT")))
    check("最多 8 个", len(st.parse_tags("一 二 三 四 五 六 七 八 九 十")) == 8,
          str(st.parse_tags("一 二 三 四 五 六 七 八 九 十")))
    check("空标签 → 空列表", st.parse_tags("   ,/ ") == [], str(st.parse_tags("   ,/ ")))

    print("== 格式嗅探 ==")
    check("PNG 认得出", st.sniff(PNG) == ("png", "image/png"), str(st.sniff(PNG)))
    check("GIF 认得出", st.sniff(GIF) == ("gif", "image/gif"), str(st.sniff(GIF)))
    check("WEBP 认得出", st.sniff(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8) == ("webp", "image/webp"),
          "webp")
    check("HTML 错误页认不出（不会存成 .png）", st.sniff(b"<html>404</html>") == ("", ""), "html")

    print("== 入库 / 去重 / 查找 ==")
    entry, created = await st.add(PNG, tags="无语 猫", note="歪头", source="群543486099", group=543486099)
    check("第一次是新建", created is True and entry is not None, str(entry))
    check("文件真落盘了", st.path_of(entry) is not None, str(st.path_of(entry)))
    check("id 是 sha1 前 8 位", entry["id"] == __import__("hashlib").sha1(PNG).hexdigest()[:8], entry["id"])

    again, created2 = await st.add(PNG, tags="猫 更好笑")
    check("同一张图不会存两份", created2 is False and st.count() == 1, str(st.count()))
    check("新标签并进老条目", "更好笑" in again["tags"] and "无语" in again["tags"], str(again["tags"]))

    await st.add(GIF, tags="笑 打滚", note="滚来滚去")
    check("库里现在 2 条", st.count() == 2, str(st.count()))

    hits = st.find("无语")
    check("按情绪找得到", bool(hits) and hits[0]["id"] == entry["id"], str([h["id"] for h in hits]))
    check("按内容找得到", bool(st.find("打滚")), "打滚")
    check("找不到时返回空", st.find("量子力学") == [], str(st.find("量子力学")))
    check("空查询按用过次数排（最常用在前）",
          len(st.find("")) == 2 and st.find("")[0]["used"] >= st.find("")[1]["used"], "空查询")
    check("标签概览里有标签", "无语" in st.tag_summary(), st.tag_summary())

    await st.bump_use(entry["id"])
    check("使用次数累加", st.get(entry["id"])["used"] == 1, str(st.get(entry["id"])["used"]))
    check("format_entries 带 id", f"id={entry['id']}" in st.format_entries(hits), st.format_entries(hits)[:60])

    print("== 下载护栏（不联网：全是校验失败路径）==")
    for label, url, want in [
        ("非 http 地址", "file:///etc/passwd", "http"),
        ("内网地址被 SSRF 闸拦下", "http://127.0.0.1:9222/json", "内网"),
        ("云元数据被拦下", "http://169.254.169.254/latest/meta-data/", "内网"),
    ]:
        try:
            await st.download(url, timeout=1.0)
            check(label, False, "居然没抛")
        except ValueError as exc:
            check(label, want in str(exc), str(exc)[:60])

    print("== 图片搜索结果解析 ==")
    import html as html_mod

    # Bing 的真实结构：m 属性里是 HTML 转义过的 JSON
    bing_meta = {
        "murl": "https://img.example.com/cat.jpg",
        "turl": "https://tse1.mm.bing.net/th?id=OIP.abc",
        "purl": "https://example.com/page",
        "t": "猫猫 无语",
    }
    bing_html = (
        '<a class="iusc" href="#" m="'
        + html_mod.escape(json.dumps(bing_meta), quote=True)
        + '"></a>'
    )
    rows = isr.parse_bing(bing_html)
    check("Bing：解析出 1 条", len(rows) == 1, str(rows))
    check("Bing：拿到原图直链", rows and rows[0]["url"] == "https://img.example.com/cat.jpg", str(rows[:1]))
    check("Bing：来源页也带上了", rows and rows[0]["page"] == "https://example.com/page", str(rows[:1]))

    baidu_payload = json.dumps(
        {
            "data": [
                {
                    "middleURL": "https://img1.baidu.com/it/u=1.jpg",
                    "thumbURL": "https://img1.baidu.com/it/u=1&s=thumb",
                    "fromPageTitleEnc": "<strong>柴犬</strong> 表情包",
                    "fromURL": "https://example.com/doge",
                },
                {"middleURL": "data:image/gif;base64,xx"},  # 该丢
            ]
        },
        ensure_ascii=False,
    )
    rows = isr.parse_baidu(baidu_payload)
    check("百度：只留能发的直链", len(rows) == 1, str(rows))
    check("百度：标题去掉了 HTML 标签", rows and "<" not in rows[0]["title"], str(rows[:1]))
    check("format_results 写明怎么用", "send_image" in isr.format_results("测试", rows), "格式化")

    print("== 消息侧：哪些图能收 ==")
    msg = Message(
        [
            MessageSegment("mface", {"url": "https://gchat.qpic.cn/a.gif", "summary": "偷笑", "key": "k1"}),
            MessageSegment("image", {"url": "https://gchat.qpic.cn/b.png", "sub_type": "1"}),
            MessageSegment("image", {"url": "https://gchat.qpic.cn/c.jpg", "sub_type": "0"}),
            MessageSegment.text("哈哈哈"),
        ]
    )
    picked = collect_stickers(msg)
    urls = [row["url"] for row in picked]
    check("mface 收进来了", "https://gchat.qpic.cn/a.gif" in urls, str(urls))
    check("表情图（sub_type=1）收进来了", "https://gchat.qpic.cn/b.png" in urls, str(urls))
    check("普通照片不算表情包", "https://gchat.qpic.cn/c.jpg" not in urls, str(urls))
    check("summary 带出来了", picked and picked[0]["summary"] == "偷笑", str(picked[:1]))

    print("== browser：页面图片直链 ==")
    normalized = br._normalize_images(
        [
            {"url": "https://a.example.com/1.jpg", "w": 640, "h": 480, "alt": "图1"},
            {"url": "https://a.example.com/1.jpg", "w": 640, "h": 480},  # 重复
            {"url": "data:image/gif;base64,xx"},                        # 非 http
            {"url": "https://a.example.com/2.png"},
            "垃圾数据",
        ]
    )
    check("去重 + 只留 http", [row["url"] for row in normalized] == [
        "https://a.example.com/1.jpg", "https://a.example.com/2.png"
    ], str([row["url"] for row in normalized]))
    report = br.format_report(
        {"ok": True, "url": "https://x", "title": "图集", "text": "正文", "chars": 2,
         "images": normalized},
        max_chars=100,
    )
    check("报告里带上图片直链", "https://a.example.com/1.jpg" in report, report[:120])
    check("并提示别一次发好几张", "别一次发好几张" in report, report[-60:])

    print("== 工具注册契约 ==")
    cfg = LLMConfig()
    check("默认给 sticker_add", "sticker_add" in names(cfg, group_id=1))
    check("库里有货才给 sticker_find", "sticker_find" in names(cfg, group_id=1))
    check("没有发图回调 → 没有 send_image", "send_image" not in names(cfg, group_id=1))
    check("没有发图回调 → 没有 sticker_send", "sticker_send" not in names(cfg, group_id=1))

    async def image_out(data: bytes) -> bool:
        return True

    check("有回调就有 send_image", "send_image" in names(cfg, group_id=1, image_out=image_out, image_max=2))
    check("有回调就有 sticker_send",
          "sticker_send" in names(cfg, group_id=1, image_out=image_out, image_max=2))
    check("image_send_max=0 → 只给收、不给发",
          "send_image" not in names(cfg, group_id=1, image_out=image_out, image_max=0)
          and "sticker_send" not in names(cfg, group_id=1, image_out=image_out, image_max=0))
    check("stickers=False → 一个表情包工具都没有",
          not any(n.startswith("sticker_") for n in names(LLMConfig(stickers=False), group_id=1,
                                                          image_out=image_out, image_max=2)))
    check("image_search=False → 没有 search_images",
          "search_images" not in names(LLMConfig(image_search=False), group_id=1))
    check("总闸 tools=False → 什么都没有", names(LLMConfig(tools=False), group_id=1) == [])
    check("五个新工具都在白名单里",
          {"sticker_add", "sticker_find", "sticker_send", "send_image", "search_images"}
          <= tl.ALLOWED_TOOL_NAMES, "白名单")

    print("== 工具行为（走 dispatch）==")
    sent_images: list[bytes] = []

    async def fake_image_out(data: bytes) -> bool:
        sent_images.append(data)
        return True

    from music_collector import stickers as st_mod

    async def fake_download(url: str, timeout: float = 20.0, *, max_bytes: int = st_mod.MAX_BYTES):
        if "broken" in url:
            raise ValueError("下载图片失败：HTTP 403")
        # 同一个地址 → 同一份字节（去重才有意义）；不同地址 → 不同图
        return (PNG if url.endswith(".png") else GIF) + url.encode("utf-8")

    real_download = st_mod.download
    st_mod.download = fake_download

    async def fake_search(query: str, count: int = 6, timeout: float = 20.0, **kwargs):
        if "无结果" in query:
            return [], "bing 没解析出任何图片"
        return [{"url": "https://img.example.com/x.jpg", "thumb": "", "page": "", "title": "示例"}], ""

    real_search = isr.search
    isr.search = fake_search
    isr.reset()

    stickers_cfg = LLMConfig(tools=True, web_tool=False, say_tool=False, image_send_max=2)
    tools, ctx = tl.build(
        stickers_cfg,
        group_id=543486099,
        image_out=fake_image_out,
        image_max=2,
        stickers=[{"kind": "mface", "url": "https://gchat.qpic.cn/s.png", "summary": "偷笑"}],
    )

    out = await tl.dispatch(tools, "sticker_add", {"tags": "  "}, ctx)
    check("空标签被拒，并告诉模型该怎么写", "不能为空" in out and "标签" in out, out[:80])

    out = await tl.dispatch(tools, "sticker_add", {"tags": "偷笑 沙雕"}, ctx)
    check("收下这次消息里的表情包", "已收进表情包库" in out, out[:80])
    match = __import__("re").search(r"id=([0-9a-f]+)", out)
    saved_id = match.group(1) if match else ""
    check("库里能按新标签找到它", bool(saved_id) and st.get(saved_id) is not None, str(saved_id))

    out = await tl.dispatch(tools, "sticker_find", {"query": "偷笑"}, ctx)
    check("find 给出 id", f"id={saved_id}" in out, out[:80])

    out = await tl.dispatch(tools, "sticker_find", {"query": "量子力学"}, ctx)
    check("找不到时明确说别硬发", "别硬发" in out, out[:80])

    out = await tl.dispatch(tools, "sticker_send", {"id": saved_id}, ctx)
    check("发出去了（走 image_out）", len(sent_images) == 1, str(len(sent_images)))
    check("回填里提醒别重复发", "别再重复发" in out, out[:80])
    check("额度用掉 1 张", ctx.image_used == 1, str(ctx.image_used))
    check("用过次数 +1", st.get(saved_id)["used"] >= 1, str(st.get(saved_id)["used"]))

    out = await tl.dispatch(tools, "sticker_send", {"id": saved_id}, ctx)
    check("第二张还能发（上限 2）", len(sent_images) == 2, str(len(sent_images)))
    out = await tl.dispatch(tools, "sticker_send", {"id": saved_id}, ctx)
    check("到上限就不发了", len(sent_images) == 2 and "上限" in out, out[:60])

    # id 不存在要在「额度还没用完」的上下文里测，否则先被额度挡掉
    tools_fresh, ctx_fresh = tl.build(
        stickers_cfg, group_id=1, image_out=fake_image_out, image_max=2
    )
    out = await tl.dispatch(tools_fresh, "sticker_send", {"id": "deadbeef"}, ctx_fresh)
    check("id 不存在时提示先 find", "sticker_find" in out or "没有 id" in out, out[:60])

    out = await tl.dispatch(tools, "sticker_add", {"tags": "坏图", "url": "https://x.example.com/broken.jpg"}, ctx)
    check("下载失败时说清原因", "没能收下" in out and "403" in out, out[:80])

    out = await tl.dispatch(tools, "send_image", {"url": "https://img.example.com/ok.png"}, ctx)
    check("send_image 也受同一个额度管（已满）", "上限" in out and len(sent_images) == 2, out[:60])

    print("== send_image（额度重置后）==")
    tools2, ctx2 = tl.build(stickers_cfg, group_id=1, image_out=fake_image_out, image_max=2)
    out = await tl.dispatch(tools2, "send_image", {"url": "https://img.example.com/ok.png"}, ctx2)
    check("发出去了", len(sent_images) == 3 and "已经发出去了" in out, out[:60])
    check("提示可以顺手收进库", "sticker_add" in out, out[:80])
    out = await tl.dispatch(tools2, "send_image", {"url": "https://img.example.com/broken.png"}, ctx2)
    check("下不下来时让它换一张", "换一条直链" in out, out[:80])
    out = await tl.dispatch(tools2, "send_image", {"url": ""}, ctx2)
    check("空 url 被拒", "不能为空" in out, out[:60])

    print("== search_images ==")
    out = await tl.dispatch(tools2, "search_images", {"query": "柴犬 表情包"}, ctx2)
    check("搜到图并给出直链", "https://img.example.com/x.jpg" in out, out[:120])
    check("提醒用 send_image 发", "send_image" in out, out[-80:])
    isr.reset()
    out = await tl.dispatch(tools2, "search_images", {"query": "无结果 的东西"}, ctx2)
    check("搜不到时给替代路径", "browse" in out and "web_search" in out, out[:120])

    print("== chat 层：工具真的接到模型手上了 ==")
    from music_collector.llm import LLMBot

    chat_cfg = LLMConfig(
        tools=True, tool_rounds=2, say_tool=False, web_tool=False, web_search=False,
        image_send_max=2, cooldown=0, history_rounds=0, api_key="sk-test",
        base_url="https://api.example.com/v1", model="test-model",
    )
    seen_prompts: list[str] = []
    sent_images.clear()

    def _tool_call(name: str, args: dict, call_id: str) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ],
        }

    async def run_turn(calls_in_a_row: list[tuple[str, dict]], final: str) -> str:
        """让桩模型按给定顺序调工具，最后给一句正文。"""
        queue = list(calls_in_a_row)
        bot = LLMBot()

        async def fake_post(messages, cfg, tools=None):
            seen_prompts.append(str(messages[0].get("content") or ""))
            if queue:
                name, args = queue.pop(0)
                return _tool_call(name, args, f"call_{len(queue)}")
            return {"role": "assistant", "content": final}

        bot._post_chat = fake_post  # type: ignore[method-assign]
        reply, reason = await bot.chat(
            543486099, 42, "张三", "在吗", chat_cfg,
            stickers=[{"kind": "mface", "url": "https://gchat.qpic.cn/live.png", "summary": "偷笑"}],
            send_image=fake_image_out,
        )
        check("chat 正常返回正文", reply == final and reason == "", f"{reply!r}/{reason!r}")
        return reply

    await run_turn([("sticker_add", {"tags": "偷笑 现场收的"})], "收好了")
    check("系统提示词里写了表情包能力", "表情包与发图" in (seen_prompts[0] if seen_prompts else ""),
          (seen_prompts[0] if seen_prompts else "")[-80:])
    check("chat 把 stickers 传进了工具上下文（能收下这张图）",
          bool(st.find("现场收的")), str(st.tag_summary()))

    live_id = st.find("现场收的")[0]["id"] if st.find("现场收的") else ""
    await run_turn([("sticker_send", {"id": live_id})], "发你了")
    check("chat 把 send_image 回调传下去了（图真的发出去了）", len(sent_images) == 1,
          str(len(sent_images)))

    st_mod.download = real_download
    isr.search = real_search

    print("== 群友发的表情包：自动收（llm.sticker_auto_collect）==")
    import music_collector as mc
    from music_collector.config import config_manager

    cfg_live = config_manager.config.llm
    cfg_live.stickers = True
    cfg_live.sticker_auto_collect = True
    cfg_live.sticker_auto_daily = 30
    cfg_live.sticker_dir = ""

    def _mface_event(summary: str, url: str, *, uid: int = 42, gid: int = 677868394):
        return type(
            "Ev",
            (),
            {
                "group_id": gid,
                "user_id": uid,
                "self_id": 1,
                "message": Message(
                    MessageSegment("mface", {"url": url, "summary": summary, "key": "k"})
                ),
            },
        )()

    st_mod.download = fake_download
    before = st.count()
    await mc._auto_collect_sticker(None, _mface_event("偷笑", "https://gchat.qpic.cn/auto1.gif"))
    check("自动收下了一张", st.count() == before + 1, f"{before} → {st.count()}")
    auto = [it for it in st.find("", st.MAX_ITEMS)
            if str(it.get("source") or "").startswith("群677868394")]
    check("标签用了 QQ 给的 summary", bool(auto) and "偷笑" in auto[0]["tags"], str(auto[:1]))
    check("来源标成群 + 表情包", bool(auto) and "商城表情" in auto[0]["source"], str(auto[:1]))
    check("kind 记成 mface", bool(auto) and auto[0]["kind"] == "mface", str(auto[:1]))

    after_first = st.count()
    await mc._auto_collect_sticker(None, _mface_event("偷笑", "https://gchat.qpic.cn/auto1.gif"))
    check("同一张不会再存一份（sha1 去重）", st.count() == after_first, str(st.count()))

    cfg_live.sticker_auto_collect = False
    await mc._auto_collect_sticker(None, _mface_event("另一张", "https://gchat.qpic.cn/auto2.gif"))
    check("开关关掉就不收了", st.count() == after_first, str(st.count()))
    cfg_live.sticker_auto_collect = True

    from datetime import datetime
    day = datetime.now(mc.service.resolver.tz).strftime("%Y-%m-%d")
    mc._sticker_auto_count[day] = 10 ** 6          # 假装今天已经收满了
    await mc._auto_collect_sticker(None, _mface_event("第三张", "https://gchat.qpic.cn/auto3.gif"))
    check("到当天上限就不再自动收", st.count() == after_first, str(st.count()))
    mc._sticker_auto_count.pop(day, None)

    plain = type("Ev", (), {"group_id": 1, "user_id": 42, "self_id": 1,
                            "message": Message("只是说话")})()
    await mc._auto_collect_sticker(None, plain)
    check("普通文字消息不会动库", st.count() == after_first, str(st.count()))

    print("== search_images 会先把库里的候选摆出来 ==")
    isr.reset()
    isr.search = fake_search
    tools_s, ctx_s = tl.build(
        LLMConfig(tools=True, web_tool=False, say_tool=False, image_send_max=2),
        group_id=1, image_out=fake_image_out, image_max=2,
    )
    out = await tl.dispatch(tools_s, "search_images", {"query": "偷笑"}, ctx_s)
    check("返回里有「先看库里」", "先看库里" in out, out[:80])
    check("并给出 sticker_send 的用法", "sticker_send" in out, out[:120])
    check("网上搜到的结果仍附在后面", "https://img.example.com/x.jpg" in out, out[-120:])

    out = await tl.dispatch(tools_s, "search_images", {"query": "库里完全没这个词"}, ctx_s)
    check("库里没命中就不硬塞提示", "先看库里" not in out, out[:80])

    st_mod.download = real_download
    isr.search = real_search

    print("== 没有发图回调时的防御 ==")
    tools3, ctx3 = tl.build(LLMConfig(tools=True, web_tool=False, say_tool=False), group_id=1)
    out = await tl._handle_send_image({"url": "https://img.example.com/ok.png"}, ctx3)
    check("handler 自己兜底：发不了图就说清楚", "发不" in out and "图" in out, out[:60])

    print("== 最近发送（面板要显示的那份历史）==")
    a, _ = await st.add(GIF + b"recent-a", tags="排队A")
    b, _ = await st.add(GIF + b"recent-b", tags="排队B")
    await st.bump_use(a["id"], group=111)
    await st.bump_use(b["id"], group=222)
    rec = st.recent(limit=5)
    check("最近发送有记录", len(rec) >= 2, str(len(rec)))
    check("新的在最前面", rec and rec[0]["id"] == b["id"], str([r["id"] for r in rec[:3]]))
    check("带时间与群号", bool(rec and rec[0]["at"] and rec[0]["group"] == 222), str(rec[:1]))
    check("带上当前标签（面板不用二次请求）", "排队B" in (rec[0]["tags"] if rec else []),
          str(rec[:1]))
    check("条目被删后也留记录（exists=False）", "exists" in (rec[0] if rec else {}), str(rec[:1]))

    print("== 网页面板：表情包 API ==")
    from fastapi import HTTPException

    from music_collector import webui as W

    class _Req:
        """够用就行的假 Request：这几个接口只碰 method / query_params / headers / json()。"""

        def __init__(self, method="GET", query=None, body=None):
            self.method = method
            self.query_params = dict(query or {})
            self.headers = {}
            self._body = body

        async def json(self):
            return self._body

    def _payload(resp):
        return json.loads(bytes(resp.body).decode("utf-8"))

    listed = _payload(await W._api_stickers(_Req()))
    check("GET 列表 ok", listed.get("ok") is True, str(listed)[:80])
    check("列出的条数和库一致", len(listed.get("items") or []) == st.count(),
          f"{len(listed.get('items') or [])} vs {st.count()}")
    check("stats 也一起给了", (listed.get("stats") or {}).get("count") == st.count(),
          str((listed.get("stats") or {}).get("count")))
    check("recent 一起给了", isinstance(listed.get("recent"), list), str(type(listed.get("recent"))))
    check("带开关状态（面板要据此提示）",
          "enabled" in listed and "send_max" in listed, str(sorted(listed))[:80])

    target = (listed.get("items") or [])[0]
    media = await W._api_stickers(_Req(query={"media": "1", "id": target["id"]}))
    check("取图返回图片本体", str(media.media_type).startswith("image/") and len(media.body) > 16,
          f"{media.media_type} / {len(media.body)}B")

    try:
        await W._api_stickers(_Req(query={"media": "1", "id": "nope"}))
        check("取不存在的图回 404", False, "居然没抛")
    except HTTPException as exc:
        check("取不存在的图回 404", exc.status_code == 404, str(exc.status_code))

    upd = _payload(await W._api_stickers(_Req("POST", body={
        "action": "update", "id": target["id"], "tags": "无语 手改的", "note": "面板改的"})))
    saved = st.get(target["id"]) or {}
    check("改标签成功", upd.get("ok") is True, str(upd)[:80])
    check("标签真的写进库了", "手改的" in (saved.get("tags") or []), str(saved.get("tags")))
    check("备注也写进去了", saved.get("note") == "面板改的", str(saved.get("note")))

    bad = _payload(await W._api_stickers(_Req("POST", body={
        "action": "update", "id": target["id"], "tags": "  "})))
    check("空标签被拒（否则模型再也搜不到它）",
          bad.get("ok") is False and "标签" in str(bad.get("message")), str(bad)[:80])

    victim, _ = await st.add(GIF + b"panel-del", tags="待删 面板")
    victim_path = st.path_of(victim)
    gone = _payload(await W._api_stickers(_Req("POST", body={
        "action": "delete", "id": victim["id"]})))
    check("删除成功", gone.get("ok") is True, str(gone)[:80])
    check("索引里没了", st.get(victim["id"]) is None, "还在")
    check("图片文件也删了", victim_path is not None and not victim_path.is_file(), str(victim_path))

    print()
    bad = [c for c in checks if not c[1]]
    if bad:
        print(f"失败 {len(bad)} 项：")
        for label, _, extra in bad:
            print(f"  - {label} {extra}")
        return 1
    print(f"通过 {len(checks)} 项，失败 0 项")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

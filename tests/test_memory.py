"""群记忆：人物档案、路径防护、注入预算、聊天记录持久化、整理结果解析。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_memory.py

**绝不碰真实 data/**：所有用例都显式把 ``MemoryConfig.dir`` 指到临时目录，
数据库也用临时文件——否则跑一次测试就会把线上档案搅乱。
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import digest as dg  # noqa: E402
from music_collector import llm as llm_mod  # noqa: E402
from music_collector import memory as mm  # noqa: E402
from music_collector import tools as tl  # noqa: E402
from music_collector.config import AppConfig, LLMConfig, MemoryConfig  # noqa: E402
from music_collector.llm import LLMBot  # noqa: E402
from music_collector.store import Store  # noqa: E402

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


def tmp_mem(**kw) -> MemoryConfig:
    """一个指向临时目录的记忆配置。"""
    root = tempfile.mkdtemp(prefix="mem-")
    return MemoryConfig(dir=root, **kw)


# ---------------------------------------------------------------- 路径防护


def test_paths() -> None:
    print("\n[1] 路径：QQ 号必须是纯数字，路径出不了 people/")
    mem = tmp_mem()
    for bad in ("../etc", "12/34", "..", "abc", "", "1 2", "１２３"):
        try:
            mm._path_for(mem, 1, bad)
            check(f"拒绝 uid={bad!r}", False, "没报错")
        except ValueError:
            check(f"拒绝 uid={bad!r}", True)
    # 群号同理
    for bad in (-1, 0.5):
        try:
            mm.group_dir(mem, bad)
            check(f"拒绝群号={bad!r}", False, "没报错")
        except (ValueError, TypeError):
            check(f"拒绝群号={bad!r}", True)
    check("合法 uid 得到 people/123.md",
          mm._path_for(mem, 1, "123").name == "123.md")
    check("根目录可解析", mm.memory_root(mem) == Path(mem.dir))


# ---------------------------------------------------------------- 档案读写


def test_person() -> None:
    print("\n[2] 档案：追加 / 去重 / 删除 / 整段重写 / 挤掉旧记录")
    mem = tmp_mem()
    ok, msg = mm.append_note(mem, 100, "123", "喜欢周杰伦", "小明")
    check("首次写入成功", ok, msg)
    person = mm.load_person(mem, 100, "123")
    check("落盘了昵称", person is not None and person.nick == "小明", str(person))
    check("正文是列表项", person.body.startswith("- "), person.body[:40])
    check("首次记录时间已填", bool(person.first_seen))

    ok2, msg2 = mm.append_note(mem, 100, "123", "喜欢周杰伦", "小明")
    check("同一条不重复写", not ok2 and "记过" in msg2, msg2)

    ok3, _ = mm.append_note(mem, 100, "123", "在杭州读大三")
    person = mm.load_person(mem, 100, "123")
    check("第二条追加成功", ok3 and len(person.notes()) == 2, str(person.notes()))

    # 整段重写（网页端编辑走这条）
    mm.rewrite_body(mem, 100, "123", "- 只留这一条")
    person = mm.load_person(mem, 100, "123")
    check("重写覆盖正文", person.notes() == ["只留这一条"], str(person.notes()))
    check("重写不动昵称", person.nick == "小明", person.nick)

    # 删除某条
    mm.append_note(mem, 100, "123", "养了一只猫")
    removed, msg3 = mm.forget_note(mem, 100, "123", "猫")
    check("按关键词删掉 1 条", removed == 1 and "删掉" in msg3, msg3)
    check("删完只剩原来那条",
          mm.load_person(mem, 100, "123").notes() == ["只留这一条"])
    removed2, _ = mm.forget_note(mem, 100, "123", "不存在的词")
    check("删不到时不改文件", removed2 == 0)

    # 上限：挤掉最早的
    tight = tmp_mem(note_chars=200)
    for i in range(12):
        mm.append_note(tight, 100, "9", f"第{i}条记录内容比较长一点用来占位置")
    body = mm.load_person(tight, 100, "9").body
    check("档案不超过上限（+省略号）", len(body) <= 205, str(len(body)))
    check("最早的记录被挤掉", "第0条" not in body, body[:60])
    check("最新的记录还在", "第11条" in body, body[-60:])

    # 坏文件不抛异常
    bad = mm._path_for(tight, 100, "555")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\n这不是: [合法 yaml\n---\n正文还在\n", encoding="utf-8")
    person = mm.load_person(tight, 100, "555")
    check("frontmatter 坏掉仍能读正文", person is not None and "正文还在" in person.body,
          str(person))


def test_touch_and_list() -> None:
    print("\n[3] 活跃度：touch 建档 / 累加消息数 / 列表按最近活跃排序")
    mem = tmp_mem()
    created = mm.touch(mem, 200, "111", "老王")
    check("首次 touch 建档", created)
    p = mm.load_person(mem, 200, "111")
    check("消息数 = 1", p.msgs == 1, str(p.msgs))
    created2 = mm.touch(mem, 200, "111", "")
    p = mm.load_person(mem, 200, "111")
    check("再次 touch 不重复建档", not created2)
    check("消息数累加", p.msgs == 2, str(p.msgs))
    check("空昵称不会覆盖已有昵称", p.nick == "老王", p.nick)

    mm.touch(mem, 200, "222", "小李")
    mm.append_note(mem, 200, "222", "常点夜宵")
    people = mm.list_people(mem, 200)
    check("列表含 2 人", len(people) == 2, str([x.uid for x in people]))
    check("最近动的排最前", people[0].uid in ("111", "222"), people[0].uid)

    mm.delete_person(mem, 200, "222")
    check("删除后只剩 1 人", len(mm.list_people(mem, 200)) == 1)
    check("删除不存在的返回 False", not mm.delete_person(mem, 200, "999"))


def test_resolve() -> None:
    print("\n[4] 认人：QQ 号直通 / 昵称唯一 / 重名不猜")
    mem = tmp_mem()
    mm.append_note(mem, 300, "1", "喜欢摇滚", "小明")
    mm.append_note(mem, 300, "2", "喜欢民谣", "小明")
    mm.append_note(mem, 300, "3", "在深圳", "阿强")

    uid, _ = mm.resolve_who(mem, 300, "3")
    check("QQ 号直接命中", uid == "3", str(uid))
    uid, why = mm.resolve_who(mem, 300, "阿强")
    check("唯一昵称命中", uid == "3", why)
    uid, why = mm.resolve_who(mem, 300, "小明")
    check("重名拒绝猜", uid is None and "2 个人" in why, why)
    uid, why = mm.resolve_who(mem, 300, "查无此人")
    check("找不到给出提示", uid is None and "没有叫" in why, why)
    uid, why = mm.resolve_who(mem, 300, "")
    check("空输入不报错", uid is None and "没说是谁" in why, why)


def test_render() -> None:
    print("\n[5] 注入：群概况 + 成员摘要 + 预算截断 + 关闭即空")
    mem = tmp_mem()
    mm.write_group_note(mem, 400, "本群黑话：yyds=永远的神")
    for i in range(6):
        mm.append_note(mem, 400, str(100 + i), f"第{i}个人的特征描述", f"成员{i}")

    text = mm.render_for_prompt(mem, 400)
    check("带群概况", "【群概况】" in text and "yyds" in text, text[:60])
    check("带成员档案", "【群成员档案】" in text)
    check("人数写对", "共 6 人" in text, text[:120])
    check("摘要里有人名", "成员0(100)" in text or "成员5(105)" in text)

    # 预算砍到很小：只装得下一两个人，并注明还剩几个
    small = tmp_mem(inject_chars=1000)
    mm.write_group_note(small, 400, "黑话")
    for i in range(30):
        mm.append_note(small, 400, str(500 + i), "很长的特征描述" * 5, f"人{i}")
    text = mm.render_for_prompt(small, 400)
    check("预算生效（不会全装）", text.count("\n- ") < 30, str(text.count("\n- ")))
    check("注明还有几人没装", "还有" in text and "没装进来" in text, text[-80:])
    check("长度受控", len(text) <= 1400, str(len(text)))

    check("关闭时返回空", mm.render_for_prompt(MemoryConfig(enabled=False, dir=mem.dir), 400) == "")
    check("没档案时返回空", mm.render_for_prompt(mem, 99999) == "")


def test_group_note() -> None:
    print("\n[6] 群概况：写入 / 追加去重")
    mem = tmp_mem()
    ok, _ = mm.write_group_note(mem, 500, "群规：不刷屏")
    check("写入成功", ok)
    check("读得回来", "不刷屏" in mm.read_group_note(mem, 500))
    added, _ = mm.append_group_note(mem, 500, ["黑话：awsl=啊我死了", "群规：不刷屏"])
    check("只追加新的一条", added == 1, str(added))
    note = mm.read_group_note(mem, 500)
    check("两条都在", "awsl" in note and "不刷屏" in note, note)
    check("空追加返回 0", mm.append_group_note(mem, 500, [])[0] == 0)


# ---------------------------------------------------------------- 聊天记录


def _store() -> Store:
    path = Path(tempfile.mkdtemp(prefix="memdb-")) / "t.db"
    store = Store(path)
    asyncio.run(store.init())
    return store


def test_chat_log() -> None:
    print("\n[7] 聊天记录：落库 / 按 pair 裁剪 / 旁听隔离 / 整理游标")
    store = _store()
    now = time.time()

    async def fill():
        await store.append_chat(1, 11, "小明", "user", "第一问", created_at=now - 40)
        await store.append_chat(1, 0, "", "assistant", "第一答", created_at=now - 39)
        await store.append_chat(1, 11, "小明", "user", "第二问", created_at=now - 20)
        await store.append_chat(1, 0, "", "assistant", "第二答", created_at=now - 19)
        await store.append_chat(1, 12, "小红", "user", "旁听的话", kind="listen",
                                created_at=now - 10)
        await store.append_chat(1, 0, "", "user", "", created_at=now)  # 空内容跳过

    asyncio.run(fill())
    rows = asyncio.run(store.load_chat(1))
    check("chat 只取对话（4 条，不含旁听）", len(rows) == 4,
          str([r["content"] for r in rows]))
    check("时间正序", rows[0]["content"] == "第一问" and rows[-1]["content"] == "第二答",
          str([r["content"] for r in rows]))
    check("首条是 user", rows[0]["role"] == "user")

    cut = asyncio.run(store.load_chat(1, max_chars=8))
    check("预算裁剪成对丢弃", len(cut) == 2 and cut[0]["content"] == "第二问",
          str([r["content"] for r in cut]))

    only_new = asyncio.run(store.load_chat(1, since=now - 25))
    check("since 过滤掉旧消息", len(only_new) == 2, str(len(only_new)))

    stats = asyncio.run(store.chat_log_stats(1))
    check("统计：5 条（旁听也算）", stats["rows"] == 5, str(stats))
    check("统计：待整理 3 条 user", stats["pending"] == 3, str(stats))

    pending = asyncio.run(store.pending_digest(1, limit=10))
    check("待整理只含 user", all(p["content"] != "第一答" for p in pending), str(pending))
    check("待整理含旁听内容", any(p["content"] == "旁听的话" for p in pending),
          str([p["content"] for p in pending]))

    last_id = max(p["id"] for p in pending)
    marked = asyncio.run(store.mark_digested(1, last_id))
    check("标记已整理", marked == 3, str(marked))
    check("再取就没有了", asyncio.run(store.pending_digest(1)) == [])

    check("群列表含 1", asyncio.run(store.chat_groups()) == [1])
    cleared = asyncio.run(store.clear_chat(1, kind="chat"))
    check("只清对话不清旁听", cleared == 4, str(cleared))
    check("旁听还在", len(asyncio.run(store.load_chat(1, kind="listen"))) == 1)

    gone = asyncio.run(store.prune_chat(time.time() + 10))
    check("按时间清理全删", gone == 1, str(gone))


def test_llm_persistence() -> None:
    print("\n[8] 上下文持久化：从库预热 / 清空会落库")
    store = _store()
    cfg = LLMConfig(history_rounds=10, cooldown=0)

    async def seed():
        await store.append_chat(9, 1, "张三", "user", "重启前的问题")
        await store.append_chat(9, 0, "", "assistant", "重启前的回答", "想了想")

    asyncio.run(seed())
    bot = LLMBot()
    bot.set_store(store)
    check("未注入后端时没有 store", LLMBot()._store is None)
    asyncio.run(bot._sync_history(9, cfg))
    hist = bot.history(9)
    check("预热出 2 条", len(hist) == 2, str(len(hist)))
    check("内容正确", hist[0]["content"] == "重启前的问题")
    check("思考内容也回来了", hist[1].get("reasoning_content") == "想了想", str(hist[1]))

    # 清空：内存立刻空，库里要等下一次 sync 才删
    bot.clear_history(9)
    check("内存已清空", bot.history(9) == [])
    asyncio.run(bot._sync_history(9, cfg))
    check("库里也清了", asyncio.run(store.load_chat(9)) == [])
    check("清空后不会又冒出来", bot.history(9) == [])

    # 预热条数要跟着字数预算走，**不能被旧的硬编码 4000 卡住**——
    # 卡住的话 history_chars 配到 160 万也吃不满 1M 窗口（20~40 万字就到顶了）。
    seen: dict = {}

    async def fake_load(group_id, *, kind="chat", max_chars=0, limit=0, since=0.0):
        seen["limit"] = limit
        return []

    spy = _store()
    spy.load_chat = fake_load  # type: ignore[method-assign]
    big_bot = LLMBot()
    big_bot.set_store(spy)
    big = LLMConfig(history_rounds=20000, history_chars=1_400_000)
    asyncio.run(big_bot._sync_history(77, big))
    check("大预算下预热条数 > 4000（不再被硬编码卡住）", seen.get("limit", 0) > 4000, str(seen))
    check("按字数预算反推条数（1400000/40=35000）", seen.get("limit") == 35000, str(seen))

    # 反过来：轮数小的时候仍然听轮数的
    seen.clear()
    small_bot = LLMBot()
    small_bot.set_store(spy)
    asyncio.run(small_bot._sync_history(78, LLMConfig(history_rounds=10, history_chars=1_400_000)))
    check("轮数更小时以轮数为准（10 轮=20 条）", seen.get("limit") == 20, str(seen))

    # 预算不设上限（history_chars=0）时退回轮数，不受字数限制
    seen.clear()
    asyncio.run(small_bot._sync_history(79, LLMConfig(history_rounds=3000, history_chars=0)))
    check("history_chars=0 时不按字数砍（3000 轮=6000 条）", seen.get("limit") == 6000, str(seen))


def test_system_text() -> None:
    print("\n[9] 系统提示词顺序：人设 → 记忆 → 硬规则")
    cfg = LLMConfig(system_prompt="你是助手")
    text = llm_mod.system_text(cfg, "\n【规则】边界", "\n【档案】小明喜欢摇滚")
    check("人设在前", text.startswith("你是助手"), text[:20])
    check("档案在规则之前",
          text.index("【档案】") < text.index("【规则】"), text)
    check("规则仍在末尾", text.endswith("【规则】边界"))
    # 人设写满也挤不掉规则
    long_cfg = LLMConfig(system_prompt="啊" * 9000)
    text2 = llm_mod.system_text(long_cfg, "\n规则", "\n档案")
    check("人设被截断到 4000", len(text2) < 4000 + 20, str(len(text2)))
    check("截断后档案和规则都还在", "档案" in text2 and text2.endswith("规则"))


# ---------------------------------------------------------------- 整理


def test_parse_reply() -> None:
    print("\n[10] 整理结果解析：纯 JSON / 代码块 / 前后带话 / 坏数据")
    good = {"people": [{"uid": "1", "nick": "小明", "facts": ["喜欢摇滚"]}], "group": ["黑话"]}
    raw = json.dumps(good, ensure_ascii=False)
    check("纯 JSON", dg.parse_reply(raw) == good)
    check("代码块包裹", dg.parse_reply("```json\n" + raw + "\n```") == good)
    check("前后有解释", dg.parse_reply("好的，结果如下：\n" + raw + "\n希望有帮助") == good)
    check("不是 JSON 返回空", dg.parse_reply("我不知道") == {})
    check("空输入返回空", dg.parse_reply("") == {})
    check("数组不认", dg.parse_reply("[1,2,3]") == {})


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.marked = []

    async def pending_digest(self, gid, limit=300, since=0.0):
        return list(self.rows)

    async def mark_digested(self, gid, up_to_id=0):
        self.marked.append((gid, up_to_id))
        return len(self.rows)

    async def chat_groups(self):
        return [1]


class _FakeService:
    def __init__(self, store, app_cfg):
        self.store = store
        self._app = app_cfg

    def cfg(self, gid):
        return self._app


class _FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def complete(self, *a, **kw):
        self.calls += 1
        return self.text, ""


def test_digest_flow() -> None:
    print("\n[11] 整理流程：只写材料里出现过的人 / 失败不消耗素材")
    mem = tmp_mem()
    app = AppConfig(
        llm=LLMConfig(enabled=True, base_url="http://x/v1", api_key="k"),
        memory=mem,
    )
    rows = [
        {"id": 10, "user_id": 123, "nick": "小明", "content": "我最近在听摇滚"},
        {"id": 11, "user_id": 456, "nick": "小红", "content": "我在杭州读大三"},
    ]
    payload = {
        "people": [
            {"uid": "123", "nick": "小明", "facts": ["喜欢摇滚"]},
            {"uid": "999", "nick": "编的", "facts": ["这条不该写进去"]},
        ],
        "group": ["群里爱聊音乐"],
    }
    store = _FakeStore(rows)
    fake = _FakeLLM(json.dumps(payload, ensure_ascii=False))

    old_store, old_llm = dg.service, dg.llm_bot
    dg.service, dg.llm_bot = _FakeService(store, app), fake
    try:
        res = asyncio.run(dg.digest_group(1, mem=mem))
        check("状态 ok", res.get("status") == "ok", str(res))
        check("写了 1 个人 1 条事实", res.get("facts") == 1 and res.get("people") == 1,
              str(res))
        p = mm.load_person(mem, 1, "123")
        check("小明档案有内容", p is not None and "喜欢摇滚" in p.body, str(p))
        check("幽灵 uid 没被写", mm.load_person(mem, 1, "999") is None)
        check("群概况写入", "爱聊音乐" in mm.read_group_note(mem, 1))
        check("素材被标记已整理", store.marked == [(1, 11)], str(store.marked))

        # 模型回一堆废话 → 不消耗素材，下次还能重试
        store2 = _FakeStore(rows)
        bad = _FakeLLM("抱歉，我不太确定。")
        dg.service, dg.llm_bot = _FakeService(store2, app), bad
        res2 = asyncio.run(dg.digest_group(1, mem=mem))
        check("坏 JSON 报 error", res2.get("status") == "error", str(res2))
        check("坏 JSON 不标记已整理", store2.marked == [], str(store2.marked))

        # 没有新素材 → skipped
        store3 = _FakeStore([])
        dg.service, dg.llm_bot = _FakeService(store3, app), _FakeLLM("{}")
        res3 = asyncio.run(dg.digest_group(1, mem=mem))
        check("没消息时 skipped", res3.get("status") == "skipped", str(res3))

        # 记忆关了 → 不动
        dg.service = _FakeService(store3, app)
        res4 = asyncio.run(dg.digest_group(1, mem=MemoryConfig(enabled=False, dir=mem.dir)))
        check("关闭时 skipped", res4.get("status") == "skipped", str(res4))
    finally:
        dg.service, dg.llm_bot = old_store, old_llm


# ---------------------------------------------------------------- 工具


def test_tools() -> None:
    print("\n[12] 工具：remember / recall / forget 端到端")
    mem = tmp_mem()
    cfg = LLMConfig()
    built, ctx = tl.build(cfg, 700, 42, "小明", mem)
    names = [t.name for t in built]
    check("注册了三个记忆工具",
          {"remember", "recall", "forget"} <= set(names), str(names))
    check("都在白名单里", {"remember", "recall", "forget"} <= tl.ALLOWED_TOOL_NAMES)
    check("ctx 带昵称", ctx.nick == "小明")

    out = asyncio.run(tl.dispatch(built, "remember", {"note": "喜欢周杰伦"}, ctx))
    check("记下当前说话的人", "已记到" in out, out[:60])
    p = mm.load_person(mem, 700, "42")
    check("昵称自动补上", p is not None and p.nick == "小明", str(p))
    check("正文有这条", "周杰伦" in p.body, p.body)

    out = asyncio.run(tl.dispatch(built, "remember",
                                  {"note": "在杭州读大三", "who": "42"}, ctx))
    check("按 QQ 号记别人/自己", "已记到" in out, out[:60])

    out = asyncio.run(tl.dispatch(built, "recall", {}, ctx))
    check("recall 全群一览", "群成员档案" in out and "小明" in out, out[:80])
    out = asyncio.run(tl.dispatch(built, "recall", {"who": "小明"}, ctx))
    check("recall 单人含全文", "周杰伦" in out and "杭州" in out, out[:80])
    out = asyncio.run(tl.dispatch(built, "recall", {"who": "查无此人"}, ctx))
    check("recall 找不到有提示", "没有叫" in out, out[:80])

    out = asyncio.run(tl.dispatch(built, "forget", {"keyword": "周杰伦"}, ctx))
    check("forget 删掉指定条目", "删掉" in out, out[:80])
    check("确实没了", "周杰伦" not in mm.load_person(mem, 700, "42").body)

    off, _ = tl.build(cfg, 700, 42, "小明", MemoryConfig(enabled=False, dir=mem.dir))
    check("记忆关掉就不给记忆工具",
          not ({"remember", "recall", "forget"} & {t.name for t in off}),
          str([t.name for t in off]))

    # 记忆工具名不能被当成「装技能」拒绝
    check("remember 不在拒绝正则里", not tl.looks_like_install("remember"))
    check("install_skill 会被拒", tl.looks_like_install("install_skill"))
    # 工具总闸关掉时也不给
    no_tools, _ = tl.build(LLMConfig(tools=False), 700, 42, "小明", mem)
    check("tool 总闸关掉全没有", no_tools == [])


# ---------------------------------------------------------------- 配置


def test_config() -> None:
    print("\n[13] 配置：默认值 / 上限 / 进程级不可按群覆盖")
    from music_collector.config import is_group_scopable

    mem = MemoryConfig()
    check("默认开启", mem.enabled and mem.listen)
    check("默认每 6 小时整理", mem.digest_hours == 6.0, str(mem.digest_hours))
    check("默认注入 6 万字", mem.inject_chars == 60000)
    check("默认单人上限 8000", mem.note_chars == 8000)
    for bad_kw in ({"inject_chars": 0}, {"note_chars": 10}, {"digest_hours": -1}):
        try:
            MemoryConfig(**bad_kw)
            check(f"拒绝 {bad_kw}", False, "没报错")
        except Exception:
            check(f"拒绝 {bad_kw}", True)
    check("memory.* 不按群覆盖", not is_group_scopable("memory.enabled"))
    check("llm.* 不按群覆盖", not is_group_scopable("llm.model"))
    check("AppConfig 有 memory 段", isinstance(AppConfig().memory, MemoryConfig))


def main() -> None:
    test_paths()
    test_person()
    test_touch_and_list()
    test_resolve()
    test_render()
    test_group_note()
    test_chat_log()
    test_llm_persistence()
    test_system_text()
    test_parse_reply()
    test_digest_flow()
    test_tools()
    test_config()
    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 52)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

"""三桶记忆结构：旧格式兼容、三节往返幂等、条目配额、覆盖防护、并发单例。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_digest_buckets.py

**绝不碰真实 data/**：``MemoryConfig.dir`` 一律指向临时目录，``MUSIC_DATA_DIR``
也指到临时目录（和 test_memory.py 同一套做法）。
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="buckets-data-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import digest as dg  # noqa: E402
from music_collector import memory as mm  # noqa: E402
from music_collector.config import AppConfig, LLMConfig, MemoryConfig  # noqa: E402

PASSED = 0
FAILED = 0

#: 一次整理看到的聊天记录。``user_id`` 必须是 123，否则「幽灵 uid」那条红线会把它筛掉
_ROWS = [
    {"id": 10, "user_id": 123, "nick": "小明", "content": "我在杭州读大三"},
    {"id": 11, "user_id": 123, "nick": "小明", "content": "九月十六要交作业"},
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def tmp_mem(**kw) -> MemoryConfig:
    return MemoryConfig(dir=tempfile.mkdtemp(prefix="buckets-"), **kw)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def person_path(mem: MemoryConfig, uid: str = "123") -> Path:
    return mm._path_for(mem, 1, uid)


# ---------------------------------------------------------------- 桩


class _FakeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self.marked: list = []

    async def pending_digest(self, gid, limit=300, since=0.0):
        return list(self.rows)

    async def mark_digested(self, gid, up_to_id=0):
        self.marked.append((gid, up_to_id))
        return len(self.rows)


class _FakeService:
    def __init__(self, store, app):
        self.store = store
        self._app = app

    def cfg(self, gid):
        return self._app


class _FakeLLM:
    """记下被调用了几次——「素材/重复触发不该调模型」这类断言全靠它。"""

    def __init__(self, text=""):
        self.text = text
        self.calls = 0

    async def complete(self, *a, **kw):
        self.calls += 1
        return self.text, ""


def _run(mem: MemoryConfig, reply: str, rows=None):
    """跑一次 digest_group，返回 ``(结果, store, llm)``。"""
    import asyncio

    app = AppConfig(
        llm=LLMConfig(enabled=True, base_url="http://x/v1", api_key="k"),
        memory=mem,
    )
    store = _FakeStore(_ROWS if rows is None else rows)
    llm = _FakeLLM(reply)
    old_store, old_llm = dg.service, dg.llm_bot
    dg.service, dg.llm_bot = _FakeService(store, app), llm
    try:
        return asyncio.run(dg.digest_group(1, mem=mem)), store, llm
    finally:
        dg.service, dg.llm_bot = old_store, old_llm


def reset_guard() -> None:
    """清掉「整理中」标记（单例断言之外每个用例都从干净状态开始）。"""
    dg._DIGESTING.clear()


def seed(mem: MemoryConfig, sections: dict, nick: str = "小明") -> str:
    """按三桶格式写一份档案，返回落盘文本（供「库内容不变」逐字比对）。"""
    person = mm.Person(uid="123", nick=nick)
    person.body = mm.join_body(sections)
    mm.save_person(mem, 1, person)
    return person_path(mem).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1 旧格式


def test_legacy_body() -> None:
    print("\n[1] 旧格式档案（正文没有 ## 小节）：读得进、内容不丢、能再写回")
    mem = tmp_mem()
    path = person_path(mem)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        '---\nuid: "123"\nnick: "老王"\nmsgs: 3\n---\n'
        "- 2026-09-14 喜欢摇滚\n- 在杭州读大三\n"
    )
    path.write_text(raw, encoding="utf-8")

    person = mm.parse_person(path.read_text(encoding="utf-8"), "123")
    check("旧格式读进来不抛、昵称还在", person.nick == "老王", str(person))
    check("条目一条不少",
          person.notes() == ["2026-09-14 喜欢摇滚", "在杭州读大三"], str(person.notes()))
    sections, rest = mm.parse_body(person.body)
    check("旧格式解析不出桶", sections == {}, str(sections))
    check("整段原文留在「认不出的部分」里", "喜欢摇滚" in rest, rest)
    check("原样拼回去逐字不变", mm.join_body(sections, rest) == person.body)
    check("dump → parse → dump 幂等",
          mm.dump_person(mm.parse_person(mm.dump_person(person), "123"))
          == mm.dump_person(person))

    # 三桶档案里混着旧格式内容（比如人手工粘进去的一段）：同样不许丢
    mixed = mm.join_body(
        {"who": ["是学生"]}, rest="- 手写的一段旧记录\n- 另一条"
    )
    check("三桶 + 旧内容能共存", "手写的一段旧记录" in mixed and "## 他是谁" in mixed, mixed)
    check("混合内容再拼一次也稳定", mm.join_body(*mm.parse_body(mixed)) == mixed)


# ---------------------------------------------------------------- 2 往返幂等


def test_roundtrip() -> None:
    print("\n[2] dump_person / parse_person 三节往返幂等")
    sections = {
        "who": ["是学生，住寝室（2026-09-14 提到，2 次）", "在上海，独居"],
        "us": ["习惯每天互道晚安"],
        "event": ["9月16日要交作业（2026-09-15 提到）"],
    }
    body = mm.join_body(sections)
    for title in ("## 他是谁", "## 我们之间", "## 事件"):
        check(f"正文有小节 {title}", title in body, body)
    parsed, rest = mm.parse_body(body)
    check("解析回来与写入一致", parsed == sections, str(parsed))
    check("没有剩余内容", rest == "", repr(rest))
    check("再拼一次逐字相同", mm.join_body(*mm.parse_body(body)) == body)

    person = mm.Person(uid="123", nick="小明")
    person.body = body
    text = mm.dump_person(person)
    check("经 frontmatter 往返正文不变", mm.parse_person(text, "123").body == body)
    check("notes() 只取三桶、顺序稳定",
          mm.parse_person(text, "123").notes() == sections["who"] + sections["us"]
          + sections["event"], str(mm.parse_person(text, "123").notes()))

    # 未分类也参与往返，且不会在第二次拼装时被挪位置
    body2 = mm.join_body({**sections, mm.UNSORTED: ["旧格式记的一句"]})
    check("未分类能往返",
          mm.parse_body(body2)[0].get(mm.UNSORTED) == ["旧格式记的一句"], body2)
    check("未分类再拼幂等", mm.join_body(*mm.parse_body(body2)) == body2)
    check("空桶不写空标题", "## 我们之间" not in mm.join_body({"who": ["x"]}))


# ---------------------------------------------------------------- 3 配额


def test_digest_quota() -> None:
    print("\n[3] 一次整理：三节齐全 / 整库 ≤ l1_max_facts / 每桶 ≤ 配额")
    reset_guard()
    mem = tmp_mem(
        buckets_enabled=True, l1_max_facts=8, l1_quota={"who": 3, "us": 3, "event": 2}
    )
    payload = {
        "people": [
            {
                "uid": "123",
                "nick": "小明",
                "who": [{"c": f"人设{i}", "seen": "2026-09-14", "n": 1} for i in range(5)],
                "us": [{"c": f"关系{i}", "seen": "2026-09-15", "n": 2} for i in range(5)],
                "event": [{"c": f"{i}月1日有事", "seen": "2026-09-15"} for i in range(5)],
                "recent_new": [{"d": today(), "c": "中午刚醒，说要去吃饭"}],
            }
        ],
        "group": ["群里爱聊音乐"],
    }
    res, store, _llm = _run(mem, json.dumps(payload, ensure_ascii=False))
    check("状态 ok", res.get("status") == "ok", str(res))

    person = mm.load_person(mem, 1, "123")
    sections, _rest = mm.parse_body(person.body)
    check("三节齐全", all(sections.get(k) for k in mm.BUCKETS), str(sections))
    counts = {k: len(sections.get(k) or []) for k in mm.BUCKETS}
    check("每桶不超配额", counts == {"who": 3, "us": 3, "event": 2}, str(counts))
    check("整库不超 l1_max_facts", sum(counts.values()) <= 8, str(counts))
    check("近期流水落盘",
          any("中午刚醒" in x for x in sections.get(mm.RECENT) or []),
          str(sections.get(mm.RECENT)))
    check("近期不占长期配额", sum(counts.values()) == 8, str(counts))
    check("日期与次数写进句子", "2026-09-15 提到，2 次" in person.body, person.body[:200])
    check("群概况写入", "爱聊音乐" in mm.read_group_note(mem, 1))
    check("素材被标记已整理", store.marked == [(1, 11)], str(store.marked))

    # 敏感条目：一条都不许落盘
    mem2 = tmp_mem(buckets_enabled=True)
    payload2 = {
        "people": [
            {
                "uid": "123",
                "who": [
                    {"c": "手机号 138xxxx0000", "sensitive": True},
                    {"c": "是学生"},
                ],
            }
        ]
    }
    res2, _store2, _ = _run(mem2, json.dumps(payload2, ensure_ascii=False))
    body2 = mm.load_person(mem2, 1, "123").body
    check("敏感条目不落盘", "138" not in body2, body2)
    check("非敏感条目照常落盘", "是学生" in body2, body2)
    check("统计里记了丢弃条数", res2.get("sensitive") == 1, str(res2))

    # 近期流水按天淘汰：三天前的条目（recent_days=3）应被清掉
    mem3 = tmp_mem(buckets_enabled=True, recent_days=3)
    old_day = "2020-01-01"
    payload3 = {
        "people": [
            {
                "uid": "123",
                "who": [{"c": "是学生"}],
                "recent_new": [
                    {"d": old_day, "c": "很久以前的一句话"},
                    {"d": today(), "c": "今天说的"},
                ],
            }
        ]
    }
    _run(mem3, json.dumps(payload3, ensure_ascii=False))
    body3 = mm.load_person(mem3, 1, "123").body
    check("过期流水被淘汰", "很久以前的一句话" not in body3, body3)
    check("当天流水保留", "今天说的" in body3, body3)


# ---------------------------------------------------------------- 4 覆盖防护


def test_cover_guard() -> None:
    print("\n[4] 覆盖防护：条目数不足一半 → 告警 + 用 .bak 回滚，库内容不变")
    reset_guard()
    mem = tmp_mem(buckets_enabled=True)
    before = seed(
        mem,
        {
            "who": [f"人设{i}" for i in range(4)],
            "us": ["习惯互道晚安"],
            "event": ["9月16日交作业"],
        },
    )

    # 既有 6 条，模型只回 1 条 → 1×2 < 6，判为疑似误删
    payload = {"people": [{"uid": "123", "who": [{"c": "只剩这一条"}]}]}
    res, _store, _llm = _run(mem, json.dumps(payload, ensure_ascii=False))
    after = person_path(mem).read_text(encoding="utf-8")
    check("这一人被回滚", res.get("rolled_back") == ["123"], str(res))
    check("整体状态 partial", res.get("status") == "partial", str(res))
    check("库内容一个字都没变", after == before, after[:160])
    check("留下了 .bak", person_path(mem).with_name("123.md.bak").exists())

    # 模型回空 people 数组 → 不写、不消耗素材
    res2, store2, _ = _run(mem, json.dumps({"people": []}, ensure_ascii=False))
    check("空数组判为失败", res2.get("status") == "error", str(res2))
    check("空数组不写盘", person_path(mem).read_text(encoding="utf-8") == before)
    check("空数组不消耗素材", store2.marked == [], str(store2.marked))

    # 人给了但三个桶全空 → 一条都算不出来，同样不写
    res3, _store3, _ = _run(
        mem, json.dumps({"people": [{"uid": "123", "who": [], "us": [], "event": []}]},
                        ensure_ascii=False)
    )
    check("全空桶不写盘", person_path(mem).read_text(encoding="utf-8") == before, str(res3))

    # 条目数没掉一半 → 正常写入（防护不能把正常整理也拦下来）
    payload4 = {
        "people": [
            {
                "uid": "123",
                "who": [f"人设{i}" for i in range(4)],
                "us": ["习惯互道晚安"],
                "event": ["9月16日交作业"],
            }
        ]
    }
    res4, _store4, _ = _run(mem, json.dumps(payload4, ensure_ascii=False))
    check("原样抄回来就正常通过", res4.get("status") == "ok", str(res4))


# ---------------------------------------------------------------- 5 坏输入


def test_bad_reply() -> None:
    print("\n[5] 非法 JSON / 顶层非对象 / 代码块包裹：跳过本次，库不变")
    reset_guard()
    mem = tmp_mem(buckets_enabled=True)
    before = seed(mem, {"who": ["是学生"], "us": ["习惯互道晚安"], "event": ["9月16日交作业"]})

    for bad in ("抱歉，我不太确定。", "[1, 2, 3]", '{"people": [ 坏掉了'):
        res, store, _llm = _run(mem, bad)
        check(f"坏输入判失败（{bad[:12]}…）", res.get("status") == "error", str(res))
        check("坏输入不消耗素材", store.marked == [], str(store.marked))
        check("坏输入不动库", person_path(mem).read_text(encoding="utf-8") == before)

    # 被 ``` 包裹：**合法** JSON 照样认（模型最爱这么回），包着坏东西才算失败
    good = {"people": [{"uid": "123", "who": ["是学生"], "us": ["习惯互道晚安"],
                        "event": ["9月16日交作业"]}]}
    fenced = "```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
    res, _store, _llm = _run(mem, fenced)
    check("代码块里的合法 JSON 能解析", res.get("status") == "ok", str(res))

    fenced_bad = "```json\n{\"people\": 坏\n```"
    res2, store2, _ = _run(mem, fenced_bad)
    check("代码块里是坏东西 → 失败", res2.get("status") == "error", str(res2))
    check("失败时不消耗素材", store2.marked == [], str(store2.marked))


# ---------------------------------------------------------------- 6 旧扁平格式


def test_legacy_flat_format() -> None:
    print("\n[6] 兼容旧的 {\"facts\": [...]} 扁平格式：落到未分类，不硬塞进事件桶")
    reset_guard()
    mem = tmp_mem(buckets_enabled=True)
    payload = {
        "people": [
            {"uid": "123", "nick": "小明", "facts": ["喜欢摇滚", "在杭州读大三"]}
        ],
        "group": ["群里爱聊音乐"],
    }
    res, _store, _llm = _run(mem, json.dumps(payload, ensure_ascii=False))
    check("旧格式也算成功", res.get("status") == "ok", str(res))
    person = mm.load_person(mem, 1, "123")
    sections, _rest = mm.parse_body(person.body)
    check("内容一条不丢",
          sections.get(mm.UNSORTED) == ["喜欢摇滚", "在杭州读大三"], str(sections))
    check("没被塞进事件桶（没有时间点的不当事件）",
          not sections.get("event"), str(sections.get("event")))
    check("旧扁平格式不占长期配额", res.get("facts") == 0, str(res))

    # 顶层直接给桶（没有 people 数组）也认：当成单个人的档案
    flat_top = {"uid": "123", "who": [{"c": "是学生"}], "recent_new": []}
    res2, _store2, _ = _run(mem, json.dumps(flat_top, ensure_ascii=False))
    check("顶层直接给桶能解析", res2.get("status") == "ok", str(res2))


# ---------------------------------------------------------------- 7 并发单例


def test_single_flight() -> None:
    print("\n[7] 同群单例：整理中再进来一次直接被挡住")
    reset_guard()
    mem = tmp_mem(buckets_enabled=True)
    before = seed(mem, {"who": ["是学生"], "us": ["习惯互道晚安"], "event": ["9月16日交作业"]})
    payload = json.dumps({
        "people": [{"uid": "123", "who": ["是学生"], "us": ["习惯互道晚安"],
                    "event": ["9月16日交作业"]}]
    }, ensure_ascii=False)

    check("第一次打标记成功", dg.begin_digest(1) is True)
    check("标记可见", dg.is_digesting(1) is True)
    res, store, llm = _run(mem, payload)
    check("第二次被判 skipped", res.get("status") == "skipped", str(res))
    check("说明里写清「正在整理」", "整理" in (res.get("why") or ""), str(res.get("why")))
    check("被挡住时根本没调模型", llm.calls == 0, str(llm.calls))
    check("被挡住时库不变", person_path(mem).read_text(encoding="utf-8") == before)
    check("被挡住时也不消耗素材", store.marked == [], str(store.marked))

    dg.end_digest(1)
    check("标记已清", dg.is_digesting(1) is False)
    res2, _store2, llm2 = _run(mem, payload)
    check("放开后能正常整理", res2.get("status") == "ok", str(res2))
    check("这次调了一次模型", llm2.calls == 1, str(llm2.calls))
    check("整理完标记自动清掉", dg.is_digesting(1) is False)


# ---------------------------------------------------------------- 8 关掉时行为不变


def test_off_is_legacy() -> None:
    print("\n[8] buckets_enabled=False：仍走老的扁平流程（不写小节、原文追加）")
    reset_guard()
    mem = tmp_mem()  # buckets_enabled 默认 False
    payload = {
        "people": [{"uid": "123", "nick": "小明", "facts": ["喜欢摇滚"]}],
        "group": ["群里爱聊音乐"],
    }
    res, _store, _llm = _run(mem, json.dumps(payload, ensure_ascii=False))
    check("老流程状态 ok", res.get("status") == "ok", str(res))
    check("写了 1 条事实", res.get("facts") == 1, str(res))
    body = mm.load_person(mem, 1, "123").body
    check("正文还是老样子（没有 ## 小节）", "## " not in body, body)
    check("正文以列表项开头", body.startswith("- "), body)


def main() -> None:
    test_legacy_body()
    test_roundtrip()
    test_digest_quota()
    test_cover_guard()
    test_bad_reply()
    test_legacy_flat_format()
    test_single_flight()
    test_off_is_legacy()
    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 52)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

"""LLM 调用记录（``llmtrace``）：归档、量纲、过滤。

这里是纯逻辑，不发请求也不碰网络；「chat() 有没有真的写进来」在
``test_llm_bot.py`` 的「LLM 调用记录接线」段里验。

运行: MUSIC_DATA_DIR=$(mktemp -d) python tests/test_llmtrace.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

# 导入 music_collector 的任何子模块都会执行包的 __init__，必须先初始化
nonebot.init(driver="~fastapi")

from music_collector.config import LLMConfig  # noqa: E402
from music_collector.llmtrace import MAX_FIELD, MAX_STEPS, TraceBuffer  # noqa: E402

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


def test_basic() -> None:
    section("1. 归档：成功和失败都要留下")
    buf = TraceBuffer(keep=10)
    cfg = LLMConfig(model="m1", thinking="off", tools=True)

    e = buf.start(100, 7, "张三", cfg, question="群友问了啥")
    check("start 记下提问", e["question"] == "群友问了啥", str(e["question"]))
    check("没传背景时为空", e["background"] == "", str(e["background"]))
    check("默认 kind=chat", e["kind"] == "chat", str(e["kind"]))
    check("start 记下模型", e["model"] == "m1", e["model"])
    check("start 记下工具开关", e["tools_on"] is True)
    check("start 阶段还没归档", buf.stats()["size"] == 0)

    # 判官预检也要留痕，否则「开了主动搭话却没动静」只能靠猜
    judge = buf.start(100, 0, "判官", cfg, question="一段群聊", kind="judge")
    check("可以标成判官记录", judge["kind"] == "judge", str(judge["kind"]))

    buf.step(e, "第一步")
    buf.step(e, "第二步")
    e["system_chars"] = 123
    buf.finish(e, reply="好的", elapsed_ms=1234.6)

    st = buf.stats()
    check("finish 后归档 1 条", st["size"] == 1, str(st))
    check("成功计数 +1", st["succeeded"] == 1, str(st))
    got = buf.snapshot(limit=10)[0]
    check("回复存下来了", got["reply"] == "好的", got["reply"])
    check("步骤按顺序保留", got["steps"] == ["第一步", "第二步"], str(got["steps"]))
    check("system 字数保留", got["system_chars"] == 123)
    check("耗时取整", got["elapsed_ms"] == 1234, str(got["elapsed_ms"]))
    check("ok 为真", got["ok"] is True)

    e2 = buf.start(100, 8, "李四", cfg, question="第二问")
    buf.finish(e2, reply="", error="error: 502", elapsed_ms=10)
    check("失败也归档（不吞）", buf.stats()["size"] == 2, str(buf.stats()))
    bad = buf.snapshot(limit=1)[0]
    check("失败标记 ok=False", bad["ok"] is False)
    check("错误原因留下来", "502" in bad["error"], bad["error"])
    check("失败计数 +1", buf.stats()["failed"] == 1, str(buf.stats()))

    # 「有半截回复但同时报错」算失败：宁可标红让人去查
    e3 = buf.start(100, 9, "王五", cfg, question="第三问")
    buf.finish(e3, reply="半截", error="error: 超时")
    check("带错误的半截回复算失败", buf.snapshot(limit=1)[0]["ok"] is False)


def test_background() -> None:
    section("1b. 群聊背景：单独存一份、超长截断、能搜到")
    buf = TraceBuffer(keep=10)
    cfg = LLMConfig(model="m1")
    bg = (
        "【群里刚才在聊的，不是在跟你说话，判断语境用】\n"
        "[21:53] 张三(1)：锅贴真不错\n[21:54] 李四(2)：我连吃三天"
    )
    e = buf.start(100, 7, "张三", cfg, question="他们说的啥", background=bg)
    check("背景原样存下", e["background"] == bg, str(e["background"])[:60])
    buf.finish(e, reply="说锅贴")
    got = buf.snapshot(limit=1)[0]
    check("归档后仍能看见背景", "锅贴真不错" in got["background"], got["background"][:60])
    check("关键词能搜到背景里的内容",
          len(buf.snapshot(query="连吃三天")) == 1, str(len(buf.snapshot(query="连吃三天"))))

    e2 = buf.start(100, 7, "张三", cfg, question="长背景", background="x" * (MAX_FIELD + 500))
    check("背景超长按上限截断", len(e2["background"]) == MAX_FIELD, str(len(e2["background"])))


def test_limits() -> None:
    section("2. 量纲：长字段截断、步骤上限、容量上限")
    buf = TraceBuffer(keep=10)
    cfg = LLMConfig(model="m")

    e = buf.start(1, 1, "甲", cfg, question="问" * (MAX_FIELD + 500))
    check("提问截到 MAX_FIELD", len(e["question"]) == MAX_FIELD, str(len(e["question"])))
    buf.finish(e, reply="答" * (MAX_FIELD + 500))
    check("回复截到 MAX_FIELD", len(buf.snapshot(limit=1)[0]["reply"]) == MAX_FIELD)

    e2 = buf.start(1, 1, "乙", cfg, question="x")
    for i in range(60):
        buf.step(e2, f"步骤{i}")
    check("步骤有上限", len(e2["steps"]) <= MAX_STEPS + 1, str(len(e2["steps"])))
    check("超限后留一条省略提示", "省略" in e2["steps"][-1], e2["steps"][-1])
    check("步骤也能对着 None 安全调用", (buf.step(None, "x") is None))

    small = TraceBuffer(keep=10)
    for i in range(25):
        ex = small.start(1, i, "丙", cfg, question=f"第{i}问")
        small.finish(ex, reply="ok")
    check("超出容量丢最旧的", small.stats()["size"] == 10, str(small.stats()))
    check("留下的是最新的", small.snapshot(limit=1)[0]["question"] == "第24问")
    check("进程累计不受容量影响", small.stats()["total"] == 25, str(small.stats()))
    check("容量对外可见", small.stats()["capacity"] == 10, str(small.stats()))


def test_query() -> None:
    section("3. 过滤：关键词 / 群号 / 只看失败 / 条数")
    buf = TraceBuffer(keep=100)
    cfg = LLMConfig(model="m")

    def add(gid: int, who: str, q: str, reply: str, err: str = "", steps=()) -> None:
        e = buf.start(gid, 1, who, cfg, question=q)
        for s in steps:
            buf.step(e, s)
        buf.finish(e, reply=reply, error=err)

    add(100, "张三", "推荐首歌", "试试这首")
    add(200, "李四", "帮我查天气", "", err="error: 超时")
    add(100, "王五", "这段代码怎么改", "改这里", steps=("🔧 调 fetch_url",))

    check("默认新的在前", buf.snapshot(limit=1)[0]["speaker"] == "王五")
    check("不传 limit 全给", len(buf.snapshot()) == 3, str(len(buf.snapshot())))
    check("按群号过滤", len(buf.snapshot(group_id=100)) == 2, str(len(buf.snapshot(group_id=100))))
    check("只看失败", len(buf.snapshot(only_failed=True)) == 1)
    check("问句关键词命中", len(buf.snapshot(query="天气")) == 1)
    check("回复关键词命中", len(buf.snapshot(query="试试这首")) == 1)
    check("错误信息能搜", len(buf.snapshot(query="超时")) == 1)
    check("步骤内容能搜", len(buf.snapshot(query="fetch_url")) == 1)
    check("昵称能搜", len(buf.snapshot(query="李四")) == 1)
    check("搜不到就是空", buf.snapshot(query="绝不存在的词") == [])
    check("limit 截断", len(buf.snapshot(limit=2)) == 2)

    check("clear 返回清掉的条数", buf.clear() == 3)
    check("clear 后为空", buf.snapshot() == [] and buf.stats()["size"] == 0)


def main() -> None:
    test_basic()
    test_background()
    test_limits()
    test_query()
    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

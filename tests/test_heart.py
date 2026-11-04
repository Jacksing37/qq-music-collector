"""心事：素材门槛、超长截断、失败保留旧版、注入顺序、关掉时逐字不变。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_heart.py

**绝不碰真实 data/**：``MUSIC_DATA_DIR`` 指到临时目录（心事落 ``data/persona/<群号>/``），
``MemoryConfig.dir`` 也指到临时目录（和 test_memory.py 同一套做法）。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="heart-data-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import heart as ht  # noqa: E402
from music_collector import memory as mm  # noqa: E402
from music_collector import style as style_mod  # noqa: E402
from music_collector.config import (  # noqa: E402
    AppConfig,
    LLMConfig,
    MemoryConfig,
    PersonaConfig,
)

PASSED = 0
FAILED = 0

GID = 9001


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  →  {detail}" if detail else ""))


# ---------------------------------------------------------------- 桩


class _FakeStore:
    def __init__(self, listen=None, chat=None):
        self.listen = list(listen or [])
        self.chat = list(chat or [])

    async def recent_chat(self, gid, **kw):
        return list(self.listen)

    async def load_chat(self, gid, **kw):
        return list(self.chat)


class _FakeService:
    def __init__(self, app, store):
        self.store = store
        self._app = app

    def cfg(self, gid):
        return self._app


class _FakeLLM:
    """记下被调用的次数与最后一次的 prompt——「不许调模型」「延续不是重来」都靠它断言。"""

    def __init__(self, text="", err=""):
        self.text = text
        self.err = err
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, prompt, *a, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        return self.text, self.err


def dialog(n: int) -> list[dict]:
    return [
        {"id": i + 1, "user_id": 100 + i, "nick": f"群友{i}", "content": f"第{i}句话"}
        for i in range(n)
    ]


def app_for(mem: MemoryConfig, pcfg: PersonaConfig) -> AppConfig:
    return AppConfig(
        llm=LLMConfig(
            enabled=True, base_url="http://x/v1", api_key="k", system_prompt="你是一只猫。"
        ),
        memory=mem,
        persona=pcfg,
    )


def tmp_mem(**kw) -> MemoryConfig:
    return MemoryConfig(dir=tempfile.mkdtemp(prefix="heart-mem-"), **kw)


def run_update(mem, pcfg, *, reply="", err="", rows=12):
    """跑一次 heart.update，返回 (返回文本, 状态, llm 桩)。"""
    store = _FakeStore(listen=dialog(rows))
    llm = _FakeLLM(reply, err)
    old_service, old_llm = ht.service, ht.llm_bot
    ht.service, ht.llm_bot = _FakeService(app_for(mem, pcfg), store), llm
    status: dict = {}
    try:
        text = asyncio.run(ht.update(GID, status=status))
        return text, status, llm
    finally:
        ht.service, ht.llm_bot = old_service, old_llm


def seed_heart(text: str) -> None:
    ok, msg = ht.write(GID, text, source_rows=3, chars=400)
    assert ok, msg


# ---------------------------------------------------------------- 1 素材不足


def test_need_more_source() -> None:
    print("\n[1] 素材不足：不调用模型（桩计数为 0），旧心事保留")
    mem = tmp_mem()
    pcfg = PersonaConfig(heart_enabled=True, heart_min_source=10)
    seed_heart("旧心事：我好像总是那个在深夜被叫醒的家伙。")

    text, status, llm = run_update(mem, pcfg, reply="新写的", rows=3)
    check("返回空串", text == "", repr(text))
    check("没调用模型", llm.calls == 0, str(llm.calls))
    check("状态是 skipped", status.get("status") == "skipped", str(status))
    check("旧心事原样保留", ht.read(GID).startswith("旧心事"), ht.read(GID))

    # 刚好够门槛就该调模型
    text2, status2, llm2 = run_update(mem, pcfg, reply="新的心事，接着上一版往下长的一点点。", rows=10)
    check("够门槛就调模型", llm2.calls == 1, str(llm2.calls))
    check("写入了新心事", ht.read(GID).startswith("新的心事"), ht.read(GID))
    check("状态 ok", status2.get("status") == "ok", str(status2))


# ---------------------------------------------------------------- 2 超长截断


def test_truncate() -> None:
    print("\n[2] 超字数：截断而不是报错")
    mem = tmp_mem()
    pcfg = PersonaConfig(heart_enabled=True, heart_chars=120, heart_min_source=3)
    long_text = "我在这个群里是个总被随手叫醒的家伙。" * 30
    text, status, llm = run_update(mem, pcfg, reply=long_text, rows=20)
    check("调用了一次模型", llm.calls == 1, str(llm.calls))
    check("状态 ok（没报错）", status.get("status") == "ok", str(status))
    check("落盘被截到上限附近", len(ht.read(GID)) <= 121, str(len(ht.read(GID))))
    check("截断处带省略号", ht.read(GID).endswith("…"), ht.read(GID)[-10:])
    check("返回的也是截断后的正文", text == ht.read(GID), f"{len(text)} vs {len(ht.read(GID))}")

    # render 也按预算截断（心事是每轮注入的固定成本）
    old_service = ht.service
    ht.service = _FakeService(app_for(mem, pcfg), _FakeStore())
    try:
        body = ht.render(GID, mem=mem)
    finally:
        ht.service = old_service
    check("render 带固定前缀", body.strip().startswith("【你心里的事】"), body[:20])
    check("render 不超过预算 + 前后缀", len(body) <= 121 + 60, str(len(body)))


# ---------------------------------------------------------------- 3 失败保留


def test_failure_keeps_old() -> None:
    print("\n[3] 模型返回空 / 异常 / 太短：旧心事保留")
    mem = tmp_mem()
    pcfg = PersonaConfig(heart_enabled=True, heart_min_source=3)
    seed_heart("旧心事：我知道自己只是这个群里路过的那一个。")
    before = ht.read(GID)

    for reply, err in (("", "boom"), ("", ""), ("嗯", "")):
        text, status, llm = run_update(mem, pcfg, reply=reply, err=err, rows=20)
        check(f"返回空串（reply={reply!r}）", text == "", repr(text))
        check(f"状态 error（reply={reply!r}）", status.get("status") == "error", str(status))
        check(f"旧心事还在（reply={reply!r}）", ht.read(GID) == before, ht.read(GID)[:40])


# ---------------------------------------------------------------- 4 注入顺序


def test_inject_order() -> None:
    print("\n[4] 注入顺序：群概况 → 风格卡 → 心事 → 成员档案")
    mem = tmp_mem()
    pcfg = PersonaConfig(heart_enabled=True, heart_min_source=3)
    mm.write_group_note(mem, GID, "本群黑话：yyds=永远的神")
    ok, msg = style_mod.write_style(mem, GID, "- 句长：短\n- 标点：爱用。。。")
    check("先有风格卡", ok, msg)
    person = mm.Person(uid="123", nick="小明", last_seen="2026-11-05 09:00")
    person.body = mm.join_body({"who": ["是学生，住寝室"]})
    mm.save_person(mem, GID, person)
    seed_heart("我心里的事：这个群里的人喜欢在半夜说话。")

    old_service = ht.service
    ht.service = _FakeService(app_for(mem, pcfg), _FakeStore())
    try:
        text = mm.render_for_prompt(mem, GID)
    finally:
        ht.service = old_service

    for tag in ("【群概况】", "【本群说话习惯】", "【你心里的事】", "【群成员档案】"):
        check(f"带 {tag}", tag in text, text[:200])
    check("心事在风格卡之后", text.index("【本群说话习惯】") < text.index("【你心里的事】"), text[:200])
    check("心事在成员档案之前", text.index("【你心里的事】") < text.index("【群成员档案】"), text[:200])
    check("心事正文进来了", "半夜说话" in text, text[:300])


# ---------------------------------------------------------------- 5 关掉时逐字不变


def test_off_is_identical() -> None:
    print("\n[5] heart_enabled=False：不注入，输出与不开时逐字相同")
    mem = tmp_mem()
    mm.write_group_note(mem, GID, "本群黑话：awsl")
    person = mm.Person(uid="123", nick="小明", last_seen="2026-11-05 09:00")
    person.body = mm.join_body({"who": ["是学生"]})
    mm.save_person(mem, GID, person)
    seed_heart("我心里的事：这段不该出现在提示词里。")

    on = PersonaConfig(heart_enabled=True, heart_min_source=3)
    off = PersonaConfig(heart_enabled=False)

    old_service = ht.service
    try:
        ht.service = _FakeService(app_for(mem, on), _FakeStore())
        with_heart = mm.render_for_prompt(mem, GID)
        ht.service = _FakeService(app_for(mem, off), _FakeStore())
        without = mm.render_for_prompt(mem, GID)
        # 把「有没有心事文件」这个变量消掉：空文件 + 关掉 == 有文件 + 关掉
        ht.heart_path(GID).unlink()
        ht.service = _FakeService(app_for(mem, off), _FakeStore())
        baseline = mm.render_for_prompt(mem, GID)
    finally:
        ht.service = old_service

    check("打开时确实注入了", "这段不该出现在提示词里" in with_heart, with_heart[:200])
    check("关掉时不含心事", "【你心里的事】" not in without, without[:200])
    check("关掉时与「根本没有心事文件」逐字相同", without == baseline, f"{without!r} vs {baseline!r}")


# ---------------------------------------------------------------- 6 延续不是重来


def test_prompt_carries_old_heart() -> None:
    print("\n[6] 提示词：带上旧心事 / 强调「延续不是重来」/ 群场景语义")
    mem = tmp_mem()
    pcfg = PersonaConfig(heart_enabled=True, heart_min_source=3)
    seed_heart("旧心事：我在这个群里是那个被随手叫醒的家伙。")
    person = mm.Person(uid="123", nick="小明")
    person.body = mm.join_body({"who": ["是学生"], "us": ["习惯互道晚安"]})
    mm.save_person(mem, GID, person)

    _text, _status, llm = run_update(mem, pcfg, reply="新的心事，仍然是被随手叫醒的那个。", rows=20)
    prompt = llm.prompts[-1] if llm.prompts else ""
    check("提示词带旧心事原文", "被随手叫醒的家伙" in prompt, prompt[:200])
    check("提示词写明了「延续不是重来」", "延续不是重来" in prompt)
    check("提示词写明「我在这个群里是谁」", "我在这个群里是谁" in prompt)
    check("提示词禁止复述事实清单", "不许复述事实清单" in prompt)
    check("提示词带上了人设", "你是一只猫" in prompt, prompt[:300])
    check("提示词带上了成员事实", "习惯互道晚安" in prompt, prompt[-400:])
    check("提示词带上了最近对话", "第0句话" in prompt, prompt[-400:])
    check("字数上限写进提示词", str(int(pcfg.heart_chars)) in prompt)


def main() -> None:
    test_need_more_source()
    test_truncate()
    test_failure_keeps_old()
    test_inject_order()
    test_off_is_identical()
    test_prompt_carries_old_heart()
    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 52)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

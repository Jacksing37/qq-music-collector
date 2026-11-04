"""记忆相关度检索：分词、三项打分、按当前这句话挑人、无命中回退、预算截断、异常兜底。

运行: PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe tests/test_recall.py

**绝不碰真实 data/**：``MemoryConfig.dir`` 一律指向临时目录，``MUSIC_DATA_DIR``
也指到临时目录（和 test_memory.py 同一套做法）。
"""

import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
os.environ.setdefault("MUSIC_DATA_DIR", tempfile.mkdtemp(prefix="recall-data-"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import memory as memory_mod  # noqa: E402
from music_collector.config import MemoryConfig  # noqa: E402

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


def new_dir() -> str:
    """一个全新的空目录（同一用例里想对比新旧行为时，让两个配置指向同一个）。"""
    return tempfile.mkdtemp(prefix="recall-")


def tmp_mem(**kw) -> MemoryConfig:
    return MemoryConfig(dir=new_dir(), **kw)


def seed(mem: MemoryConfig, gid: int, uid: str, nick: str, notes: list[str],
         last_seen: str = "2026-11-05 09:00") -> None:
    """直接建档，不等 append_note。

    ``append_note`` 会把 ``last_seen`` 刷成「现在」，而新鲜度断言需要可控的时间，
    所以这里先写好 frontmatter 再落盘（走的是同一个 save_person）。
    """
    person = memory_mod.Person(uid=uid, nick=nick, last_seen=last_seen)
    person.body = "\n".join(f"- {n}" for n in notes)
    memory_mod.save_person(mem, gid, person)


def uids_in(text: str) -> set[str]:
    """从注入块的行里抠出 QQ 号（行格式 ``- 昵称(123)：摘要``）。"""
    return set(re.findall(r"^\- .*?\((\d+)\)：", text, re.M))


# ---------------------------------------------------------------- 分词


def test_tokenize() -> None:
    print("\n[1] 分词：中文二元组 / 英文整词 / 数字 / 混排 / 空串 / 纯标点")
    tk = memory_mod.tokenize
    got = tk("我昨天加班到十点")
    check("中文二元组",
          got == {"我昨", "昨天", "天加", "加班", "班到", "到十", "十点"}, str(sorted(got)))
    check("两字词就是一个二元组", tk("加班") == {"加班"}, str(sorted(tk("加班"))))
    check("单字保留", tk("猫") == {"猫"}, str(sorted(tk("猫"))))
    check("英文整词（转小写）", tk("Jay CHOU") == {"jay", "chou"}, str(sorted(tk("Jay CHOU"))))
    check("数字整词", tk("2002") == {"2002"}, str(sorted(tk("2002"))))
    mixed = tk("周杰伦Jay")
    check("混排：中文切二元组、英文单独成词",
          {"周杰", "杰伦", "jay"} <= mixed, str(sorted(mixed)))
    check("空串", tk("") == set(), str(sorted(tk(""))))
    check("纯标点", tk("，。！？…… 　") == set(), str(sorted(tk("，。！？…… 　"))))
    check("标点两侧各成一段", tk("周杰伦，Jay") == {"周杰", "杰伦", "jay"},
          str(sorted(tk("周杰伦，Jay"))))


# ---------------------------------------------------------------- 关闭 / 空 query


def test_off_is_identical() -> None:
    print("\n[2] 关掉或 query 为空：注入块与改造前逐字相同")
    root = new_dir()
    off = MemoryConfig(dir=root)                  # recall_enabled 默认 False
    on = MemoryConfig(dir=root, recall_enabled=True)
    gid = 8001
    memory_mod.append_group_note(off, gid, ["群里都听华语流行"])
    seed(off, gid, "21", "小周", ["喜欢周杰伦，常点《七里香》"], "2026-11-04 10:00")
    seed(off, gid, "22", "老王", ["在杭州读大三"], "2026-11-03 10:00")
    seed(off, gid, "23", "阿强", ["养了一只猫叫豆豆"], "2026-11-02 10:00")

    base = memory_mod.render_for_prompt(off, gid)
    check("基线非空（否则后面的等价断言没意义）",
          bool(base) and "【群成员档案】" in base, base[:80])
    check("关掉时传 query 也不变",
          memory_mod.render_for_prompt(off, gid, query="周杰伦哪年的") == base)
    check("不传与传空串相同", memory_mod.render_for_prompt(off, gid, query="") == base)
    check("开着但 query 为空 → 仍是旧输出",
          memory_mod.render_for_prompt(on, gid) == base)
    check("开着但 query 全空白 → 仍是旧输出",
          memory_mod.render_for_prompt(on, gid, query="   \n ") == base)
    check("打开检索不影响群概况",
          "【群概况】" in base and "【群概况】" in memory_mod.render_for_prompt(on, gid, query="周杰伦"))


# ---------------------------------------------------------------- 相关度筛选


def test_only_relevant_people() -> None:
    print("\n[3] query=周杰伦：只挑档案里提到周杰伦的人")
    mem = tmp_mem(recall_enabled=True, recall_top=12, recall_min=1, recall_chars=4000)
    gid = 8002
    seed(mem, gid, "31", "小周", ["喜欢周杰伦，常点《七里香》"], "2026-11-01 10:00")
    seed(mem, gid, "32", "老王", ["在杭州读大三，喜欢打羽毛球"], "2026-11-04 10:00")
    seed(mem, gid, "33", "阿强", ["养了一只猫，叫豆豆"], "2026-11-05 10:00")
    seed(mem, gid, "34", "小美", ["最近在学吉他，弹得还很生"], "2026-11-05 11:00")

    picked = memory_mod.pick_people(mem, gid, "周杰伦哪年的", top=12, min_n=1, chars=4000)
    check("pick_people 只返回命中的人",
          [p.uid for p in picked] == ["31"], str([p.uid for p in picked]))

    text = memory_mod.render_for_prompt(mem, gid, query="周杰伦哪年的")
    check("注入块里有小周", "小周(31)" in text, text)
    for absent in ("老王(32)", "阿强(33)", "小美(34)"):
        check(f"注入块里没有 {absent}", absent not in text, text)
    check("标题说明是按相关度挑的", "按与这句话的相关度挑选" in text, text[:100])
    check("末尾交代还有 3 人没进来", "还有 3 人" in text, text[-120:])

    # 命中太少就按分数补齐：只挑 1 个人时模型容易把「一个人」当「全群」
    filled = memory_mod.pick_people(mem, gid, "周杰伦哪年的", top=12, min_n=3, chars=4000)
    check("不足 min_n 时按分数补齐", len(filled) == 3, str([p.uid for p in filled]))
    check("命中的仍然排第一", filled[0].uid == "31", str([p.uid for p in filled]))
    capped = memory_mod.pick_people(mem, gid, "周杰伦哪年的", top=1, min_n=4, chars=4000)
    check("min_n 超过 top 时以 top 为准", [p.uid for p in capped] == ["31"],
          str([p.uid for p in capped]))

    # 只念名字、正文里没有名字，也要挑得出来
    named = memory_mod.pick_people(mem, gid, "阿强呢", top=12, min_n=1, chars=4000)
    check("只念昵称也能命中", [p.uid for p in named] == ["33"], str([p.uid for p in named]))


def test_subset_of_old_behaviour() -> None:
    print("\n[4] 子集断言：注入的人不许凭空多出来")
    root = new_dir()
    off = MemoryConfig(dir=root)
    on = MemoryConfig(dir=root, recall_enabled=True, recall_top=3, recall_min=1, recall_chars=1200)
    gid = 8003
    seed(off, gid, "41", "小周", ["喜欢周杰伦的早期专辑"], "2026-11-06 10:00")
    seed(off, gid, "42", "老王", ["在杭州读大三"], "2026-11-05 10:00")
    seed(off, gid, "43", "阿强", ["养了一只猫叫豆豆"], "2026-11-04 10:00")
    seed(off, gid, "44", "小美", ["最近在学吉他"], "2026-11-03 10:00")
    seed(off, gid, "45", "大刘", ["每天骑行二十公里"], "2026-11-02 10:00")
    seed(off, gid, "46", "小赵", ["爱吃辣不吃香菜"], "2026-11-01 10:00")

    old_text = memory_mod.render_for_prompt(off, gid)
    new_text = memory_mod.render_for_prompt(on, gid, query="周杰伦哪年的")
    old_uids, new_uids = uids_in(old_text), uids_in(new_text)
    check("旧行为把 6 人都装进来了", len(old_uids) == 6, str(sorted(old_uids)))
    check("新行为确实挑了人", bool(new_uids), str(new_uids))
    check("新注入的人是旧行为的子集", new_uids <= old_uids, str(new_uids - old_uids))
    check("不超过 recall_top", len(new_uids) <= 3, str(sorted(new_uids)))


def test_no_hit_falls_back() -> None:
    print("\n[5] 全员无关 → 完全退化为按最近活跃（旧行为）")
    root = new_dir()
    off = MemoryConfig(dir=root)
    on = MemoryConfig(dir=root, recall_enabled=True, recall_top=2, recall_min=1, recall_chars=200)
    gid = 8004
    seed(off, gid, "51", "小周", ["喜欢周杰伦的早期专辑"], "2026-11-06 10:00")
    seed(off, gid, "52", "老王", ["在杭州读大三"], "2026-11-05 10:00")
    seed(off, gid, "53", "阿强", ["养了一只猫叫豆豆"], "2026-11-04 10:00")
    seed(off, gid, "54", "小美", ["最近在学吉他"], "2026-11-03 10:00")
    seed(off, gid, "55", "大刘", ["每天骑行二十公里"], "2026-11-02 10:00")
    seed(off, gid, "56", "小赵", ["爱吃辣不吃香菜"], "2026-11-01 10:00")

    old_text = memory_mod.render_for_prompt(off, gid)
    new_text = memory_mod.render_for_prompt(on, gid, query="量子力学和炒股的联系")
    check("逐字相同（recall_chars 也一并失效）", new_text == old_text,
          f"{len(new_text)} vs {len(old_text)}")
    fallback = memory_mod.pick_people(on, gid, "量子力学和炒股的联系",
                                     top=2, min_n=1, chars=200)
    check("pick_people 也退回全量、且保持活跃倒序",
          [p.uid for p in fallback] == ["51", "52", "53", "54", "55", "56"],
          str([p.uid for p in fallback]))


# ---------------------------------------------------------------- 预算与兜底


def test_chars_truncation() -> None:
    print("\n[6] 超 recall_chars → 截断，且「还有 X 人」计数正确")
    mem = tmp_mem(recall_enabled=True, recall_top=10, recall_min=1,
                  recall_chars=300, inject_chars=60000)
    gid = 8005
    for i in range(5):
        seed(mem, gid, str(61 + i), f"周迷{i}",
             [f"喜欢周杰伦的第{i}个理由" + "、细节" * 33], "2026-11-05 10:00")

    text = memory_mod.render_for_prompt(mem, gid, query="周杰伦哪年的")
    shown = text.count("\n- ")
    check("被截断（少于 5 人）", 0 < shown < 5, str(shown))
    check("还有 X 人计数正确", f"还有 {5 - shown} 人" in text, text[-140:])
    check("装进来的都提到周杰伦", "周迷" in text)
    check("总长相对于 recall_chars 没有失控", len(text) <= 300 + 200, str(len(text)))


def test_broken_archive_and_exception() -> None:
    print("\n[7] 档案坏掉 / 打分抛异常：不泄漏异常，退化为旧行为")
    root = new_dir()
    off = MemoryConfig(dir=root)
    on = MemoryConfig(dir=root, recall_enabled=True, recall_top=4, recall_min=1,
                      recall_chars=1200)
    gid = 8006
    seed(off, gid, "71", "小周", ["喜欢周杰伦，常点《七里香》"], "2026-11-05 10:00")
    seed(off, gid, "72", "老王", ["在杭州读大三"], "2026-11-04 10:00")
    # 坏 JSON：frontmatter 花括号半截、后面又跟一段废文本
    bad = memory_mod._path_for(off, gid, "73")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{"uid": "73", "nick": "坏档案"\n{"oops": \n', encoding="utf-8")

    ok, text = True, ""
    try:
        text = memory_mod.render_for_prompt(on, gid, query="周杰伦哪年的")
    except Exception as exc:  # noqa: BLE001 — 抛了就是这条用例要抓的 bug
        ok, text = False, repr(exc)
    check("坏 JSON 不抛异常", ok, str(text)[:120])
    check("坏掉之后仍然有注入块", isinstance(text, str) and "【群成员档案】" in text,
          str(text)[:120])

    old_text = memory_mod.render_for_prompt(off, gid)
    orig = memory_mod._pick_detail

    def boom(*args, **kwargs):
        raise RuntimeError("打分炸了")

    memory_mod._pick_detail = boom
    try:
        degraded = memory_mod.render_for_prompt(on, gid, query="周杰伦哪年的")
    finally:
        memory_mod._pick_detail = orig
    check("打分抛异常时逐字退回旧行为", degraded == old_text,
          f"{len(degraded)} vs {len(old_text)}")
    restored = memory_mod.render_for_prompt(on, gid, query="周杰伦哪年的")
    check("恢复后仍能按相关度挑", "小周(71)" in restored and "老王(72)" not in restored,
          restored)


# ---------------------------------------------------------------- 打分本身


def test_score_person() -> None:
    print("\n[8] 打分：相关度 / 新鲜度衰减 / 姓名加权 / 重要度常量")
    now = datetime(2026, 11, 5, 12, 0)
    tokens = memory_mod.tokenize("周杰伦")

    person = memory_mod.Person(uid="1", nick="小明", last_seen="2026-11-05 09:00")
    person.body = "- 喜欢周杰伦"
    fresh = memory_mod.score_person(person, tokens, now)
    check("今天：0.5(相关) + 0.15(重要) + 0.2(新鲜) ≈ 0.85", abs(fresh - 0.85) < 0.01,
          str(fresh))

    person.last_seen = "2026-10-06 09:00"      # 30 天前
    aged = memory_mod.score_person(person, tokens, now)
    check("30 天前：新鲜度减半 → ≈0.75", abs(aged - 0.75) < 0.01, str(aged))

    person.last_seen = ""
    check("没有时间字段给 0.5 新鲜度（同样 ≈0.75）",
          abs(memory_mod.score_person(person, tokens, now) - 0.75) < 0.01)

    person.last_seen = "2026-11-05 09:00"
    unrelated = memory_mod.score_person(person, memory_mod.tokenize("量子力学"), now)
    check("无关：0 + 0.15 + 0.2 = 0.35", abs(unrelated - 0.35) < 0.01, str(unrelated))

    named = memory_mod.Person(uid="2", nick="小明", last_seen="2026-11-05 09:00")
    named.body = "- 在杭州读大三"
    by_name = memory_mod.score_person(named, memory_mod.tokenize("小明"), now)
    check("只念昵称也能拿到满相关度（×1.4 后再封顶）", by_name > 0.8, str(by_name))

    chatty = memory_mod.Person(uid="3", nick="话痨", msgs=9999, last_seen="2026-11-05 09:00")
    chatty.body = "- 在杭州读大三"
    quiet = memory_mod.Person(uid="4", nick="潜水", msgs=0, last_seen="2026-11-05 09:00")
    quiet.body = "- 在杭州读大三"
    hangzhou = memory_mod.tokenize("杭州")
    check("重要度不吃 msgs（话痨与潜水同分）",
          abs(memory_mod.score_person(chatty, hangzhou, now)
              - memory_mod.score_person(quiet, hangzhou, now)) < 1e-9)


def test_config_defaults() -> None:
    print("\n[9] 配置：默认关 + 四项默认值 + 越界拒绝")
    mem = MemoryConfig()
    check("默认关（关掉 = 旧行为）", mem.recall_enabled is False)
    check("默认 top=12", mem.recall_top == 12, str(mem.recall_top))
    check("默认 min=6", mem.recall_min == 6, str(mem.recall_min))
    check("默认 chars=1200", mem.recall_chars == 1200, str(mem.recall_chars))
    for bad in ({"recall_top": 0}, {"recall_min": 0}, {"recall_chars": 10}):
        try:
            MemoryConfig(**bad)
            check(f"拒绝 {bad}", False, "没报错")
        except Exception:
            check(f"拒绝 {bad}", True)


def main() -> None:
    test_tokenize()
    test_off_is_identical()
    test_only_relevant_people()
    test_subset_of_old_behaviour()
    test_no_hit_falls_back()
    test_chars_truncation()
    test_broken_archive_and_exception()
    test_score_person()
    test_config_defaults()
    print("\n" + "=" * 52)
    print(f"通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 52)
    if FAILED:
        sys.exit(1)
    print("recall tests OK")


if __name__ == "__main__":
    main()

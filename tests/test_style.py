"""风格卡：采样过滤、每人配额、注入预算、失败不覆盖旧卡。

运行: PYTHONDONTWRITEBYTECODE=1 <python> tests/test_style.py

**绝不碰真实 data/**：``MemoryConfig.dir`` 一律指到临时目录；``service`` 也整个
替换成桩，避免碰到真库真配置。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi")

from music_collector import llm as llm_mod  # noqa: E402
from music_collector import memory as mm  # noqa: E402
from music_collector import style as st  # noqa: E402
from music_collector.config import AppConfig, LLMConfig, MemoryConfig  # noqa: E402

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
    return MemoryConfig(dir=tempfile.mkdtemp(prefix="style-"), **kw)


# ---------------------------------------------------------------- 采样过滤


def test_eligible() -> None:
    print("\n[1] 单条素材合格性")
    check("正常短句", st.eligible("绷不住了属实是"))
    check("极短梗（2 字）", st.eligible("寄了"))
    check("笑声也算语体", st.eligible("哈哈哈哈"))
    check("剥掉多余空白后判长度", st.eligible("  笑了  真的  "))
    for bad, why in (
        ("", "空"),
        ("1", "太短"),
        ("长" * 80, "太长"),
        ("https://music.163.com/song?id=1", "链接"),
        ("分享单曲：晴天", "分享卡片"),
        ("。。。。", "纯标点"),
        ("12345", "纯数字"),
    ):
        check(f"滤掉{why}", not st.eligible(bad), bad[:20])


def test_pick_samples() -> None:
    print("\n[2] 采样：每人配额 + 时间正序")
    rows = []
    rid = 0
    # 甲刷了 40 条（群里的话痨），乙只有 3 条
    for i in range(40):
        rid += 1
        rows.append({"id": rid, "user_id": 1, "content": f"甲的第三条消息第{i}号"})
    for i in range(3):
        rid += 1
        rows.append({"id": rid, "user_id": 2, "content": f"乙说的话第{i}号"})
    picked = st.pick_samples(rows, per_person=5)
    check("话痨被配额压住（≤5 条）", sum(1 for r in picked if r["user_id"] == 1) <= 5)
    check("话少的全留下（3 条）", sum(1 for r in picked if r["user_id"] == 2) == 3)
    check("结果按时间正序", [r["id"] for r in picked] == sorted(r["id"] for r in picked))

    # 等间隔：首尾都要保住，不能只取最近的几条
    first_ids = [r["id"] for r in picked if r["user_id"] == 1]
    check("抽到最旧的一条", first_ids and first_ids[0] == 1, str(first_ids))
    check("抽到较新的一条", first_ids and first_ids[-1] >= 30, str(first_ids))

    # 总字数上限：从最旧开始丢
    kept = st.pick_samples(rows, per_person=5, max_chars=30)
    check("总字数上限生效", len(kept) < len(picked), f"{len(kept)} vs {len(picked)}")
    check("丢的是旧的", kept and kept[-1]["id"] == picked[-1]["id"])

    # 噪声不该混进来
    noisy = [{"id": 1, "user_id": 1, "content": "https://x.cn/a"}, {"id": 2, "user_id": 1, "content": "真的假的"}]
    check("链接被滤掉", [r["content"] for r in st.pick_samples(noisy)] == ["真的假的"])


def test_material() -> None:
    print("\n[3] 素材不带身份信息")
    rows = [{"id": 1, "user_id": 123456, "nick": "小明", "content": "这歌有点东西"}]
    text = st.build_material(rows)
    check("只有原话", text == "- 这歌有点东西", text)
    check("不含昵称", "小明" not in text)
    check("不含 QQ 号", "123456" not in text)


# ---------------------------------------------------------------- 结构化结果


def test_compose_and_accept() -> None:
    print("\n[4] 结果渲染与门槛")
    data = {
        "length": "多数 8~15 字，一句一行",
        "punct": "句尾基本不打句号，爱用「。。。」",
        "words": ["绷", "乐", "属实"],
        "avoid": ["首先/其次这类书面连接词"],
        "samples": ["绷不住了属实是", "？你认真的吗。。。", "小明：这歌有点东西"],
    }
    body = st.compose_body(data)
    check("写了句长", "句长：" in body)
    check("写了标点", "标点：" in body)
    check("高频词成行", "- 高频词：绷、乐、属实" in body)
    check("样本带序号", "1. 绷不住了属实是" in body)
    check("剥掉样本里的身份前缀", "3. 这歌有点东西" in body, body)
    check("合格", st.acceptable(data)[0])

    check("空结果不合格", not st.acceptable({})[0])
    few = {"length": "短", "samples": ["绷", "寄"]}
    ok, why = st.acceptable(few)
    check("样本不足 3 条就不合格", not ok, why)
    check("拒绝原因说清楚", "样本" in why, why)
    no_desc = {"samples": ["绷不住了", "？真的吗", "有点东西"]}
    check("只有样本、没句长标点也不合格", not st.acceptable(no_desc)[0])


# ---------------------------------------------------------------- 读写与注入


def test_files() -> None:
    print("\n[5] 落盘、版本轮转、注入预算")
    mem = tmp_mem(style_chars=200)
    gid = 1001
    check("没卡时注入为空", st.render_for_prompt(mem, gid) == "")

    ok, _ = st.write_style(mem, gid, "- 句长：极短\n群友原话（学语感用，别复述内容）：\n1. 绷")
    check("写入成功", ok)
    check("文件在群目录下", st.style_path(mem, gid).exists())
    check("读回正文", "句长：极短" in st.read_style(mem, gid))
    check("frontmatter 不外泄", "---" not in st.read_style(mem, gid))
    check("meta 有更新时间", bool(st.read_meta(mem, gid).get("updated_at")))

    block = st.render_for_prompt(mem, gid)
    check("注入带标题", "【本群说话习惯】" in block)
    check("注入带约束句（别照搬原话）", "别照搬原话" in block)

    # 预算：卡比 style_chars 长时截断，且不报错
    st.write_style(mem, gid, "- 句长：" + "很长" * 300, rotate=False)
    long_block = st.render_for_prompt(mem, gid)
    check("超预算被截断", long_block.count("很长") * 2 <= 210, str(len(long_block)))
    check("截断后仍有标题", "【本群说话习惯】" in long_block)

    # 版本轮转：新卡进来，旧卡进 v1
    st.write_style(mem, gid, "- 句长：新的这一版")
    v1 = mm.group_dir(mem, gid) / "style.v1.md"
    check("旧卡进了 v1", v1.exists())
    check("v1 是上一版内容", "很长" in v1.read_text(encoding="utf-8"))

    # 开关
    off = tmp_mem(style_enabled=False)
    st.write_style(off, gid, "- 句长：随便")
    check("开关关了就不注入", st.render_for_prompt(off, gid) == "")

    # 空内容不覆盖
    before = st.read_style(mem, gid)
    ok, msg = st.write_style(mem, gid, "   ")
    check("空内容被拒", not ok and "空" in msg)
    check("旧卡原样保留", st.read_style(mem, gid) == before)


def test_render_position() -> None:
    print("\n[6] 注入顺序：群概况 → 风格 → 成员档案")
    mem = tmp_mem()
    gid = 2002
    mm.append_group_note(mem, gid, ["群里都听摇滚"])
    mm.touch(mem, gid, 123456, "小明")
    mm.append_note(mem, gid, 123456, "喜欢周杰伦", "小明")
    st.write_style(mem, gid, "- 句长：极短\n1. 绷")

    text = mm.render_for_prompt(mem, gid)
    i_group = text.find("【群概况】")
    i_style = text.find("【本群说话习惯】")
    i_people = text.find("【群成员档案】")
    check("三块都在", min(i_group, i_style, i_people) >= 0, text[:120])
    check("群概况在最前", i_group < i_style < i_people, f"{i_group}/{i_style}/{i_people}")

    # 记忆开关关掉时，风格卡也不注入（它属于记忆的一部分）
    off = tmp_mem(enabled=False)
    st.write_style(off, gid, "- 句长：极短")
    check("记忆关了风格也不注入", st.render_for_prompt(off, gid) == "")


# ---------------------------------------------------------------- 蒸馏任务


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    async def recent_chat(self, group_id, **kw):
        return list(self.rows)


class _FakeService:
    def __init__(self, rows, app):
        self.store = _FakeStore(rows)
        self._app = app

    def cfg(self, group_id):
        return self._app


def _sample_rows(n: int = 40) -> list[dict]:
    out = []
    for i in range(n):
        out.append({
            "id": i + 1,
            "user_id": 100 + (i % 4),
            "nick": f"群友{i % 4}",
            "content": f"第{i}条：这歌有点东西{['绷', '乐', '寄', '？'][i % 4]}",
            "created_at": 1_700_000_000 + i,
        })
    return out


def test_distill() -> None:
    print("\n[7] 蒸馏：失败绝不覆盖旧卡")
    mem = tmp_mem()
    gid = 3003
    app = AppConfig(
        llm=LLMConfig(enabled=True, base_url="http://x/v1", api_key="k", model="m"),
        memory=mem,
    )
    st.write_style(mem, gid, "- 句长：旧卡的内容")
    good = {
        "length": "极短，一句一行",
        "punct": "很少用句号",
        "words": ["绷", "乐"],
        "samples": ["这歌有点东西", "？你认真的吗", "绷不住了属实是"],
    }

    orig_service = st.service
    orig_complete = st.llm_bot.complete
    replies = [("不是 JSON 的胡话", ""), (json.dumps({**good, "samples": ["只有一条"]}, ensure_ascii=False), ""), (json.dumps(good, ensure_ascii=False), "")]
    calls = {"n": 0}

    async def fake_complete(prompt, cfg, **kw):
        idx = min(calls["n"], len(replies) - 1)
        calls["n"] += 1
        return replies[idx]

    st.service = _FakeService(_sample_rows(), app)
    st.llm_bot.complete = fake_complete
    try:
        r1 = asyncio.run(st.distill_group(gid))
        check("非 JSON → error", r1.get("status") == "error", str(r1))
        check("旧卡没被覆盖", st.read_style(mem, gid) == "- 句长：旧卡的内容")

        r2 = asyncio.run(st.distill_group(gid))
        check("样本太少 → error", r2.get("status") == "error", str(r2))
        check("旧卡仍然在", st.read_style(mem, gid) == "- 句长：旧卡的内容")

        r3 = asyncio.run(st.distill_group(gid))
        check("合格结果 → ok", r3.get("status") == "ok", str(r3))
        body = st.read_style(mem, gid)
        check("新卡生效", "极短，一句一行" in body, body)
        check("样本写进去了", "1. 这歌有点东西" in body, body)
        check("旧卡进了 v1", (mm.group_dir(mem, gid) / "style.v1.md").exists())

        # 素材不够时不该白打一次模型
        st.service = _FakeService(_sample_rows(5), app)
        before_calls = calls["n"]
        r4 = asyncio.run(st.distill_group(gid))
        check("素材太少 → skipped", r4.get("status") == "skipped", str(r4))
        check("素材太少不调用模型", calls["n"] == before_calls)

        # 开关关掉直接跳过
        off_mem = tmp_mem(style_enabled=False)
        st.service = _FakeService(_sample_rows(), AppConfig(memory=off_mem, llm=app.llm))
        r5 = asyncio.run(st.distill_group(gid))
        check("开关关掉 → skipped", r5.get("status") == "skipped", str(r5))
    finally:
        st.service = orig_service
        st.llm_bot.complete = orig_complete


def main() -> None:
    test_eligible()
    test_pick_samples()
    test_material()
    test_compose_and_accept()
    test_files()
    test_render_position()
    test_distill()
    print(f"\n{'=' * 46}\n通过 {PASSED} / 失败 {FAILED}")
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    main()

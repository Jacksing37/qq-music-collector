"""群成员长期记忆：每人一份 markdown，模型能读能写，人也能直接改。

目录结构::

    data/memory/
      <群号>/
        group.md             群概况：常聊话题、黑话、约定
        people/
          <QQ号>.md          一个人一份

为什么落到文件而不是只进数据库：

- 你能直接用任何编辑器打开、改、删——模型记错的时候不用绕一圈教它；
- 一个文件只装一个人，模型写错某人不会连带毁掉别人；
- 文本可 diff、可备份、可以手动挪走。

**安全边界**：``uid`` 必须是纯数字（QQ 号），文件名由代码拼出来，模型只能指定
「谁」而不能给路径——所以 ``../`` 这类穿越在这里没有入口。写入统一走「临时文件 +
``os.replace``」，进程被杀也不会留下半截文件。

接口约定：本模块所有函数第一个参数都是 :class:`MemoryConfig`（不是 ``AppConfig``
也不是 ``LLMConfig``）。这样 ``llm`` / ``tools`` 只传自己手里那段配置就够了，
不必为了读一个目录名去拖整个配置树。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import yaml
from nonebot.log import logger

from .config import DATA_DIR, MemoryConfig

#: frontmatter 与正文的分界（和技能包同一套写法，人不用记两种格式）
_FRONTMATTER = re.compile(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
#: QQ 号：1~20 位**半角**数字。钉死这一点，路径就不可能跑出 people/ 目录。
#: 用 [0-9] 而不是 \d——后者在 Python 里连全角「１２３」也算数字，那不该被当成 QQ 号。
_UID = re.compile(r"\A[0-9]{1,20}\Z")
#: 群号同理（-1、小数、全角都会被拒绝）
_GID = re.compile(r"\A[0-9]{1,20}\Z")
#: 正文里的一条记录
_NOTE_LINE = re.compile(r"^\s*[-*]\s+(.*)$")
#: 正文里的二级小节标题（三桶格式的骨架）
_SECTION_LINE = re.compile(r"^##\s+(.+?)\s*$")

#: 注入时每个人的摘要最多多少字（不是档案上限，只是喂给模型的那一行）
SUMMARY_CHARS = 220


# ---------------------------------------------------------------- 三桶正文
#
# 正文有两种形态，**都认、都能写回**：
#
#   1. 旧格式（本项目改造前一直是这个）：一段自由文本，每行 ``- 日期 内容``；
#   2. 三桶格式：``## 他是谁`` / ``## 我们之间`` / ``## 事件`` 三个小节。
#
# 为什么不在 ``Person`` 上开一个 ``buckets`` 字段：那样「字段」和「正文」就是两份
# 真相——人在网页上改完正文、字段还留着旧值，下一次整理就会把人的修改覆盖掉。
# 所以正文始终是唯一存储，三桶是**从正文解析出来的视图**，进出一律走
# :func:`parse_body` / :func:`join_body`。

#: 三个长期桶。顺序就是写盘与展示的顺序，改这里等于改文件格式。
BUCKETS: tuple[str, ...] = ("who", "us", "event")
#: 桶 → 小节标题。用中文标题是因为档案首先是**人也要读要改**的文本
#: （「## 他是谁」比「## who」更像一份给人看的档案）。
BUCKET_TITLES: dict[str, str] = {"who": "他是谁", "us": "我们之间", "event": "事件"}
#: 「近期」流水：带日期的日常琐事，由代码按天滚动淘汰，不计入长期条目配额。
#: 它和 ``event`` 是两种东西：``event`` 是「哪天要交作业」这种有确定时间点的
#: 长期事实，``recent`` 是「今天中午刚醒」这种过几天就没意义的流水。
RECENT = "recent"
RECENT_TITLE = "近期"
#: 认不出的内容（旧格式正文、未知小节、模型给的扁平 ``facts``）原样留在这里。
#: **绝不丢**：整理是整库重写，丢一次就再也找不回来了。
UNSORTED = "unclassified"
UNSORTED_TITLE = "未分类"

#: 写盘顺序。未分类放最后，免得它看起来像第四个正牌桶。
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("who", BUCKET_TITLES["who"]),
    ("us", BUCKET_TITLES["us"]),
    ("event", BUCKET_TITLES["event"]),
    (RECENT, RECENT_TITLE),
    (UNSORTED, UNSORTED_TITLE),
)
_TITLE_TO_KEY: dict[str, str] = {title: key for key, title in _SECTION_ORDER}


def parse_body(body: str) -> tuple[dict[str, list[str]], str]:
    """把正文拆成「小节 → 条目」+「认不出的原文」。

    返回 ``(sections, rest)``：

    - ``sections``：键取自 :data:`_SECTION_ORDER`（``who``/``us``/``event``/
      ``recent``/``unclassified``），值是**去掉 ``- `` 前缀**的条目；
    - ``rest``：不属于任何已知小节的原文（旧格式整段正文、未知小节连同标题），
      按原顺序保留——它既不进模型，也绝不丢。

    **旧格式（正文没有 ``##`` 小节）会得到空 ``sections`` + 整段原文**，
    调用方据此判断「这份档案还没分桶」，行为与改造前一致。
    """
    sections: dict[str, list[str]] = {}
    outside: list[str] = []
    current: Optional[str] = None
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = _SECTION_LINE.match(line)
        if matched:
            key = _TITLE_TO_KEY.get(matched.group(1).strip())
            if key is None:
                # 未知小节：标题连同内容一起留在原地（人可能自己加了「## 备注」）
                current = None
                outside.append(line)
                continue
            current = key
            sections.setdefault(key, [])
            continue
        if current is None:
            outside.append(line)
            continue
        text = line[2:].strip() if line.startswith(("- ", "* ")) else line
        if text:
            sections.setdefault(current, []).append(text)
    return sections, "\n".join(outside)


def join_body(sections: dict[str, list[str]], rest: str = "") -> str:
    """把解析出来的条目拼回 markdown 正文（:func:`parse_body` 的逆操作）。

    - 空桶不写标题，不留空壳；
    - ``rest`` 非空时单独成 ``## 未分类`` 一节；**只有** ``rest``、没有任何桶时
      原样返回 ``rest``——这样旧格式档案被读进来再写回去，一个字都不会变。
    """
    blocks: list[str] = []
    for key, title in _SECTION_ORDER:
        items = [" ".join(str(x).split()) for x in (sections.get(key) or [])]
        items = [x for x in items if x]
        if not items:
            continue
        blocks.append(f"## {title}")
        blocks.extend(f"- {item}" for item in items)
    body = "\n".join(blocks)
    tail = (rest or "").strip()
    if not tail:
        return body
    if not body:
        return tail
    # 和上面「未分类」小节的写法保持一致（不留空行），否则同一份内容拼两次会长得不一样
    return body + f"\n## {UNSORTED_TITLE}\n" + tail


# ---------------------------------------------------------------- 数据模型


@dataclass
class Person:
    """一个人的档案。``body`` 是 markdown 正文，每条观察一行 ``- 日期 内容``。"""

    uid: str
    nick: str = ""
    aliases: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    msgs: int = 0
    body: str = ""

    # -------------------------------------------------- 视图

    @property
    def display(self) -> str:
        """给模型看的称呼：``小明(123456)``；没昵称就只留 QQ 号。"""
        return f"{self.nick}({self.uid})" if self.nick else self.uid

    def notes(self) -> list[str]:
        """正文里的条目（去掉 ``- `` 前缀）。

        三桶正文只取 ``who``/``us``/``event``/``未分类`` 四节：``近期`` 是按天滚动
        淘汰的流水，混进注入摘要会把长期特征挤掉（摘要是有字数上限的一行）。
        **旧格式（没有 ``##`` 小节）走原来那条路**，返回值与改造前逐字相同。
        """
        sections, rest = parse_body(self.body)
        if not sections:
            return [
                m.group(1).strip()
                for m in map(_NOTE_LINE.match, self.body.splitlines())
                if m
            ]
        out: list[str] = []
        for key in (*BUCKETS, UNSORTED):
            out.extend(sections.get(key) or [])
        # 未知小节里的人写内容也要进摘要，否则「看得见但模型看不到」
        out.extend(m.group(1).strip() for m in map(_NOTE_LINE.match, rest.splitlines()) if m)
        return out

    def names(self) -> list[str]:
        """可以用来匹配的称呼集合（昵称 + 别名），忽略空值。"""
        out = [self.nick, *self.aliases]
        return [n for n in (x.strip() for x in out) if n]

    def sections(self) -> dict[str, list[str]]:
        """正文里的三桶条目（``who``/``us``/``event``，可能还有 ``recent``/``unclassified``）。

        旧格式正文返回空 dict——调用方据此区分「还没分桶」和「分了桶但是空的」。
        """
        return parse_body(self.body)[0]

    def summary(self, limit: int = SUMMARY_CHARS) -> str:
        """一行摘要：档案里所有条目用「；」串起来。"""
        items = self.notes()
        text = "；".join(items) if items else self.body.strip().replace("\n", " ")
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text or "（还没有记录）"


# ---------------------------------------------------------------- 路径


def memory_root(mem: MemoryConfig) -> Path:
    """记忆根目录：``memory.dir`` 没填就用 ``data/memory``。

    相对路径按 ``DATA_DIR`` 解析——和 ``skills_dir`` 同一套约定，
    免得「填了个相对路径结果跑到启动目录」。
    """
    raw = (mem.dir or "").strip()
    if not raw:
        return DATA_DIR / "memory"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (DATA_DIR / path)


def group_dir(mem: MemoryConfig, group_id: int) -> Path:
    # float / bool 一律拒绝：int(0.5) 会变成合法的 0，等于把「调用方传错」
    # 变成「悄悄写进群 0 的目录」，这种静默降级别扭又难查。
    if isinstance(group_id, bool) or not isinstance(group_id, (int, str)):
        raise ValueError(f"群号不合法: {group_id!r}")
    gid = str(int(group_id))
    if not _GID.match(gid):
        raise ValueError(f"群号不合法: {group_id!r}")
    return memory_root(mem) / gid


def people_dir(mem: MemoryConfig, group_id: int) -> Path:
    return group_dir(mem, group_id) / "people"


def _uid(value: object) -> Optional[str]:
    """把任意输入收敛成一个安全的用户名（QQ 号）。不合法返回 ``None``。"""
    text = str(value or "").strip()
    return text if _UID.match(text) else None


def _path_for(mem: MemoryConfig, group_id: int, uid: str) -> Path:
    safe = _uid(uid)
    if safe is None:
        raise ValueError(f"QQ 号不合法: {uid!r}")
    return people_dir(mem, group_id) / f"{safe}.md"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 解析 / 序列化


def _q(value: str) -> str:
    """把字符串写成 YAML 标量。用 JSON 引号最稳：中文不转义、特殊字符不会破格式。"""
    return json.dumps(str(value or ""), ensure_ascii=False)


def dump_person(person: Person) -> str:
    """序列化成「frontmatter + 正文」。手写而不是 yaml.dump，是为了排版稳定。

    正文**原样写出**（``person.body``）：三桶只是正文的一种排版，由 :func:`join_body`
    在写之前拼好（整理任务与网页端都走这条路），这里不再拆一次——两处都负责格式，
    迟早会出现「字段和正文各说一套」的情况。旧格式正文因此也能逐字写回。
    """
    aliases = "[" + ", ".join(_q(a) for a in person.aliases) + "]"
    head = [
        "---",
        f"uid: {_q(person.uid)}",
        f"nick: {_q(person.nick)}",
        f"aliases: {aliases}",
        f"first_seen: {_q(person.first_seen)}",
        f"last_seen: {_q(person.last_seen)}",
        f"msgs: {int(person.msgs)}",
        "---",
        "",
    ]
    body = (person.body or "").strip()
    return "\n".join(head) + (body + "\n" if body else "")


def parse_person(text: str, uid: str = "") -> Person:
    """从文件内容还原档案。**不抛异常**：坏文件当成「只有正文」。

    正文交给 :meth:`Person.sections` / :func:`parse_body` 按小节拆——这一步放在读取
    之外，是为了让「读到的东西」永远等于「文件里写的东西」，旧格式与三桶格式都成立。
    """
    raw = text or ""
    meta: dict = {}
    body = raw
    m = _FRONTMATTER.match(raw)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:  # noqa: BLE001 — frontmatter 坏了不影响正文
            meta = {}
        body = m.group(2)
    aliases = meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    try:
        msgs = int(meta.get("msgs") or 0)
    except (TypeError, ValueError):
        msgs = 0
    return Person(
        uid=str(meta.get("uid") or uid),
        nick=str(meta.get("nick") or ""),
        aliases=[str(a) for a in aliases if str(a).strip()],
        first_seen=str(meta.get("first_seen") or ""),
        last_seen=str(meta.get("last_seen") or ""),
        msgs=msgs,
        body=body.strip(),
    )


# ---------------------------------------------------------------- 读写


def _atomic_write(path: Path, text: str) -> None:
    """写临时文件再 ``os.replace``，避免读到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: 给同包其它模块复用（``style.py`` 写风格卡用）。做成公开别名，调用方就不用引用
#: 下划线名字，原子写也只在这里实现一次。
atomic_write = _atomic_write


def load_person(mem: MemoryConfig, group_id: int, uid: object) -> Optional[Person]:
    path = _path_for(mem, group_id, str(uid))
    if not path.exists():
        return None
    try:
        return parse_person(path.read_text(encoding="utf-8"), str(uid))
    except OSError as exc:
        logger.warning(f"[memory] 读档案失败 {path}: {exc}")
        return None


def save_person(mem: MemoryConfig, group_id: int, person: Person) -> None:
    _atomic_write(_path_for(mem, group_id, person.uid), dump_person(person))


def list_people(
    mem: MemoryConfig, group_id: int, *, newest_first: bool = True
) -> list[Person]:
    """列出该群所有档案。按最后活跃时间倒序（没时间的排后面）。"""
    folder = people_dir(mem, group_id)
    if not folder.is_dir():
        return []
    people: list[Person] = []
    for path in folder.glob("*.md"):
        uid = path.stem
        if _uid(uid) is None:
            continue
        try:
            people.append(parse_person(path.read_text(encoding="utf-8"), uid))
        except OSError:
            continue
    if newest_first:
        people.sort(key=lambda p: (p.last_seen or "", p.msgs), reverse=True)
    else:
        people.sort(key=lambda p: p.uid)
    return people


def list_groups(mem: MemoryConfig) -> list[int]:
    """磁盘上已经有记忆的群号（网页端「群记忆」页的下拉数据源）。"""
    root = memory_root(mem)
    if not root.is_dir():
        return []
    out: list[int] = []
    for path in root.iterdir():
        if path.is_dir() and _GID.match(path.name):
            out.append(int(path.name))
    return sorted(out)


# ---------------------------------------------------------------- 写入动作


def touch(mem: MemoryConfig, group_id: int, uid: object, nick: str = "") -> bool:
    """更新「最后活跃时间 / 消息数 / 昵称」，必要时建档。返回是否新建。

    旁听每条消息都会调它，所以**不重排正文**，只动 frontmatter。
    """
    safe = _uid(uid)
    if safe is None:
        return False
    person = load_person(mem, group_id, safe)
    created = person is None
    if person is None:
        person = Person(uid=safe, first_seen=_now())
    person.last_seen = _now()
    person.msgs += 1
    if nick:
        person.nick = str(nick).strip()[:40]
    save_person(mem, group_id, person)
    return created


def _append_recent(
    mem: MemoryConfig, group_id: int, person: Person, text: str, limit: int
) -> tuple[bool, str]:
    """把一条带日期的观察追加进 ``## 近期`` 小节（三桶模式专用）。

    为什么是「近期」而不是直接塞进三个桶：``remember`` 只拿到一句「用户说了什么」，
    分不清这句是长期特征还是一次性的事；让模型在下一次整理时判，比让工具现场猜准。
    超字数时**只挤近期流水**，绝不动三个长期桶——那才是档案的主体。
    """
    sections, rest = parse_body(person.body)
    recent = sections.setdefault(RECENT, [])
    item = f"{_today()} {text}"
    if item in recent:
        return False, "这条已经记过了，没有重复写入。"
    recent.append(item)
    dropped = 0
    while len(join_body(sections, rest)) > limit and len(recent) > 1:
        recent.pop(0)
        dropped += 1
    person.body = join_body(sections, rest)
    save_person(mem, group_id, person)
    logger.info(
        f"[memory] 群{group_id} 记下 {person.display} 一条近期流水（{len(text)} 字）"
        + (f"，挤掉最早的 {dropped} 条" if dropped else "")
    )
    extra = f"（近期流水已满，挤掉了最早的 {dropped} 条）" if dropped else ""
    return True, f"已记到 {person.display} 的档案里{extra}"


def append_note(
    mem: MemoryConfig,
    group_id: int,
    uid: object,
    note: str,
    nick: str = "",
) -> tuple[bool, str]:
    """追加一条观察。返回 ``(是否写入, 给模型看的说明)``。

    超过 ``memory.note_chars`` 时丢掉**最早**的条目腾地方，并把这件事写进返回值——
    模型看得到「我记满了，旧记录被挤掉了」，不至于以为自己写成功了全套。

    ``memory.buckets_enabled`` 打开时，这条带日期的观察进 ``## 近期`` 小节：
    ``remember`` 工具记的是「当下发生的事」，本来就该先进流水，等下一次整理再由
    模型判断它到底属于「他是谁」还是「事件」。**关掉时走老路径，一字不改。**
    """
    safe = _uid(uid)
    if safe is None:
        return False, f"「{uid}」不是合法的 QQ 号，无法记录。"
    text = (note or "").strip()
    if not text:
        return False, "内容为空，没有记录。"
    limit = max(200, int(mem.note_chars))

    person = load_person(mem, group_id, safe)
    if person is None:
        person = Person(uid=safe, first_seen=_now())
    if nick and not person.nick:
        person.nick = str(nick).strip()[:40]
    person.last_seen = _now()

    if mem.buckets_enabled:
        return _append_recent(mem, group_id, person, text, limit)

    line = f"- {_today()} {text}"
    lines = [ln for ln in person.body.splitlines() if ln.strip()]
    # 完全一样的一条不重复记
    if line in lines:
        return False, "这条已经记过了，没有重复写入。"
    lines.append(line)
    dropped = 0
    while sum(len(x) for x in lines) > limit and len(lines) > 1:
        lines.pop(0)
        dropped += 1
    body = "\n".join(lines)
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    person.body = body
    save_person(mem, group_id, person)
    logger.info(
        f"[memory] 群{group_id} 记下 {person.display} 一条（{len(text)} 字，"
        f"现 {len(body)}/{limit} 字" + (f"，挤掉旧记录 {dropped} 条" if dropped else "") + "）"
    )
    extra = f"（档案已满，挤掉了最早的 {dropped} 条记录）" if dropped else ""
    return True, f"已记到 {person.display} 的档案里{extra}"


def rewrite_body(
    mem: MemoryConfig, group_id: int, uid: object, body: str
) -> tuple[bool, str]:
    """整段替换正文（网页端编辑用；模型的整理写入也走这里）。"""
    safe = _uid(uid)
    if safe is None:
        return False, f"「{uid}」不是合法的 QQ 号。"
    limit = max(200, int(mem.note_chars))
    person = load_person(mem, group_id, safe)
    if person is None:
        person = Person(uid=safe, first_seen=_now())
    text = (body or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    person.body = text
    person.last_seen = _now()
    save_person(mem, group_id, person)
    return True, f"已更新 {person.display} 的档案（{len(text)} 字）"


def forget_note(
    mem: MemoryConfig, group_id: int, uid: object, keyword: str
) -> tuple[int, str]:
    """删掉正文里包含 ``keyword`` 的条目。返回 ``(删了几条, 说明)``。"""
    safe = _uid(uid)
    key = (keyword or "").strip()
    if safe is None:
        return 0, f"「{uid}」不是合法的 QQ 号。"
    if not key:
        return 0, "要删哪条？给我一个关键词。"
    person = load_person(mem, group_id, safe)
    if person is None:
        return 0, f"还没有 {safe} 的档案。"
    lines = [ln for ln in person.body.splitlines() if ln.strip()]
    keep = [ln for ln in lines if key not in ln]
    removed = len(lines) - len(keep)
    if not removed:
        return 0, f"{person.display} 的档案里没有包含「{key}」的条目。"
    person.body = "\n".join(keep)
    save_person(mem, group_id, person)
    logger.info(
        f"[memory] 群{group_id} 从 {person.display} 档案删掉 {removed} 条（关键词「{key}」）"
    )
    return removed, f"已从 {person.display} 的档案删掉 {removed} 条包含「{key}」的记录"


def delete_person(mem: MemoryConfig, group_id: int, uid: object) -> bool:
    path = _path_for(mem, group_id, str(uid))
    if not path.exists():
        return False
    path.unlink()
    return True


# ---------------------------------------------------------------- 群概况


def group_note_path(mem: MemoryConfig, group_id: int) -> Path:
    return group_dir(mem, group_id) / "group.md"


def read_group_note(mem: MemoryConfig, group_id: int) -> str:
    path = group_note_path(mem, group_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_group_note(
    mem: MemoryConfig, group_id: int, text: str
) -> tuple[bool, str]:
    """写群概况。``---`` 开头会被当成 frontmatter 处理掉，所以统一补标题。"""
    limit = max(500, int(mem.note_chars) * 2)
    body = (text or "").strip()
    if not body:
        return False, "内容为空。"
    if body.startswith("---"):
        body = "\n".join(body.splitlines()[1:]).lstrip("-\n").strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    _atomic_write(group_note_path(mem, group_id), "# 群概况\n\n" + body + "\n")
    return True, f"群概况已更新（{len(body)} 字）"


def append_group_note(
    mem: MemoryConfig, group_id: int, lines: Iterable[str]
) -> tuple[int, str]:
    """往群概况末尾追加几条（去重）。返回 ``(新增几条, 说明)``。

    和 ``append_note`` 一样，满了就丢最早的——群概况不该无限长，否则它会把
    「成员档案」的注入预算挤掉。
    """
    limit = max(500, int(mem.note_chars) * 2)
    fresh = [str(x).strip() for x in lines if str(x).strip()]
    if not fresh:
        return 0, "没有新内容。"
    current = read_group_note(mem, group_id)
    existing = [ln.strip() for ln in current.splitlines() if ln.strip()]
    if existing and existing[0].startswith("# 群概况"):
        existing = existing[1:]
    # 去掉列表符号后比较：手写的「群规：不刷屏」和追加的「- 群规：不刷屏」是同一条
    seen = {ln[2:].strip() if ln.startswith(("- ", "* ")) else ln for ln in existing}
    added = 0
    for item in fresh:
        text = item[2:].strip() if item.startswith(("- ", "* ")) else item
        if not text or text in seen:
            continue
        seen.add(text)
        existing.append(f"- {text}")
        added += 1
    if not added:
        return 0, "这些内容已经记过了。"
    dropped = 0
    while sum(len(x) for x in existing) > limit and len(existing) > 1:
        existing.pop(0)
        dropped += 1
    body = "\n".join(existing)
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    _atomic_write(group_note_path(mem, group_id), "# 群概况\n\n" + body + "\n")
    extra = f"，挤掉最早的 {dropped} 条" if dropped else ""
    return added, f"群概况新增 {added} 条{extra}"


# ---------------------------------------------------------------- 定位「谁」


def resolve_who(
    mem: MemoryConfig, group_id: int, who: str
) -> tuple[Optional[str], str]:
    """把模型给的「谁」解析成 QQ 号。返回 ``(uid, 说明)``。

    顺序：纯数字 → 昵称/别名精确匹配 → 唯一包含匹配。多个都能匹配时**不猜**，
    把候选人列出来让模型自己决定——猜错人比记不住更糟。
    """
    raw = str(who or "").strip()
    if not raw:
        return None, "没说是谁。"
    safe = _uid(raw)
    if safe:
        return safe, ""
    people = list_people(mem, group_id, newest_first=False)
    exact = [p for p in people if raw in p.names()]
    if len(exact) == 1:
        return exact[0].uid, ""
    if len(exact) > 1:
        names = "、".join(p.display for p in exact[:8])
        return None, f"有 {len(exact)} 个人都叫「{raw}」：{names}。请改用 QQ 号指定。"
    loose = [p for p in people if any(raw in n or n in raw for n in p.names())]
    if len(loose) == 1:
        return loose[0].uid, ""
    if len(loose) > 1:
        names = "、".join(p.display for p in loose[:8])
        return None, f"「{raw}」可能指好几个人：{names}。请改用 QQ 号指定。"
    return None, f"档案里没有叫「{raw}」的人。可以先用 QQ 号记录它。"


# ---------------------------------------------------------------- 相关度检索

#: 相关度 / 重要度 / 新鲜度三者的权重。相关度占一半是刻意的：这一层存在的意义就是
#: 「问谁就带谁」，活跃度只能当配角，否则又会退回按活跃倒序。
_W_RELEVANCE = 0.5
_W_IMPORTANCE = 0.3
_W_FRESHNESS = 0.2
#: 重要度常量。``Person`` 里只有 ``msgs`` / ``first_seen`` / ``last_seen`` 三个元数据，
#: 其中 ``msgs`` 是**活跃度**不是重要度——拿它当重要度等于让「话最多的人」重新赢回排序，
#: 而这层检索就是为了摆脱这个偏差。档案 frontmatter 目前没有手工标记，所以一律 0.5。
_IMPORTANCE = 0.5
#: 相关度放大量：query 的 token 有一半被覆盖就算「很相关」（0.5×2.2 已封顶）。
#: 二元组切分会产生「伦哪」这种跨词碎片，不放大一点真相关的档案得分会被稀释得太低。
_RELEVANCE_GAIN = 2.2
#: 姓名/别名在 query 里被完整念出时的额外加权
_NAME_BOOST = 1.4
#: 新鲜度半衰期（天）：一个月没说话，这一项减半
_FRESH_HALF_LIFE = 30.0
#: ``last_seen`` 的两种写法（``touch`` 写前者，手改的档案可能只写日期）
_SEEN_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")

#: 中文按二元组切，英文/数字整词。**刻意不引分词库**：无监督分词在群昵称、黑话、
#: 生造词上并不可靠，二元组召回够用、零依赖、零维护。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RUN = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> set[str]:
    """中文按二元组 + 英文/数字整词。

    「我昨天加班到十点」→ 我昨/昨天/天加/加班/班到/到十/十点。
    单字成段（「猫」「妈」）保留单字本身，否则一个人的名字只有一个字时永远匹配不上；
    纯标点、空白、空串一律得到空集合（调用方据此判断「这句话没有可检索的信息」）。
    """
    raw = text or ""
    tokens: set[str] = set()
    for run in _CJK_RUN.findall(raw):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    tokens.update(word.lower() for word in _WORD_RUN.findall(raw))
    return tokens


def _name_tokens(person: Person) -> set[str]:
    """昵称/别名切出来的 token（提问里念到名字也算命中这份档案）。"""
    out: set[str] = set()
    for name in person.names():
        out |= tokenize(name)
    return out


def _name_hit(person: Person, tokens: set[str]) -> bool:
    """query 里是否**完整**念出了某个称呼。

    要求称呼的二元组全部命中：「王小明」不会因为提问里有「小明」就算命中，
    否则群里叫「阿强」的和叫「小阿强」的会互相偷分数。
    """
    for name in person.names():
        name_tokens = tokenize(name)
        if name_tokens and name_tokens <= tokens:
            return True
    return False


def _relevance(person: Person, tokens: set[str]) -> float:
    """相关度：query 的 token 有多少被这份档案覆盖，放大后封顶 1.0。

    覆盖范围是**正文 + 称呼**：问「小明喜欢什么」时，小明的档案正文里未必写着「小明」
    三个字，只算正文会让最该被带上的那个人拿 0 分。
    """
    if not tokens:
        return 0.0
    covered = tokens & (tokenize(person.body) | _name_tokens(person))
    score = min(1.0, len(covered) / len(tokens) * _RELEVANCE_GAIN)
    if _name_hit(person, tokens):
        score = min(1.0, score * _NAME_BOOST)
    return score


def _freshness(person: Person, now: datetime) -> float:
    """新鲜度 ``0.5 ** (days / 30)``：今天≈1，一个月前 0.5。

    没有时间字段（或格式认不出来）给 0.5——不给 0 是为了不让缺元数据的档案被判死，
    也不给 1 是为了不假装它很新。未来时间按 0 天算（时钟回拨时不至于炸）。
    """
    stamp = (person.last_seen or "").strip()
    if not stamp:
        return 0.5
    seen: Optional[datetime] = None
    for fmt in _SEEN_FORMATS:
        try:
            seen = datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    if seen is None:
        return 0.5
    days = (now - seen).total_seconds() / 86400.0
    return 0.5 ** (max(0.0, days) / _FRESH_HALF_LIFE)


def _total_score(relevance: float, freshness: float) -> float:
    """三项加权。``pick_people`` 里相关度只算一次，所以先拆成这个函数再组装。"""
    return (
        relevance * _W_RELEVANCE
        + _IMPORTANCE * _W_IMPORTANCE
        + freshness * _W_FRESHNESS
    )


def score_person(person: Person, tokens: set[str], now: datetime) -> float:
    """单个人的得分：相关度×0.5 + 重要度×0.3 + 新鲜度×0.2。

    只有 :func:`pick_people` 关心排序，但把它单独拎出来是为了能被直接测：
    「换个日期，新鲜度就该掉多少」这类断言不该通过注入块间接验证。
    """
    return _total_score(_relevance(person, tokens), _freshness(person, now))


def _row_text(person: Person) -> str:
    """注入时每个人的那一行。挑人时量字数、拼正文时写字，两处必须同一个写法。"""
    return f"- {person.display}：{person.summary()}"


def _pick_detail(
    mem: MemoryConfig, group_id: int, query: str, *, top: int, min_n: int, chars: int
) -> tuple[list[Person], bool]:
    """按 ``query`` 挑人，返回 ``(选中的人, 是否命中)``。

    **没命中就返回「按最近活跃倒序的全量列表」+ ``False``**，调用方据此走旧渲染路径
    （旧路径的预算、顺序、标题文案一字不改）——这是「退化后不更差」的落点。
    ``query`` 为空、没有档案、命中但一个人都装不下，也都走这条：

    - 全员无关时不该把档案池缩小成空（问一句「量子力学」不等于群里没人在）；
    - 一个人都装不下时旧路径至少还能装，凭空变差不如不变。
    """
    people = list_people(mem, group_id)
    tokens = tokenize(query or "")
    if not people or not tokens:
        return people, False

    now = datetime.now()
    # 相关度只算一次：再交给 score_person 会重复折一遍分词，人多了纯属浪费
    scored: list[tuple[Person, float, float]] = []
    for person in people:
        relevance = _relevance(person, tokens)
        scored.append((person, _total_score(relevance, _freshness(person, now)), relevance))
    if not any(relevance > 0 for _, _, relevance in scored):
        return people, False

    # sorted 是稳定排序：同分的人保持「最近活跃」的相对次序，退化时也不抖动
    ranked = sorted(scored, key=lambda item: item[1], reverse=True)
    cap_top = max(1, int(top))
    cap_min = min(max(1, int(min_n)), cap_top)  # 补齐也不能突破 top：最多挑 top 条是硬上限
    budget = max(1, int(chars))

    picked = [person for person, _, relevance in ranked if relevance > 0][:cap_top]
    if len(picked) < cap_min:
        chosen = {id(person) for person in picked}
        for person, _, _ in ranked:
            if len(picked) >= cap_min:
                break
            if id(person) not in chosen:
                picked.append(person)
                chosen.add(id(person))

    out: list[Person] = []
    used = 0
    for person in picked:
        row = _row_text(person)
        if used + len(row) > budget:
            break
        out.append(person)
        used += len(row)
    return (out, True) if out else (people, False)


def pick_people(
    mem: MemoryConfig, group_id: int, query: str, *, top: int, min_n: int, chars: int
) -> list[Person]:
    """按当前这句话挑人；``query`` 为空 / 全员无关时退化为「按最近活跃倒序」的全量列表。

    - 命中：按分数取 ``top`` 条；不足 ``min_n`` 时按分数补齐（以免只挑出一个人时，
      模型把「一个人」当成「全群」）；总长不超过 ``chars`` 字。
    - 命中的人全部排在最前，同分时保持原有活跃次序。
    """
    return _pick_detail(
        mem, group_id, query, top=top, min_n=min_n, chars=chars
    )[0]


# ---------------------------------------------------------------- 注入


def render_for_prompt(mem: MemoryConfig, group_id: int, query: str = "") -> str:
    """拼出塞进系统提示词的「记忆」段落。**绝不抛异常**（记忆坏了不能耽误回话）。

    预算用 ``memory.inject_chars``：装不下的部分不装，末尾注明还剩多少人没进来——
    模型知道「有人没看到」，就不会把人记混成「群里只有这些人」。

    ``query`` 是当前这句话。给了它、且 ``memory.recall_enabled`` 打开时，装人前先按
    相关度挑一遍（:func:`pick_people`）；**两者任一不满足，输出与改造前逐字相同**
    （群概况、风格卡、末尾提示的行为也一律不变）。

    块顺序固定为 **群概况 → 风格卡 → 心事 → 成员档案**。心事由 :mod:`heart` 提供，
    ``persona.heart_enabled`` 关着时它返回空串、这一格不占位——所以默认配置下
    整段输出与改造前逐字相同。
    """
    if not mem.enabled:
        return ""
    try:
        budget = max(500, int(mem.inject_chars))
        blocks: list[str] = []
        used = 0

        group_text = read_group_note(mem, group_id)
        if group_text:
            room = min(len(group_text), max(500, budget // 4))
            clipped = group_text[:room]
            if len(group_text) > room:
                clipped += "…"
            blocks.append("【群概况】\n" + clipped)
            used += len(clipped)

        # 风格卡排在群概况之后、成员档案之前：它是「怎么说话」，比「谁是谁」更该
        # 被先读到。**不计入 used**——它有独立预算 style_chars，不该挤掉成员档案。
        # 延迟导入避开循环依赖：style 要用 memory 的路径与原子写。
        try:
            from . import style as style_mod

            style_block = style_mod.render_for_prompt(mem, group_id)
        except Exception as exc:  # noqa: BLE001 — 风格卡坏了不能耽误回话
            logger.warning(f"[memory] 组装风格卡失败（本次不带）: {exc}")
            style_block = ""
        if style_block:
            blocks.append(style_block.strip("\n"))

        # 心事排在风格卡之后、成员档案之前：它回答的是「我成了谁」，比「谁是谁」更该
        # 先被读到；同时它必须留在群概况与风格卡之后，否则「这个群怎么说话」会被挤到
        # 人设末尾的角落里，语气就散了。``persona.heart_enabled`` 关着时它返回空串，
        # 这一格不占位——默认配置下整段输出与改造前逐字相同。
        # 延迟导入避开循环依赖：heart 要用 memory 的路径与原子写。
        try:
            from . import heart as heart_mod

            heart_block = heart_mod.render(group_id, mem=mem)
        except Exception as exc:  # noqa: BLE001 — 心事坏了不能耽误回话
            logger.warning(f"[memory] 组装心事失败（本次不带）: {exc}")
            heart_block = ""
        if heart_block:
            blocks.append(heart_block.strip("\n"))

        people = list_people(mem, group_id)
        # 检索只插在「装人」这一步：群概况与风格卡已经拼好，不参与打分也不改预算
        picked = people
        by_score = False
        if (query or "").strip() and mem.recall_enabled:
            try:
                picked, by_score = _pick_detail(
                    mem,
                    group_id,
                    query or "",
                    top=mem.recall_top,
                    min_n=mem.recall_min,
                    chars=mem.recall_chars,
                )
            except Exception as exc:  # noqa: BLE001 — 打分坏了按活跃倒序，不能不回话
                logger.warning(f"[memory] 相关度挑选失败（本次按最近活跃装人）: {exc}")
                picked, by_score = people, False

        rows: list[str] = []
        for person in picked:
            row = _row_text(person)
            if used + len(row) > budget:
                break
            rows.append(row)
            used += len(row)

        if rows:
            if by_score:
                head = f"【群成员档案】共 {len(people)} 人，以下按与这句话的相关度挑选："
            else:
                head = f"【群成员档案】共 {len(people)} 人，以下按最近活跃排序："
            blocks.append(head + "\n" + "\n".join(rows))
        # 两种「没进来」都要交代：预算装不下（旧行为），或被相关度筛掉了（新行为）——
        # 不说明的话模型会以为群里只有被挑中的这几个人
        skipped = len(people) - len(rows) if (len(rows) < len(picked) or by_score) else 0
        if skipped:
            blocks.append(
                f"（还有 {skipped} 人的档案没装进来，需要时用 recall 按名字或 QQ 号查）"
            )
        if not blocks:
            return ""
        return "\n" + "\n".join(blocks) + "\n"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[memory] 组装记忆失败（本次不带记忆）: {exc}")
        return ""


def stats(mem: MemoryConfig, group_id: int) -> dict:
    """网页端状态显示用：人数、总字数、最后更新时间、三桶条目数。"""
    try:
        people = list_people(mem, group_id)
        sizes = [len(p.body) for p in people]
        # 三桶条目数：网页端据此显示「他是谁 6 / 我们之间 3 / 事件 2」。
        # 只在开关打开时算——关着的时候正文是自由文本，统计它没有意义，
        # 也免得给每条老档案多跑一次解析。
        bucket_counts = {key: 0 for key in (*BUCKETS, RECENT)}
        if mem.buckets_enabled:
            for person in people:
                sections, _rest = parse_body(person.body)
                for key in bucket_counts:
                    bucket_counts[key] += len(sections.get(key) or [])
        return {
            "people": len(people),
            "chars": sum(sizes),
            "biggest": max(sizes) if sizes else 0,
            "group_chars": len(read_group_note(mem, group_id)),
            "last_seen": max((p.last_seen for p in people if p.last_seen), default=""),
            "root": str(memory_root(mem)),
            "buckets": bucket_counts,
            "buckets_enabled": bool(mem.buckets_enabled),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "people": 0,
            "chars": 0,
            "biggest": 0,
            "group_chars": 0,
            "last_seen": "",
            "root": "",
            "error": str(exc),
        }


# ---------------------------------------------------------------- 整理素材


def digest_source(rows: Iterable[dict], *, max_chars: int = 20000) -> str:
    """把 ``chat_log`` 的行拼成「谁说了什么」，供整理任务喂给模型。

    只保留有内容的行，按时间正序；超长时从**最旧**开始丢（新的信息更该被看到）。
    ``rows`` 每项需有 ``nick`` / ``user_id`` / ``content``。
    """
    lines: list[str] = []
    total = 0
    for row in reversed(list(rows)):
        nick = str(row.get("nick") or row.get("user_id") or "")
        uid = str(row.get("user_id") or "")
        text = str(row.get("content") or "").strip().replace("\n", " ")
        if not text:
            continue
        line = f"{nick}({uid})：{text[:400]}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    lines.reverse()
    return "\n".join(lines)

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

#: 注入时每个人的摘要最多多少字（不是档案上限，只是喂给模型的那一行）
SUMMARY_CHARS = 220


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
        """正文里的条目（去掉 ``- `` 前缀）。"""
        return [m.group(1).strip() for m in map(_NOTE_LINE.match, self.body.splitlines()) if m]

    def names(self) -> list[str]:
        """可以用来匹配的称呼集合（昵称 + 别名），忽略空值。"""
        out = [self.nick, *self.aliases]
        return [n for n in (x.strip() for x in out) if n]

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
    """序列化成「frontmatter + 正文」。手写而不是 yaml.dump，是为了排版稳定。"""
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
    """从文件内容还原档案。**不抛异常**：坏文件当成「只有正文」。"""
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


# ---------------------------------------------------------------- 注入


def render_for_prompt(mem: MemoryConfig, group_id: int) -> str:
    """拼出塞进系统提示词的「记忆」段落。**绝不抛异常**（记忆坏了不能耽误回话）。

    预算用 ``memory.inject_chars``：按最后活跃倒序装人，装不下就停，末尾注明还剩
    多少人没进来——模型知道「有人没看到」，就不会把人记混成「群里只有这些人」。
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

        people = list_people(mem, group_id)
        rows: list[str] = []
        skipped = 0
        for person in people:
            row = f"- {person.display}：{person.summary()}"
            if used + len(row) > budget:
                skipped = len(people) - len(rows)
                break
            rows.append(row)
            used += len(row)

        if rows:
            head = f"【群成员档案】共 {len(people)} 人，以下按最近活跃排序："
            blocks.append(head + "\n" + "\n".join(rows))
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
    """网页端状态显示用：人数、总字数、最后更新时间。"""
    try:
        people = list_people(mem, group_id)
        sizes = [len(p.body) for p in people]
        return {
            "people": len(people),
            "chars": sum(sizes),
            "biggest": max(sizes) if sizes else 0,
            "group_chars": len(read_group_note(mem, group_id)),
            "last_seen": max((p.last_seen for p in people if p.last_seen), default=""),
            "root": str(memory_root(mem)),
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

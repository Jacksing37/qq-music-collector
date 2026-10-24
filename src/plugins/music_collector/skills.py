"""Skill 能力包：从 ``data/skills/<名字>/SKILL.md`` 读取「方法论」。

和 Claude / WorkBuddy 那套 skill 一个思路，只保留最小可用集合：

- 一个技能 = 一个目录，里面**必须有** ``SKILL.md``；
- ``SKILL.md`` 开头的 YAML frontmatter 写 ``name`` / ``description``，
  正文就是给模型看的说明（步骤、话术、判断规则……）；
- 模型用 ``list_skills`` 看到「有哪些技能 + 一句话简介」，
  觉得用得上再 ``use_skill`` 读全文——这就是**渐进披露**，不会一上来就把
  所有技能正文塞进上下文。

技能正文只是**说明文字**，真正的动作只有内置工具能做。想让技能带可执行脚本，
必须在配置里显式打开 ``llm.skill_scripts``（默认关），并且脚本只能来自技能
目录内、后缀白名单、有超时和输出上限。

**聊天里不能安装技能**（硬规则，没有开关）：模型没有任何写文件的工具，脚本跑完
还会拿快照比对技能目录，新增的文件删掉、被改的写回去（见 :func:`revert_changes`）。
所以「在群里让机器人装个技能」这件事在代码层面就走不通，只能管理员手动放目录。
"""

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml
from nonebot.log import logger

#: 允许执行的脚本后缀。别加 .exe/.bat——那是「模型决定跑什么」的最短路径
ALLOWED_SUFFIX = {".py", ".sh"}
#: ``read_skill_file`` 允许读的后缀。只读文本：技能目录里也可能有图片、压缩包，
#: 读成文本只会得到一堆乱码，白占上下文
READABLE_SUFFIX = {".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".csv"}
#: 一次最多传几个参数、单个参数多长（防止模型拿参数往脚本里灌东西）
MAX_ARGS = 8
MAX_ARG_CHARS = 200
#: 脚本输出最多回给模型多少字
MAX_OUTPUT = 4000
#: 只读守卫的快照上限：文件数 / 总字节数，超了就只报告不还原（避免读爆内存）
SNAPSHOT_MAX_FILES = 800
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024

_FRONTMATTER = re.compile(r"\A\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass(frozen=True)
class Skill:
    """一个技能包。``body`` 是 frontmatter 之后的正文。"""

    name: str
    description: str
    root: Path
    body: str

    @property
    def scripts(self) -> list[str]:
        """技能目录里允许被调用的脚本（相对路径，供 ``list_skills`` 展示）。"""
        found: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix in ALLOWED_SUFFIX:
                found.append(path.relative_to(self.root).as_posix())
                if len(found) >= 20:
                    break
        return found


def parse_skill(text: str, root: Path, fallback_name: str) -> Optional[Skill]:
    """解析 ``SKILL.md``；没有 frontmatter 也收，用目录名当 name。"""
    meta: dict = {}
    body = text
    m = _FRONTMATTER.match(text)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except Exception as exc:  # noqa: BLE001 — 写错就退回「没有元数据」
            logger.debug(f"[skill] frontmatter 解析失败（{root.name}）: {exc}")
        body = m.group(2)
    name = str(meta.get("name") or fallback_name).strip()
    desc = str(meta.get("description") or "").strip()
    body = body.strip()
    if not name or not body:
        return None
    return Skill(name=name, description=desc, root=root, body=body)


def discover(root: Path) -> list[Skill]:
    """扫描 ``root`` 下的一层子目录，返回所有可用技能（按名字排序）。

    读盘失败、解析失败都当「没有这个技能」，不抛异常——技能目录是用户随手
    放的地方，一个坏文件不该让整个机器人开不了口。
    """
    root = Path(root)
    if not root.is_dir():
        return []
    skills: list[Skill] = []
    seen: set[str] = set()
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        path = child / "SKILL.md"
        if not path.is_file():
            continue
        try:
            skill = parse_skill(path.read_text(encoding="utf-8", errors="replace"), child, child.name)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[skill] 读取失败 {path}: {exc}")
            continue
        if skill is None:
            logger.debug(f"[skill] 跳过（缺 name 或正文）：{path}")
            continue
        if skill.name in seen:
            logger.warning(f"[skill] 技能名重复，跳过：{skill.name}（{path}）")
            continue
        seen.add(skill.name)
        skills.append(skill)
    return skills


def render_list(skills: Iterable[Skill]) -> str:
    """给模型看的技能清单：名字 + 一句话简介（不含正文）。"""
    rows = []
    for skill in skills:
        desc = skill.description or "（没有写简介）"
        rows.append(f"- {skill.name}: {desc}")
    return "\n".join(rows)


def find(skills: Iterable[Skill], name: str) -> Optional[Skill]:
    """按名字找技能；大小写不敏感，容忍两边空白。"""
    want = (name or "").strip().lower()
    for skill in skills:
        if skill.name.lower() == want:
            return skill
    return None


def _resolve_member(skill: Skill, rel: str, what: str = "文件") -> Path:
    """把 ``rel`` 解析成技能目录内的真实路径；越界直接拒绝。

    这是「模型给的路径」唯一要过的一道闸：``../../.env`` 这种能让它读到服务器上
    任意文件，所以必须解析后确认仍在技能根目录内。
    """
    text = (rel or "").strip().lstrip("/\\")
    if not text:
        raise ValueError(f"没有指定{what}")
    root = skill.root.resolve()
    target = (root / text).resolve()
    # startswith 不够：/data/skills/a-evil 也以 /data/skills/a 开头
    if target != root and root not in target.parents:
        raise ValueError(f"{what}不在技能目录内: {rel}")
    if not target.is_file():
        raise ValueError(f"{what}不存在: {rel}")
    return target


def _resolve_script(skill: Skill, script: str) -> Path:
    """把 ``script`` 解析成技能目录内的真实路径；越界直接拒绝。"""
    target = _resolve_member(skill, script, "脚本")
    if target.suffix not in ALLOWED_SUFFIX:
        raise ValueError(f"只允许执行 {'/'.join(sorted(ALLOWED_SUFFIX))} 脚本")
    return target


def read_file(skill: Skill, rel: str, max_chars: int = 6000) -> str:
    """读技能目录里的**附加文件**（``references/xxx.md`` 这类）。

    技能是「渐进披露」的：``SKILL.md`` 只写主线，细化内容放附属文件里，用到哪块
    再读哪块。没有这个入口的话，SKILL.md 里那句「读取 references/forum-prose.md」
    就成了一句做不到的话——模型只能瞎猜，或者跑去抓 GitHub。

    只读文本后缀；路径必须落在技能目录内（见 :func:`_resolve_member`）。
    """
    target = _resolve_member(skill, rel)
    if target.suffix.lower() not in READABLE_SUFFIX:
        raise ValueError(
            f"只能读文本文件（{' / '.join(sorted(READABLE_SUFFIX))}），不能读 {target.suffix or '无后缀'}"
        )
    limit = max(200, int(max_chars))
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n…（文件过长，只取了前 {limit} 字）"
    return text


def _command(target: Path) -> list[str]:
    # 不要用 ``-I``（隔离模式）：它会连 PYTHONIOENCODING 一起忽略，Windows 上子进程
    # 就用 GBK 输出，中文全是乱码。``-B`` 只禁字节码缓存，够用。
    if target.suffix == ".py":
        return [sys.executable, "-B", str(target)]
    return ["sh", str(target)]


def _decode(raw: bytes) -> str:
    """子进程输出解码：先当 UTF-8，不行按 GB18030 兜底（Windows / 老脚本常见）。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _minimal_env() -> dict:
    """最小环境变量集：不把 API Key、数据库路径这些东西漏给技能脚本。

    ``HOME`` / ``TMPDIR`` 故意指到系统临时目录而不是技能目录——脚本随手写临时文件
    不该落在「只读」的技能根目录里，否则每次运行都要被守卫还原一遍。
    """
    tmp = tempfile.gettempdir()
    env = {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "HOME": tmp,
        "TMPDIR": tmp,
        "TEMP": tmp,
        "TMP": tmp,
    }
    return env


def _snapshot(root: Path) -> Optional[dict[str, bytes]]:
    """把技能根目录下的文件读进内存，供脚本跑完后比对。

    返回 ``None`` 表示「太大了，不比对」——宁可漏检也不能把进程内存吃满。
    """
    root = Path(root)
    if not root.is_dir():
        return {}
    snap: dict[str, bytes] = {}
    total = 0
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file())
    except OSError:
        return None
    if len(paths) > SNAPSHOT_MAX_FILES:
        return None
    for path in paths:
        try:
            size = path.stat().st_size
            total += size
            if total > SNAPSHOT_MAX_BYTES:
                return None
            snap[path.relative_to(root).as_posix()] = path.read_bytes()
        except OSError:
            return None
    return snap


def _dirs_of(paths: Iterable[str]) -> set[str]:
    """这些相对路径涉及的所有父目录（用来判断哪个目录是脚本新建的）。"""
    out: set[str] = set()
    for rel in paths:
        parts = Path(rel).parts[:-1]
        for i in range(1, len(parts) + 1):
            out.add(Path(*parts[:i]).as_posix())
    return out


def _looks_like_data_root(root: Path) -> bool:
    """技能根目录里直接躺着数据库文件？

    说明 ``llm.skills_dir`` 指到了 ``data/`` 这种地方——这时「还原技能目录」会把
    数据库一起写回去，那就是真的数据损坏了。宁可放弃守卫也不碰数据库。
    """
    try:
        for path in root.iterdir():
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in {".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"}:
                return True
    except OSError:
        return True
    return False


def revert_changes(root: Path, before: Optional[dict[str, bytes]]) -> list[str]:
    """把脚本对技能目录的改动还原，返回被改动过的相对路径（升序）。

    「QQ 聊天里禁止安装技能」的最后一道闸：模型没有写文件的工具，但脚本是真进程，
    它可以自己往技能目录里塞东西。所以跑完做一次快照比对——

    - 新增的文件：删掉；脚本新建的目录：空了就删；
    - 被改/被删的原文件：按快照内容写回。

    快照为 ``None``（目录太大）时不做任何事，只由调用方按「无法校验」处理。
    """
    if before is None:
        return []
    root = Path(root)
    changes: list[str] = []
    try:
        current = sorted(p for p in root.rglob("*") if p.is_file())
    except OSError:
        return changes
    now: dict[str, bytes] = {}
    for path in current:
        rel = path.relative_to(root).as_posix()
        try:
            now[rel] = path.read_bytes()
        except OSError:
            continue
    # ① 新增的：删掉
    for rel in sorted(set(now) - set(before)):
        try:
            (root / rel).unlink()
            changes.append(rel)
        except OSError as exc:
            logger.warning(f"[skill] 还原失败（删 {rel}）：{exc}")
    # ② 内容变了 / 被删了的：写回原样
    for rel, data in sorted(before.items()):
        if now.get(rel) == data:
            continue
        try:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            changes.append(rel)
        except OSError as exc:
            logger.warning(f"[skill] 还原失败（写 {rel}）：{exc}")
    # ③ 脚本新建的空目录
    added_dirs = _dirs_of(set(now) | set(before)) - _dirs_of(before)
    for rel in sorted(added_dirs, key=len, reverse=True):
        try:
            (root / rel).rmdir()  # 非空会抛，说明里面还有东西，留着
        except OSError:
            pass
    return sorted(set(changes))


async def run_script(
    skill: Skill, script: str, args: Optional[list[str]] = None, timeout: float = 20.0
) -> str:
    """在技能目录里跑一个白名单脚本，返回「退出码 + 输出」文本。

    这是**唯一**会执行代码的路径，几道闸都必须有：

    - 脚本必须已经存在于技能目录内（模型只能选，不能写）；
    - 后缀白名单（``.py`` / ``.sh``）；
    - 参数个数与长度封顶（防止拿参数拼命令）；
    - 独立进程 + 超时 + 输出截断 + 最小环境变量；
    - **技能目录只读**：跑完比对快照，脚本往里塞的东西会被删掉/写回，
      这样「靠脚本装技能」也走不通。
    """
    target = _resolve_script(skill, script)
    argv = [str(a)[:MAX_ARG_CHARS] for a in (args or [])][:MAX_ARGS]
    cmd = [*_command(target), *argv]
    # 守卫范围是整个技能根目录（不只是这个技能），脚本往兄弟目录塞东西也拦得住。
    # 根目录里直接有数据库就不守卫（防止 skills_dir 指错时把库写回去）。
    guard_root = skill.root.parent
    if _looks_like_data_root(guard_root):
        logger.warning(f"[skill] 技能根目录像数据目录（{guard_root}）→ 跳过只读守卫")
        before = None
    else:
        before = _snapshot(guard_root)
    started = asyncio.get_running_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(skill.root),
            env=_minimal_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"启动脚本失败: {exc}") from None

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        revert_changes(guard_root, before)
        raise RuntimeError(f"脚本超时（{timeout:g}s）已被终止") from None

    elapsed = asyncio.get_running_loop().time() - started
    changes = revert_changes(guard_root, before)
    text = _decode(out).strip()
    errtext = _decode(err).strip()
    if errtext:
        text = f"{text}\n[stderr]\n{errtext}".strip()
    if not text:
        text = "（没有输出）"
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT].rstrip() + f"…（输出过长，已截断，共 {len(text)} 字）"
    logger.info(
        f"[skill] 执行 {skill.name}/{script} 参数 {len(argv)} 个 → "
        f"退出码 {proc.returncode}，{elapsed:.1f}s，输出 {len(text)} 字"
    )
    if changes:
        sample = "、".join(changes[:5]) + ("…" if len(changes) > 5 else "")
        logger.warning(
            f"[skill] 脚本试图修改技能目录（{len(changes)} 处）→ 已还原：{sample}"
        )
        text = (
            f"{text}\n[安全] 这个脚本动了技能目录（{sample}），已被还原——"
            "QQ 聊天中不允许安装或修改技能。"
        )
    return f"退出码 {proc.returncode}，耗时 {elapsed:.1f}s\n{text}"

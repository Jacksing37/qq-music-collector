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
"""

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml
from nonebot.log import logger

#: 允许执行的脚本后缀。别加 .exe/.bat——那是「模型决定跑什么」的最短路径
ALLOWED_SUFFIX = {".py", ".sh"}
#: 一次最多传几个参数、单个参数多长（防止模型拿参数往脚本里灌东西）
MAX_ARGS = 8
MAX_ARG_CHARS = 200
#: 脚本输出最多回给模型多少字
MAX_OUTPUT = 4000

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


def _resolve_script(skill: Skill, script: str) -> Path:
    """把 ``script`` 解析成技能目录内的真实路径；越界直接拒绝。"""
    rel = (script or "").strip().lstrip("/\\")
    if not rel:
        raise ValueError("没有指定脚本")
    root = skill.root.resolve()
    target = (root / rel).resolve()
    # startswith 不够：/data/skills/a-evil 也以 /data/skills/a 开头
    if target != root and root not in target.parents:
        raise ValueError(f"脚本不在技能目录内: {script}")
    if target.suffix not in ALLOWED_SUFFIX:
        raise ValueError(f"只允许执行 {'/'.join(sorted(ALLOWED_SUFFIX))} 脚本")
    if not target.is_file():
        raise ValueError(f"脚本不存在: {script}")
    return target


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


def _minimal_env(root: Path) -> dict:
    """最小环境变量集：不把 API Key、数据库路径这些东西漏给技能脚本。"""
    env = {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "HOME": str(root),
        "TMPDIR": str(root),
    }
    return env


async def run_script(
    skill: Skill, script: str, args: Optional[list[str]] = None, timeout: float = 20.0
) -> str:
    """在技能目录里跑一个白名单脚本，返回「退出码 + 输出」文本。

    这是**唯一**会执行代码的路径，几道闸都必须有：

    - 脚本必须已经存在于技能目录内（模型只能选，不能写）；
    - 后缀白名单（``.py`` / ``.sh``）；
    - 参数个数与长度封顶（防止拿参数拼命令）；
    - 独立进程 + 超时 + 输出截断 + 最小环境变量。
    """
    target = _resolve_script(skill, script)
    argv = [str(a)[:MAX_ARG_CHARS] for a in (args or [])][:MAX_ARGS]
    cmd = [*_command(target), *argv]
    started = asyncio.get_running_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(skill.root),
            env=_minimal_env(skill.root),
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
        raise RuntimeError(f"脚本超时（{timeout:g}s）已被终止") from None

    elapsed = asyncio.get_running_loop().time() - started
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
    return f"退出码 {proc.returncode}，耗时 {elapsed:.1f}s\n{text}"

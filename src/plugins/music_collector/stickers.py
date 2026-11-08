"""表情包库：把「想留着以后用」的图收下来，之后按标签挑出来发。

只有两条路，刻意不做全量自动囤积：

- **收**（:func:`add`）：模型看到群友发的表情包、或者自己搜到一张好图，主动调
  ``sticker_add`` 收进来。自动把群里每张图都吞下来的库，两周就会变成几百张
  没人再翻的垃圾堆，而且模型根本不知道里面有什么。
- **发**（:func:`find` + :func:`get`）：模型按情绪 / 内容找，挑一条交给发送层。

存储是**文件 + JSON 索引**（``data/stickers/``），不是塞进 ``collector.db``：
表情包是二进制、还要能手工删 / 手工拷，放在文件系统里直接看得见。

去重按 **sha1**：同一张图被转发十次，库里也只有一条——第二次 :func:`add` 只会把
新标签并进老条目（标签只会越攒越准，不会多存一份）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from nonebot.log import logger

from .config import DATA_DIR
from . import webtool

#: 索引文件格式版本。以后要加字段就升这个号，读到时按版本迁移。
#: v2 = 多了 ``recent``（最近发过哪些）。读老文件时按空列表兜底，不用迁移脚本
INDEX_VERSION = 2
#: 单张图上限。QQ 表情包一般几十到几百 KB，8MB 足够兜住动图
MAX_BYTES = 8 * 1024 * 1024
#: 库容量上限。到顶后淘汰「最久没用过」的，别让一个群把磁盘吃满
MAX_ITEMS = 2000
#: 一条表情包最多几个标签（标签是模型写的，写太多反而搜不准）
MAX_TAGS = 8
#: 单个标签最多几个字
TAG_CHARS = 16
#: 「最近发送」保留多少条。只给网页面板看，不进上下文
RECENT_MAX = 60

#: 抓图用的 UA。和 :mod:`imagelookup` 同一套：空 UA 容易被图床挡
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

#: 只认这几种：QQ 能直接显示，vision 模型也读得了
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF8", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
)

#: 索引读写的进程内互斥。同一个事件循环里可能有两轮对话同时存图
_lock = asyncio.Lock()


# ---------------------------------------------------------------- 路径与索引

def root(sticker_dir: str = "") -> Path:
    """库目录：``llm.sticker_dir`` 没填就用 ``data/stickers``。

    相对路径按 ``data/`` 解析（和 ``skills_dir`` 一个口径）。
    """
    raw = str(sticker_dir or "").strip()
    if not raw:
        return DATA_DIR / "stickers"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (DATA_DIR / path)


def _index_path(base: Path) -> Path:
    return base / "index.json"


def _empty_index() -> dict:
    return {"version": INDEX_VERSION, "items": [], "recent": []}


def _load(base: Path) -> dict:
    """读索引。文件坏了当空库——丢元数据也不能让工具整轮报错。"""
    path = _index_path(base)
    try:
        raw = path.read_text("utf-8")
    except OSError:
        return _empty_index()
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(f"[sticker] 索引不是合法 JSON，已按空库处理：{path}")
        return _empty_index()
    if not isinstance(data, dict):
        return _empty_index()
    for key in ("items", "recent"):
        if not isinstance(data.get(key), list):
            # v1 的索引没有 recent，老仓库读进来就是空列表——不用迁移脚本
            data[key] = []
    data.setdefault("version", INDEX_VERSION)
    return data


def _save(base: Path, data: dict) -> None:
    """原子写：先写 .tmp 再 replace，断电也不会留下半个 JSON。

    写盘时**总是盖上当前版本号**：v1 的老索引被读过一次、再写一次就升成 v2，
    免得「字段已经用上了、version 还写着 1」这种自相矛盾的文件留在磁盘上。
    """
    base.mkdir(parents=True, exist_ok=True)
    data["version"] = INDEX_VERSION
    path = _index_path(base)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(path)


def _items(base: Optional[Path] = None, sticker_dir: str = "") -> list[dict]:
    return list(_load(base or root(sticker_dir)).get("items") or [])


def count(sticker_dir: str = "") -> int:
    """库里有多少条。工具层用它决定要不要注册「找/发」工具。"""
    return len(_items(sticker_dir=sticker_dir))


# ---------------------------------------------------------------- 图片本体

def sniff(data: bytes) -> tuple[str, str]:
    """按文件头判真实格式，返回 ``(扩展名, MIME)``；认不出来返回 ``("", "")``。

    看**字节**而不是 ``Content-Type``：图床经常给 ``application/octet-stream``，
    而把一张 HTML 错误页存成 ``.png`` 是更糟的结果。
    """
    if not data:
        return "", ""
    for magic, ext, mime in _MAGIC:
        if data.startswith(magic):
            return ext, mime
    # WEBP：RIFF....WEBP
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "", ""


def mime_of(data: bytes) -> str:
    """发送时给协议端的 MIME（认不出来按 png 兜底）。"""
    return sniff(data)[1] or "image/png"


async def download(
    url: str,
    timeout: float = 20.0,
    *,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """把一张网络图下下来。**失败抛 ValueError（中文原因）**，由工具层翻成一句话。

    公网校验和 :mod:`webtool` 同一套：群友能让模型发图，等于让它替自己抓 URL，
    内网地址 / 云元数据必须在这里就拦掉（模型是打不通这些地址的，但它会以为能）。
    """
    text = str(url or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        raise ValueError("图片地址要是完整的 http/https 网址")
    # 公网校验放在最前面：模型拦不住自己「顺手抓个内网地址」，这道闸必须比 httpx 早
    blocked = webtool.url_block_reason(text)
    if blocked:
        raise ValueError(blocked)

    try:
        async with httpx.AsyncClient(
            timeout=float(timeout), headers=_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(text)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            data = resp.content
    except httpx.TimeoutException:
        raise ValueError(f"下载图片超时（{timeout:g} 秒）") from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"下载图片失败：HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise ValueError(f"下载图片失败：{exc}") from None

    if not data:
        raise ValueError("这张图是空的")
    if len(data) > max_bytes:
        raise ValueError(f"图片太大（{len(data) // 1024} KB，上限 {max_bytes // 1024} KB）")
    ext, _ = sniff(data)
    if not ext:
        raise ValueError(
            f"这个地址返回的不是图片（{ctype or '未知类型'}）"
            if ctype and not ctype.startswith("image/")
            else "这个地址返回的不是常见图片格式（只认 png/jpg/gif/webp/bmp）"
        )
    return data


# ---------------------------------------------------------------- 标签

_SPLIT = re.compile(r"[\s,，、/|;；+]+")


def parse_tags(raw) -> list[str]:
    """把模型给的标签（字符串或数组）规整成去重、限量的小写外列表。

    **不做大小写折叠**：中文没有大小写，英文标签保持原样更好读；匹配时统一小写比较。
    """
    if isinstance(raw, str):
        parts = _SPLIT.split(raw)
    elif isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            parts.extend(_SPLIT.split(str(item)))
    else:
        parts = []
    out: list[str] = []
    for part in parts:
        tag = " ".join(str(part).split())[:TAG_CHARS].strip()
        if tag and tag.lower() not in (t.lower() for t in out):
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


# ---------------------------------------------------------------- 收

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _merge_tags(old: list[str], new: list[str]) -> list[str]:
    merged = list(old or [])
    for tag in new or []:
        if tag.lower() not in (t.lower() for t in merged):
            merged.append(tag)
    return merged[:MAX_TAGS]


def _evict(base: Path, data: dict) -> int:
    """超出容量就淘汰最久没用的，返回淘汰条数。"""
    items = data.get("items") or []
    if len(items) <= MAX_ITEMS:
        return 0
    items.sort(key=lambda it: (str(it.get("last_used") or ""), str(it.get("created") or "")))
    drop = len(items) - MAX_ITEMS
    for item in items[:drop]:
        path = base / str(item.get("file") or "")
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            logger.debug(f"[sticker] 淘汰时删文件失败 {path}: {exc}")
    data["items"] = items[drop:]
    logger.info(f"[sticker] 库已达上限，淘汰最久没用的 {drop} 条")
    return drop


async def add(
    data: bytes,
    *,
    tags,
    note: str = "",
    source: str = "",
    group: int = 0,
    kind: str = "",
    sticker_dir: str = "",
) -> tuple[Optional[dict], bool]:
    """收一张图，返回 ``(条目, 是否新建)``。同一张图（sha1 相同）只会有一条。"""
    ext, _ = sniff(data)
    if not data or not ext:
        return None, False
    tag_list = parse_tags(tags)
    if not tag_list:
        return None, False
    if len(data) > MAX_BYTES:
        return None, False

    digest = hashlib.sha1(data).hexdigest()
    base = root(sticker_dir)
    async with _lock:
        index = _load(base)
        items = index.setdefault("items", [])
        for item in items:
            if item.get("sha1") == digest:
                # 已经收过：只把新标签并进去（标签越多越好搜），文件不必重写
                item["tags"] = _merge_tags(item.get("tags") or [], tag_list)
                if note and not item.get("note"):
                    item["note"] = str(note)[:80]
                _save(base, index)
                logger.info(
                    f"[sticker] 已在库里（{item.get('id')}），并入标签 {tag_list}"
                )
                return item, False

        base.mkdir(parents=True, exist_ok=True)
        sid = digest[:8]
        while any(it.get("id") == sid for it in items):
            sid = digest[: len(sid) + 2]
        filename = f"{sid}.{ext}"
        (base / filename).write_bytes(data)
        entry = {
            "id": sid,
            "file": filename,
            "sha1": digest,
            "tags": tag_list,
            "note": str(note or "")[:80],
            "source": str(source or "")[:200],
            "group": int(group or 0),
            "kind": str(kind or ""),
            "bytes": len(data),
            "created": _now(),
            "used": 0,
            "last_used": "",
        }
        items.append(entry)
        _evict(base, index)
        _save(base, index)
    logger.info(
        f"[sticker] 收下一张新表情包 {sid}（{len(data) // 1024} KB，标签 {tag_list}）"
    )
    return entry, True


# ---------------------------------------------------------------- 找 / 发

def get(sid: str, sticker_dir: str = "") -> Optional[dict]:
    """按 id 取一条；取不到返回 ``None``。"""
    key = str(sid or "").strip().lower()
    if not key:
        return None
    for item in _items(sticker_dir=sticker_dir):
        if str(item.get("id") or "").lower() == key:
            return item
    return None


def path_of(entry: dict, sticker_dir: str = "") -> Optional[Path]:
    """条目对应的图片文件路径（文件不在了返回 ``None``——手工删过就得当没有）。"""
    name = str((entry or {}).get("file") or "")
    if not name:
        return None
    path = root(sticker_dir) / name
    return path if path.is_file() else None


def _tokens(query: str) -> list[str]:
    return [t.lower() for t in _SPLIT.split(str(query or "").strip()) if t]


def find(query: str = "", limit: int = 5, sticker_dir: str = "") -> list[dict]:
    """按标签 / 备注找，返回得分最高的前几条。

    打分很简单，但够用：标签全等 4 分、标签互相包含 2 分、备注包含 1 分，
    同分时「用得多的」优先（用得多的通常更好笑 / 更常用）。
    """
    items = _items(sticker_dir=sticker_dir)
    want = max(1, int(limit))
    toks = _tokens(query)
    if not toks:
        items.sort(key=lambda it: (-int(it.get("used") or 0), str(it.get("created") or "")))
        return items[:want]

    scored: list[tuple[int, dict]] = []
    for item in items:
        tags = [str(t).lower() for t in (item.get("tags") or [])]
        note = str(item.get("note") or "").lower()
        score = 0
        for tok in toks:
            if any(tok == tag for tag in tags):
                score += 4
            elif any(tok in tag or tag in tok for tag in tags if tag):
                score += 2
            if tok and tok in note:
                score += 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], -int(pair[1].get("used") or 0)))
    return [item for _, item in scored[:want]]


def tag_summary(limit: int = 24, sticker_dir: str = "") -> str:
    """库里现有标签的一句话概览（模型靠它知道「能发些什么」）。"""
    counter: dict[str, int] = {}
    for item in _items(sticker_dir=sticker_dir):
        for tag in item.get("tags") or []:
            key = str(tag)
            counter[key] = counter.get(key, 0) + 1
    if not counter:
        return ""
    ordered = sorted(counter.items(), key=lambda kv: -kv[1])[: max(1, int(limit))]
    return " / ".join(f"{tag}×{n}" for tag, n in ordered)


async def bump_use(sid: str, sticker_dir: str = "", group: int = 0) -> None:
    """记一次「发过了」：次数 +1，并往「最近发送」里压一条（网页面板要显示）。

    失败只写 debug 日志：记账不该影响发图。
    """
    base = root(sticker_dir)
    key = str(sid or "").strip()
    if not key:
        return
    async with _lock:
        index = _load(base)
        for item in index.get("items") or []:
            if str(item.get("id") or "") != key:
                continue
            at = _now()
            item["used"] = int(item.get("used") or 0) + 1
            item["last_used"] = at
            recent = index.setdefault("recent", [])
            recent.append({"id": key, "at": at, "group": int(group or 0)})
            # 只留最近 RECENT_MAX 条：这是给人看的历史，不是审计日志
            if len(recent) > RECENT_MAX:
                del recent[: len(recent) - RECENT_MAX]
            try:
                _save(base, index)
            except OSError as exc:  # noqa: BLE001
                logger.debug(f"[sticker] 记使用次数失败: {exc}")
            return


# ---------------------------------------------------------------- 网页面板用

def recent(limit: int = 20, sticker_dir: str = "") -> list[dict]:
    """最近发过的表情包，**新的在前**。每行带上那条表情包的当前信息。

    为什么不只存 id + 时间：面板要显示缩略图和标签，前端不该为每条再请求一次索引。
    """
    index = _load(root(sticker_dir))
    by_id = {str(it.get("id") or ""): it for it in index.get("items") or []}
    out: list[dict] = []
    for row in reversed(index.get("recent") or []):
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "")
        item = by_id.get(sid)
        out.append(
            {
                "id": sid,
                "at": str(row.get("at") or ""),
                "group": int(row.get("group") or 0),
                # 库里已经删掉的那条也要显示（「我明明发过」比「查无此项」少误会）
                "exists": item is not None,
                "tags": list((item or {}).get("tags") or []),
                "note": str((item or {}).get("note") or ""),
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


async def update(
    sid: str,
    *,
    tags=None,
    note: Optional[str] = None,
    sticker_dir: str = "",
) -> tuple[bool, str]:
    """改一条的标签 / 备注。返回 ``(是否成功, 说明)``。

    ``tags`` 传 ``None`` = 不动标签；传空串 = 明确清空（会拒绝，空标签的表情包谁也搜不到）。
    手工改标签是有意义的：模型写标签时只能看文字描述，人看一眼图就知道该叫什么。
    """
    key = str(sid or "").strip()
    base = root(sticker_dir)
    async with _lock:
        index = _load(base)
        for item in index.get("items") or []:
            if str(item.get("id") or "") != key:
                continue
            if tags is not None:
                parsed = parse_tags(tags)
                if not parsed:
                    return False, "标签不能为空（空了以后模型就搜不到了）"
                item["tags"] = parsed
            if note is not None:
                item["note"] = str(note or "").strip()[:80]
            try:
                _save(base, index)
            except OSError as exc:  # noqa: BLE001
                return False, f"写索引失败: {exc}"
            return True, "已保存"
    return False, "库里没有这条表情包"


async def remove(sid: str, sticker_dir: str = "") -> tuple[bool, str]:
    """删一条：图片文件 + 索引里的记录。发送历史保留（它确实发过）。"""
    key = str(sid or "").strip()
    base = root(sticker_dir)
    async with _lock:
        index = _load(base)
        items = index.get("items") or []
        for pos, item in enumerate(items):
            if str(item.get("id") or "") != key:
                continue
            path = base / str(item.get("file") or "")
            try:
                if path.is_file():
                    path.unlink()
            except OSError as exc:  # noqa: BLE001
                logger.debug(f"[sticker] 删文件失败 {path}: {exc}")
            del items[pos]
            try:
                _save(base, index)
            except OSError as exc:  # noqa: BLE001
                return False, f"写索引失败: {exc}"
            logger.info(f"[sticker] 面板删除一条表情包 {key}")
            return True, "已删除"
    return False, "库里没有这条表情包"


def stats(sticker_dir: str = "") -> dict:
    """面板顶部那几个数字 + 标签分布。"""
    index = _load(root(sticker_dir))
    items = [it for it in index.get("items") or [] if isinstance(it, dict)]
    counter: dict[str, int] = {}
    total_bytes = 0
    for item in items:
        total_bytes += int(item.get("bytes") or 0)
        for tag in item.get("tags") or []:
            counter[str(tag)] = counter.get(str(tag), 0) + 1
    tags = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "root": str(root(sticker_dir)),
        "count": len(items),
        "bytes": total_bytes,
        "sent": sum(int(it.get("used") or 0) for it in items),
        "recent_count": len(index.get("recent") or []),
        "tags": [{"tag": tag, "n": n} for tag, n in tags],
        "max_items": MAX_ITEMS,
    }


# ---------------------------------------------------------------- 给模型看的话

def format_entries(rows: list[dict], *, empty_hint: str = "") -> str:
    """把候选列表拼成给模型看的一段。**带 id**——它要靠 id 调 sticker_send。"""
    if not rows:
        return empty_hint or "库里没有匹配的表情包。"
    lines = [f"库里匹配到 {len(rows)} 条（挑一条，用 sticker_send 发 id）："]
    for index, item in enumerate(rows, 1):
        tags = "、".join(item.get("tags") or []) or "（无标签）"
        note = str(item.get("note") or "").strip()
        used = int(item.get("used") or 0)
        tail = f"｜备注：{note}" if note else ""
        lines.append(f"{index}. id={item.get('id')} 标签：{tags}{tail}｜发过 {used} 次")
    lines.append("要发就用 sticker_send(id=…)。没有一条对得上就别硬发，换成文字。")
    return "\n".join(lines)


def format_saved(entry: dict, created: bool) -> str:
    """收下之后回给模型的一句话。"""
    tags = "、".join(entry.get("tags") or [])
    if created:
        return (
            f"已收进表情包库：id={entry.get('id')}，标签：{tags}。"
            "以后用 sticker_find 按情绪/内容就能找到它（sticker_send 发出去）。"
        )
    return (
        f"这张图库里已经有了（id={entry.get('id')}），新标签已并进去：{tags}。"
        "不用再收一次。"
    )

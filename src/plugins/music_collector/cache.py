"""缓存清理：榜单长图 + 封面缓存的自动回收。

两条规则同时生效，取并集删除：
- 超过 ``keep_days`` 天未修改的文件
- 保留最新 ``max_files`` 个之外的文件（按修改时间倒序）

清理时机：机器人启动、每日定点、每次渲染完成之后。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from nonebot.log import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("music_collector.cache")


@dataclass
class CleanResult:
    removed: int = 0
    freed_bytes: int = 0
    kept: int = 0
    errors: int = 0

    @property
    def freed_mb(self) -> float:
        return round(self.freed_bytes / 1024 / 1024, 2)

    def text(self) -> str:
        if self.removed == 0:
            return f"缓存无需清理，当前保留 {self.kept} 个文件"
        return (
            f"已清理 {self.removed} 个缓存文件，释放 {self.freed_mb} MB，"
            f"保留 {self.kept} 个"
            + (f"（{self.errors} 个删除失败）" if self.errors else "")
        )

    def merge(self, other: "CleanResult") -> "CleanResult":
        return CleanResult(
            removed=self.removed + other.removed,
            freed_bytes=self.freed_bytes + other.freed_bytes,
            kept=self.kept + other.kept,
            errors=self.errors + other.errors,
        )


def _iter_files(directory: Path, patterns: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    if not directory.exists():
        return files
    for pattern in patterns:
        files.extend(p for p in directory.glob(pattern) if p.is_file())
    return files


def clean_dir(
    directory: Path,
    patterns: Iterable[str],
    keep_days: float,
    max_files: int,
) -> CleanResult:
    """清理单个目录。keep_days <= 0 表示不按时间清；max_files <= 0 表示不限个数。"""
    result = CleanResult()
    files = _iter_files(directory, patterns)
    if not files:
        return result

    now = time.time()
    stats: list[tuple[Path, float, int]] = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        stats.append((path, st.st_mtime, st.st_size))

    # 新的排前面
    stats.sort(key=lambda item: item[1], reverse=True)

    doomed: set[Path] = set()
    if keep_days and keep_days > 0:
        deadline = now - keep_days * 86400
        doomed.update(p for p, mtime, _ in stats if mtime < deadline)
    if max_files and max_files > 0:
        doomed.update(p for p, _, _ in stats[max_files:])

    for path, _, size in stats:
        if path not in doomed:
            result.kept += 1
            continue
        try:
            path.unlink()
            result.removed += 1
            result.freed_bytes += size
        except OSError as exc:
            result.errors += 1
            logger.debug(f"[music] 删除缓存失败 {path}: {exc}")
    return result


def clean_caches(
    cache_dir: Path,
    keep_days: float,
    max_render_files: int,
    max_cover_files: int,
) -> CleanResult:
    """清理封面缓存（``*.img``）与榜单长图（``render/*.png``）。"""
    covers = clean_dir(cache_dir, ("*.img",), keep_days, max_cover_files)
    renders = clean_dir(cache_dir / "render", ("*.png", "*.jpg"), keep_days, max_render_files)
    total = covers.merge(renders)
    if total.removed:
        logger.info(f"[music] {total.text()}")
    return total

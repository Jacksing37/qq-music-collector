"""项目清理脚本：一键回收运行时产物。

用法（在项目根目录执行）：
    ./.venv/Scripts/python.exe scripts/cleanup.py            # 预览，不删任何东西
    ./.venv/Scripts/python.exe scripts/cleanup.py --yes      # 真正执行
    ./.venv/Scripts/python.exe scripts/cleanup.py --yes --db # 连同 collector.db 一起清空

清理范围（全部是可重建的产物，不碰源码与配置）：
- data/cache/*.img          封面图缓存
- data/cache/render/*.png   榜单长图
- data/sample/*.png         测试渲染样例
- **/__pycache__            Python 字节码
- *.log                     日志

collector.db 与 netease_session.json 属于状态数据，默认保留；
前者加 --db 才删，后者永远不动（删了要重新扫码登录）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (相对路径 glob, 描述)
FILE_TARGETS: list[tuple[str, str]] = [
    ("data/cache/*.img", "封面缓存"),
    ("data/cache/render/*.png", "榜单长图"),
    ("data/cache/render/*.jpg", "榜单长图"),
    ("data/sample/*.png", "测试样例图"),
    ("*.log", "日志"),
    ("logs/*.log", "日志"),
]

DIR_TARGETS: list[tuple[str, str]] = [
    ("**/__pycache__", "字节码缓存"),
]

# 无论如何都不能删的东西，防手滑
PROTECTED = {
    ROOT / "data" / "netease_session.json",
    ROOT / "data" / "config.yaml",
    ROOT / ".env",
}


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024.0
    return f"{size:.1f}GB"


def _collect_files(include_db: bool) -> list[tuple[Path, str, int]]:
    found: list[tuple[Path, str, int]] = []
    seen: set[Path] = set()
    for pattern, label in FILE_TARGETS:
        for path in ROOT.glob(pattern):
            if not path.is_file() or path in seen or path in PROTECTED:
                continue
            if ".venv" in path.parts:
                continue
            seen.add(path)
            found.append((path, label, path.stat().st_size))
    if include_db:
        db = ROOT / "data" / "collector.db"
        if db.is_file():
            found.append((db, "歌曲数据库", db.stat().st_size))
    return found


def _collect_dirs() -> list[tuple[Path, str, int]]:
    found: list[tuple[Path, str, int]] = []
    for pattern, label in DIR_TARGETS:
        for path in ROOT.glob(pattern):
            if not path.is_dir() or ".venv" in path.parts:
                continue
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            found.append((path, label, size))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 qq-music-collector 运行时产物")
    parser.add_argument("--yes", action="store_true", help="确认执行删除（不加则只预览）")
    parser.add_argument("--db", action="store_true", help="同时清空 data/collector.db")
    args = parser.parse_args()

    files = _collect_files(args.db)
    dirs = _collect_dirs()

    if not files and not dirs:
        print("干净得很，没有需要清理的产物。")
        return 0

    total = sum(size for _, _, size in files) + sum(size for _, _, size in dirs)
    grouped: dict[str, list[int]] = {}
    for _, label, size in files + dirs:
        grouped.setdefault(label, []).append(size)

    print(f"{'[预览]' if not args.yes else '[执行]'} 待清理 {len(files) + len(dirs)} 项，共 {_human(total)}")
    for label, sizes in sorted(grouped.items(), key=lambda kv: -sum(kv[1])):
        print(f"  - {label:<12} {len(sizes):>3} 项  {_human(sum(sizes))}")

    if not args.yes:
        print("\n以上为预览。确认无误后加 --yes 真正执行。")
        return 0

    removed = 0
    freed = 0
    errors = 0
    for path, _, size in files:
        try:
            path.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            errors += 1
            print(f"  ! 删除失败 {path.relative_to(ROOT)}: {exc}", file=sys.stderr)
    for path, _, size in dirs:
        try:
            shutil.rmtree(path)
            removed += 1
            freed += size
        except OSError as exc:
            errors += 1
            print(f"  ! 删除失败 {path.relative_to(ROOT)}: {exc}", file=sys.stderr)

    print(f"\n完成：清理 {removed} 项，释放 {_human(freed)}" + (f"，{errors} 项失败" if errors else ""))
    if args.db:
        print("collector.db 已删除，下次启动会自动建空库。")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

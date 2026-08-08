"""一键跑完全部回归脚本，汇总通过 / 失败。

用法（项目根目录执行）：
    ./.venv/Scripts/python.exe tests/run_all.py           # 全跑
    ./.venv/Scripts/python.exe tests/run_all.py naming    # 只跑名字含 naming 的
    ./.venv/Scripts/python.exe tests/run_all.py -v        # 失败时打印完整输出

每个脚本都在独立子进程里跑：这些测试各自 nonebot.init(...) 并建临时库，
同进程串跑会互相污染全局状态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent

# 排在前面的先跑（越基础越靠前），未列出的按文件名追加在后面
PREFERRED_ORDER = [
    "test_textutil.py",
    "test_naming.py",
    "test_config_compat.py",
    "test_offline.py",
    "test_features.py",
    "test_clear.py",
    "test_description.py",
    "test_start_and_unmatched.py",
    "test_card_fallback.py",
    "test_schedule_dedup.py",
    "smoke_commands.py",
]


def discover(keyword: str | None) -> list[Path]:
    found = {p.name: p for p in TESTS_DIR.glob("*.py") if p.name != "run_all.py"}
    ordered: list[Path] = []
    for name in PREFERRED_ORDER:
        if name in found:
            ordered.append(found.pop(name))
    ordered.extend(found[name] for name in sorted(found))
    if keyword:
        ordered = [p for p in ordered if keyword.lower() in p.name.lower()]
    return ordered


def main() -> int:
    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args or "--verbose" in args
    keyword = next((a for a in args if not a.startswith("-")), None)

    scripts = discover(keyword)
    if not scripts:
        print(f"没找到匹配的测试脚本{f'（关键词 {keyword}）' if keyword else ''}")
        return 1

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    passed: list[str] = []
    failed: list[tuple[str, str]] = []

    print(f"共 {len(scripts)} 个脚本，解释器 {sys.executable}\n" + "-" * 56)
    started = time.perf_counter()

    for script in scripts:
        label = script.name
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        cost = time.perf_counter() - t0
        if proc.returncode == 0:
            passed.append(label)
            print(f"  PASS  {label:<32} {cost:5.1f}s")
        else:
            output = (proc.stdout or "") + (proc.stderr or "")
            failed.append((label, output))
            print(f"  FAIL  {label:<32} {cost:5.1f}s  (exit {proc.returncode})")
            tail = [ln for ln in output.strip().splitlines() if ln.strip()]
            for line in (tail if verbose else tail[-8:]):
                print(f"        | {line}")

    total = time.perf_counter() - started
    print("-" * 56)
    print(f"通过 {len(passed)} / {len(scripts)}，耗时 {total:.1f}s")
    if failed:
        print("失败脚本: " + ", ".join(name for name, _ in failed))
        print("加 -v 可看完整输出")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

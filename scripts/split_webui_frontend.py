"""一次性脚本：把 webui.py 末尾的 DASHBOARD_HTML / ALIASES_HTML 抽到 webui_frontend.py。

运行：python scripts/split_webui_frontend.py
"""
import pathlib

BASE = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins" / "music_collector"
src_path = BASE / "webui.py"
out_path = BASE / "webui_frontend.py"

lines = src_path.read_text(encoding="utf-8").splitlines(keepends=True)

# 定位 DASHBOARD_HTML 块起点
di = next(i for i, l in enumerate(lines) if l.startswith('DASHBOARD_HTML = r"""'))
# DASHBOARD 块终点：其后第一个仅含 """ 的行
dj = next(i for i in range(di + 1, len(lines)) if lines[i].strip() == '"""')
# ALIASES_HTML 块起点
ai = next(i for i, l in enumerate(lines) if l.startswith('ALIASES_HTML = r"""'))
# 文件最后一行即为 ALIASES 的结束 """（含换行）
aj = len(lines) - 1

dash_block = "".join(lines[di:dj + 1])
ali_block = "".join(lines[ai:aj + 1])

out_path.write_text(dash_block + "\n\n\n" + ali_block + "\n", encoding="utf-8")

# 重写 webui.py：删除两个 HTML 常量块，改从 webui_frontend 导入
new_lines = lines[:di] + [
    "from .webui_frontend import DASHBOARD_HTML, ALIASES_HTML\n",
] + lines[aj + 1:]
src_path.write_text("".join(new_lines), encoding="utf-8")

print(f"DASHBOARD block lines: {dj - di + 1}")
print(f"ALIASES block lines: {aj - ai + 1}")
print("written:", out_path)

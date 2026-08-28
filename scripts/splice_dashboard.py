"""一次性脚本：用 scripts/_new_dashboard.html 替换 webui_frontend.py 里的 DASHBOARD_HTML 块。"""
import pathlib

BASE = pathlib.Path(__file__).resolve().parents[1] / "src" / "plugins" / "music_collector"
fe_path = BASE / "webui_frontend.py"
new_html = pathlib.Path(__file__).resolve().parent / "_new_dashboard.html"

lines = fe_path.read_text(encoding="utf-8").splitlines(keepends=True)
di = next(i for i, l in enumerate(lines) if l.startswith('DASHBOARD_HTML = r"""'))
dj = next(i for i in range(di + 1, len(lines)) if lines[i].strip() == '"""')
# 找到 ALIASES_HTML 起点，保留其后所有内容
ai = next(i for i, l in enumerate(lines) if l.startswith('ALIASES_HTML = r"""'))

content = new_html.read_text(encoding="utf-8")
# 确保结尾有换行
if not content.endswith("\n"):
    content += "\n"

new_block = ['DASHBOARD_HTML = r"""\n', content, '"""\n\n\n'] + lines[ai:]

fe_path.write_text("".join(new_block), encoding="utf-8")
print(f"replaced DASHBOARD block (old {dj-di+1} lines) -> new {len(content.splitlines())} lines")
print(f"kept ALIASES from line {ai}")

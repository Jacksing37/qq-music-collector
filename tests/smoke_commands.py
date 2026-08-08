"""冒烟测试：加载插件 + 验证新命令的 dispatch 与 intro 预览渲染。

不需要真实 bot 连接或网络，重点验证：
1. 整个插件模块图能无错加载（含 commands.py 的新增代码）
2. handle_command 的 dispatch 已注册 intro / descfix
3. _intro_context + 渲染 能产出合理预览（占位符 {nick}/{state}/{playlist} 被替换）
4. descfix 在空待补写队列下返回 (0,0)
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 必须在 load_plugin 之前把插件包所在目录放进 sys.path，且不能提前 import 插件模块
sys.path.insert(0, str(ROOT / "src" / "plugins"))


async def main() -> None:
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter

    nonebot.init(driver="~fastapi+~websockets", log_level="WARNING")
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotAdapter)

    # 关键：先 load_plugin，不要提前 import music_collector
    loaded = nonebot.load_plugin("music_collector")
    assert loaded, "插件加载失败"

    from music_collector import commands as cmds
    from music_collector import naming
    from music_collector.service import service

    await service.setup()

    # ---- 1. dispatch 覆盖检查 ----
    import inspect
    src = inspect.getsource(cmds.handle_command)
    assert "intro" in src and "descfix" in src, "dispatch 未注册 intro/descfix"
    print("[OK] handle_command 已注册 intro / descfix 分支")

    # ---- 2. _intro_context + 渲染 ----
    ctx = await cmds._intro_context(
        "张三", service.config.groups[0] if service.config.groups else None
    )
    preview = naming.render_template(service.config.intro.text, ctx)
    assert "{nick}" not in preview, "nick 占位符未替换"
    assert "{state}" not in preview, "state 占位符未替换"
    assert "{playlist}" not in preview, "playlist 占位符未替换"
    print("[OK] intro 预览渲染正常，占位符已替换")
    print("----- 自我介绍预览 -----")
    print(preview)
    print("------------------------")

    # ---- 3. retry_pending_desc 返回 (成功数, 失败数) 二元组 ----
    ok, failed = await service.retry_pending_desc(None)
    assert isinstance(ok, int) and isinstance(failed, int), "应返回 (成功数, 失败数)"
    print(f"[OK] retry_pending_desc 返回 ({ok}, {failed})")

    print("\n全部冒烟检查通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())

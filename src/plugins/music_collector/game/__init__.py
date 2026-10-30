"""猜歌小游戏：猜「每首歌是谁分享的」。

模块分工：

- ``store.py``    白名单 / 验证码 / 登录态三张表（与收集主库同库，但互不干扰）
- ``auth.py``     白名单校验 + 机器人私聊验证码
- ``rooms.py``    房间与对局状态机（纯内存，不含 IO，也不 import nonebot）
- ``bridge.py``   与收集主流程的桥：取历史窗口的题、现场解析、归档
- ``router.py``   HTTP / WebSocket 接口与生命周期
- ``page.py``     玩家页面 + 名单管理页
"""

from .router import register_game, setup
from . import handler as _handler  # noqa: F401  私聊「登录」→ 回验证码（matcher 在 import 时注册）

__all__ = ["setup", "register_game"]

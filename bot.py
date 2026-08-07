"""NoneBot2 启动入口。

运行前请确认：
1. 已复制 .env.example 为 .env 并按需修改
2. NapCat（或其他 OneBot v11 实现）已配置反向 WebSocket 指向本进程
"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    nonebot.run()

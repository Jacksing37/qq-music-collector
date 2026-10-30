"""私聊「登录」自动回验证码。

为什么需要这一条：协议端（NapCat）对**非好友**拒绝机器人主动发私聊——
实测两条路都是 `retcode=1200 EventChecker Failed`，群设置里「允许发起临时会话」
也救不了。但只要**玩家先开口**，会话就已经建立，机器人回一条是能到的。

所以给玩家两条路：

1. 网页点「发验证码」（机器人主动发，**要求已加好友**）；
2. 在 QQ 里私聊机器人发「登录」（对方先发起，**不必加好友**），机器人直接在
   这个会话里回码，玩家拿回网页填。

第 2 条就是本模块。关键词故意收窄（登录/登入/登陆/验证码/猜歌/login），
不发关键词就不理——私聊里"嗯""好的"这种不该收到一条验证码。
"""

from __future__ import annotations

from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from ..config import config_manager

#: 触发词。都做小写比较，英文也认。
_KEYWORDS = ("登录", "登陆", "登入", "验证码", "猜歌", "login")


def _is_login_text(text: str) -> bool:
    return any(word in (text or "").strip().lower() for word in _KEYWORDS)


def _plain(event: Event) -> str:
    try:
        return event.get_plaintext().strip().lower()
    except Exception:  # noqa: BLE001 - 拿不到纯文本就不接这条
        return ""


def _is_login_request(event: Event) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    if not config_manager.config.game.enabled:
        return False
    return _is_login_text(_plain(event))


#: priority=4：比日志旁听（1/2）靠后，比音乐消息（99）靠前。
#: block=True：命中就别让后面的流程再处理这条私聊。
login_request = on_message(rule=Rule(_is_login_request), priority=4, block=True)


@login_request.handle()
async def _send_code_back(bot: Bot, event: PrivateMessageEvent) -> None:
    from . import router

    if router.auth is None:  # 插件还没启动完成
        await login_request.finish("机器人还在启动，稍等几秒再试")
        return
    qq = int(event.user_id)
    ok, code, ttl, reason = await router.auth.issue_code(qq)
    if not ok:
        await login_request.finish(reason)
        return
    logger.info(f"[music/guess] 私聊发码 {qq}")
    # 关键：用 finish → 在**当前会话**里回复，而不是 send_private_msg 另起一条
    await login_request.finish(router.auth.code_text(code, ttl))

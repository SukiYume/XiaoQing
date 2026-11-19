"""``/qq``：Bot 管理员在私聊里查看和操作小青的 QQ。

看某个会话的聊天记录、以小青的身份去某个会话说话，用的是聊天插件自己的记录：看到的就是她当时
看到的（图片带着识图描述），说出去的话照常记成她的发言。群列表、群成员、撤回和其他 OneBot 动作
走 Core 的 ``onebot_actions`` 能力。谁能用、在哪里用由 plugin.json 声明，Core 在调用前检查。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from core.args import parse_int
from core.delivery import CommandOutcome, DeliverySegments, send_with_receipt
from core.log import LogEvent
from core.plugin_base import bounded_external_text, build_action, segments, text
from core.router import format_command_catalog, resolve_context_command_invocation

from .chat_event import other_mentions
from .chat_history import image_parts, message_body, message_stamp, sender_label
from .chat_runtime import configured_bot_name, load_runtime, optional_capability
from .handler_context import handle_errors
from .memory.memory import StoredMessage
from .store_binding import bound_state
from .turn_delivery import spoken_line_receipt

log = logging.getLogger(__name__)

Reply   = list[dict[str, Any]]
Handler = Callable[[str, dict[str, Any], Any, str], Awaitable[Reply]]

_COMMAND_ROOT = "xiaoqing_chat.qq"
# 会话写法和聊天记录、办事会话一致：g<群号> 是群，u<QQ号> 是私聊。
_CHAT_RE       = re.compile(r"([gu])(\d{5,20})")
_GROUP_RE      = re.compile(r"g?(\d{5,20})")
_MESSAGE_ID_RE = re.compile(r"-?\d{1,20}")
_CQ_AT_RE      = re.compile(r"\[CQ:at,qq=(\d{1,20}|all)\]")
_DEFAULT_LOOK  = 20
_MAX_LOOK      = 100
_MEMBER_LINES  = 60
_NAME_CHARS    = 64
# 原始响应交回的字数上限：成员列表这类大响应用专门的子命令看。
_RESPONSE_CHARS = 6000
_ROLE_LABELS    = {"owner": "群主", "admin": "管理员"}
# 这几个动作是在说话：经 say 发出才会记进那个会话的聊天记录。
_SPEAK_ACTIONS = frozenset({"send_msg", "send_group_msg", "send_private_msg"})


def _fail(message: str) -> Reply:
    """没办成的回报：agent 代为执行时据此知道这条命令没有完成。"""

    return DeliverySegments(segments(message), outcome=CommandOutcome("error"))


def _usage(usage: str) -> Reply:
    return _fail(f"❌ 用法: {usage}")


def _name(value: Any) -> str:
    """群名、昵称这类来自 QQ 的文字，去掉控制字符并限长。"""

    return bounded_external_text(value, max_chars=_NAME_CHARS, max_bytes=_NAME_CHARS * 4)


def _chat_target(token: str) -> tuple[str, int] | None:
    """``g<群号>`` / ``u<QQ号>`` 拆成会话类型和号码；写法不对时返回 ``None``。"""

    match = _CHAT_RE.fullmatch(token.strip().casefold())
    return (match.group(1), int(match.group(2))) if match else None


def _failure(response: dict[str, Any] | None) -> str:
    """OneBot 响应没成功时的说明；成功时为空。"""

    if response is None:
        return "没有拿到 OneBot 的响应：连接不可用，或者动作发出后结果未知。"
    if response.get("status") == "ok" and response.get("retcode") == 0:
        return ""
    reason = _name(response.get("wording") or response.get("message") or "")
    return f"OneBot 返回失败（retcode={response.get('retcode')}）" + (
        f"：{reason}" if reason else ""
    )


async def _onebot_data(context: Any, action: str, params: dict[str, Any]) -> tuple[Any, str]:
    """调用一个查询动作，返回 ``(data, 失败说明)``；成功时失败说明为空。"""

    onebot = optional_capability(context, "onebot_actions")
    if onebot is None:
        return None, "OneBot 操作能力不可用。"
    response = await onebot.call(action, params)
    failure  = _failure(response)
    return (None if failure else (response or {}).get("data")), failure


# ──────────────────── 看 ────────────────────


def _message_lines(message: StoredMessage) -> list[str]:
    """一条聊天记录：时间、说话人（QQ 号）、消息 ID 和正文，后面跟着还在本机的图片文件。"""

    ident = f" #{message.message_id}" if message.message_id is not None else ""
    lines = [
        f"[{message_stamp(message)}] {sender_label(message)}{ident}: "
        f"{message_body(message) or '（空）'}"
    ]
    lines.extend(f"  图片文件：{part['file_path']}" for part in image_parts(message))
    return lines


def _render_history(chat_id: str, history: Sequence[StoredMessage], count: int) -> str:
    shown = history[-count:]
    lines = [f"{chat_id} 最近 {len(shown)} 条聊天记录（旧→新，本机共存 {len(history)} 条）："]
    for message in shown:
        lines.extend(_message_lines(message))
    return "\n".join(lines)


async def _look(rest: str, _event: dict[str, Any], context: Any, usage: str) -> Reply:
    words  = rest.split()
    target = _chat_target(words[0]) if words else None
    count = parse_int(words[1], minimum=1, maximum=_MAX_LOOK) if len(words) == 2 else _DEFAULT_LOOK
    if target is None or count is None or len(words) > 2:
        return _usage(usage)
    chat_id = f"{target[0]}{target[1]}"
    history = await bound_state(context).memory_store.get_async(chat_id)
    if not history:
        return segments(f"{chat_id} 没有聊天记录。")
    # 逐张图片确认文件还在要碰磁盘，放到事件循环之外。
    return segments(await asyncio.to_thread(_render_history, chat_id, history, count))


# ──────────────────── 说 ────────────────────


def _with_mentions(content: str, leading: Sequence[str]) -> tuple[list[dict[str, Any]], str]:
    """群里要发出的消息段，以及记进聊天记录的文字（@ 写成 ``@QQ号``）。

    @ 有两个来源：agent 代发时 Core 把命令里的 ``[CQ:at,qq=…]`` 拆成事件里的 @ 段（``leading``，
    放在最前）；管理员自己发命令时它们留在内容里，就地换成 @ 段。
    """

    message: list[dict[str, Any]] = [{"type": "at", "data": {"qq": qq}} for qq in leading]
    spoken                        = "".join(f"@{qq} " for qq in leading)
    for index, piece in enumerate(_CQ_AT_RE.split(content)):
        if index % 2:
            message.append({"type": "at", "data": {"qq": piece}})
            spoken += f"@{piece}"
        elif piece:
            # QQ 里 @ 和后面的字之间有一个空格。
            lead = " " if message and message[-1]["type"] == "at" and not piece[0].isspace() else ""
            message.append(text(lead + piece))
            spoken += piece
    return message, spoken


async def _say(rest: str, event: dict[str, Any], context: Any, usage: str) -> Reply:
    words = rest.strip().split(maxsplit=1)
    target  = _chat_target(words[0]) if words else None
    content = words[1].strip() if len(words) == 2 else ""
    if target is None or not content:
        return _usage(usage)
    kind, number = target
    chat_id = f"{kind}{number}"
    # 只有群里能 @ 人；私聊里内容照字面发出。
    message, spoken = (
        _with_mentions(content, [qq for qq, _name_in_segment in other_mentions(event)])
        if kind == "g"
        else ([text(content)], content)
    )
    action = build_action(
        message,
        user_id  = number if kind == "u" else None,
        group_id = number if kind == "g" else None,
    )
    if action is None:
        return _usage(usage)
    receipt = spoken_line_receipt(
        context,
        load_runtime(context),
        chat_id,
        spoken,
        action    = "requested_message",
        reasoning = "qq_say",
    )
    sent = await send_with_receipt(context.send_action, action, receipt)
    log.info(LogEvent("qq.say", chat_id=chat_id, sent=sent, text=spoken))
    if sent is False:
        return _fail(f"没能发到 {chat_id}。")
    if sent is None:
        return segments(f"已交给发送通道，{chat_id} 那边是否送达未确认；这句话已记进聊天记录。")
    ids   = "、".join(str(item) for item in receipt.message_ids)
    ident = f"（消息 ID {ids}）" if ids else ""
    return segments(f"已以{configured_bot_name(context)}的身份发到 {chat_id}{ident}：{spoken}")


# ──────────────────── QQ 账号 ────────────────────


async def _groups(_rest: str, _event: dict[str, Any], context: Any, _usage_text: str) -> Reply:
    data, failure = await _onebot_data(context, "get_group_list", {})
    if failure:
        return _fail(failure)
    groups = [item for item in data or () if isinstance(item, dict) and item.get("group_id")]
    if not groups:
        return segments("现在没有加入任何群。")
    lines = [f"所在的群（{len(groups)} 个）："]
    for group in groups:
        members = group.get("member_count")
        size    = f"（{members} 人）" if isinstance(members, int) else ""
        lines.append(f"g{group['group_id']} {_name(group.get('group_name')) or '（无群名）'}{size}")
    return segments("\n".join(lines))


def _member_line(member: dict[str, Any]) -> str:
    card, nickname = _name(member.get("card")), _name(member.get("nickname"))
    shown = f"{card}（昵称 {nickname}）" if card and nickname and card != nickname else ""
    role  = _ROLE_LABELS.get(str(member.get("role") or ""), "")
    return f"{member.get('user_id')} {shown or card or nickname or '（无名字）'}" + (
        f" [{role}]" if role else ""
    )


async def _members(rest: str, _event: dict[str, Any], context: Any, usage: str) -> Reply:
    words = rest.strip().split(maxsplit=1)
    match = _GROUP_RE.fullmatch(words[0].casefold()) if words else None
    if match is None:
        return _usage(usage)
    group_id = int(match.group(1))
    keyword  = words[1].strip().casefold() if len(words) == 2 else ""
    data, failure = await _onebot_data(context, "get_group_member_list", {"group_id": group_id})
    if failure:
        return _fail(failure)
    members = [item for item in data or () if isinstance(item, dict) and item.get("user_id")]
    if keyword:
        members = [
            member
            for member in members
            if any(
                keyword in str(member.get(field) or "").casefold()
                for field in ("card", "nickname", "user_id")
            )
        ]
    if not members:
        return segments(f"g{group_id} 里没有找到{'匹配的' if keyword else ''}成员。")
    # 群主和管理员排在前面，其余保持 OneBot 给的顺序。
    members.sort(key=lambda member: str(member.get("role") or "") not in _ROLE_LABELS)
    lines = [f"g{group_id} 的成员（{'匹配 ' if keyword else '共 '}{len(members)} 人）："]
    lines.extend(_member_line(member) for member in members[:_MEMBER_LINES])
    if len(members) > _MEMBER_LINES:
        lines.append(f"只显示前 {_MEMBER_LINES} 人；加关键词可以缩小范围。")
    return segments("\n".join(lines))


async def _recall(rest: str, _event: dict[str, Any], context: Any, usage: str) -> Reply:
    token = rest.strip()
    if _MESSAGE_ID_RE.fullmatch(token) is None:
        return _usage(usage)
    _data, failure = await _onebot_data(context, "delete_msg", {"message_id": int(token)})
    if failure:
        return _fail(f"消息 {token} 没有撤回。{failure}")
    log.info(LogEvent("qq.recall", message_id=int(token)))
    return segments(f"已撤回消息 {token}。")


async def _call(rest: str, _event: dict[str, Any], context: Any, usage: str) -> Reply:
    words = rest.strip().split(maxsplit=1)
    action = words[0] if words else ""
    try:
        params = json.loads(words[1]) if len(words) == 2 else {}
    except ValueError:
        return _usage(usage)
    if not action or not isinstance(params, dict):
        return _usage(usage)
    if action in _SPEAK_ACTIONS:
        return _fail("发消息用 /qq say：经它发出的话会记进那个会话的聊天记录。")
    onebot = optional_capability(context, "onebot_actions")
    if onebot is None:
        return _fail("OneBot 操作能力不可用。")
    try:
        response = await onebot.call(action, params)
    except ValueError:
        return _usage(usage)
    log.info(LogEvent("qq.call", action=action, status=(response or {}).get("status")))
    failure = _failure(response)
    if failure:
        return _fail(f"{action} 没有成功。{failure}")
    body = json.dumps((response or {}).get("data"), ensure_ascii=False)
    if len(body) > _RESPONSE_CHARS:
        body = body[:_RESPONSE_CHARS] + f"…（共 {len(body)} 字符，已截断）"
    return segments(f"{action} 成功，返回数据：\n{body}")


_HANDLERS: dict[str, Handler] = {
    "groups" : _groups,
    "members": _members,
    "look"   : _look,
    "say"    : _say,
    "recall" : _recall,
    "call"   : _call,
}


@handle_errors
async def handle_qq(args: str, event: dict[str, Any], context: Any) -> Reply:
    """``/qq`` 入口：子命令的名称、别名和用法以 manifest 目录为准，这里只绑定处理函数。"""

    invocation = resolve_context_command_invocation(context, _COMMAND_ROOT, args)
    if invocation is None:
        return segments("完整命令目录暂不可用，请使用 /help qq")
    node    = invocation.chain[1] if len(invocation.chain) > 1 else None
    handler = _HANDLERS.get(node.name) if node is not None else None
    if node is None or handler is None:
        title = f"🐧 {configured_bot_name(context)}的 QQ · 完整命令目录"
        return segments(format_command_catalog(invocation.root, title=title))
    return await handler(invocation.remainder_after(1), event, context, node.usage)

"""agent 执行循环：模型选择工具，工具真实执行，直到交付、出错或预算耗尽。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from core.ai import AIError

from .tasks import Outcome
from .tools import Tool, ToolEnv, execute_tool

logger = logging.getLogger(__name__)

AGENT_ROUTE = "agent"
# 模型既不调用工具也不说话时，最多提醒几次。
_MAX_EMPTY_REPLIES = 2
# 上下文超过这个字符数时，把较早的工具输出、调用参数和推理内容压成开头预览，
# 只保留最近几条原文。
_COMPACT_THRESHOLD_CHARS        = 80_000
_KEEP_RECENT_TOOL_MESSAGES      = 8
_KEEP_RECENT_ASSISTANT_MESSAGES = 4
_COMPACTED_PREVIEW_CHARS        = 300
_WRAP_UP_PROMPT                 = (
    "（系统提示）步数或时间预算已经用完。不要再调用工具，直接根据已有结果给发起人一个简短的"
    "最终回复：说清做到了哪一步、得到了什么、还差什么。"
)
_EMPTY_REPLY_PROMPT = "（系统提示）请继续：调用工具推进任务，或用 final_answer 交付结果。"
_NOTES_PREFIX       = "（发起人在任务进行中补充了要求，请按补充调整后续步骤）\n"
_FINAL_DEFERRED     = "没有交付：发起人刚补充了要求（见下一条消息），按补充调整后再调用 final_answer。"
_SKIPPED_FOR_NOTES  = (
    "没有执行：发起人刚补充了要求（见下一条消息）。这一步是按原来的要求安排的，"
    "按新要求重新决定还要不要做。"
)


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id  : str
    name     : str
    arguments: Any


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return ""


def response_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


def parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """只接收结构完整的调用；缺失或重复的 ID 用稳定序号补齐。"""

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[ToolCall] = []
    used : set[str]       = set()
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
            continue
        function = raw["function"]
        name     = str(function.get("name") or "").strip()
        if not name:
            continue
        call_id = str(raw.get("id") or "").strip()
        if not call_id or call_id in used:
            call_id = f"call_{index}_{len(used)}"
        used.add(call_id)
        calls.append(ToolCall(call_id, name, function.get("arguments", "{}")))
    return calls


def _assistant_message(message: dict[str, Any], calls: list[ToolCall]) -> dict[str, Any]:
    entry: dict[str, Any] = {"role": "assistant", "content": _message_content(message)}
    if calls:
        entry["tool_calls"] = [
            {
                "id"      : call.call_id,
                "type"    : "function",
                "function": {
                    "name": call.name,
                    "arguments": (
                        call.arguments
                        if isinstance(call.arguments, str)
                        else json.dumps(call.arguments, ensure_ascii=False)
                    ),
                },
            }
            for call in calls
        ]
    # 思考模式的模型要求同一轮工具调用里原样带回推理内容。
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        entry["reasoning_content"] = reasoning
    return entry


def _message_size(message: dict[str, Any]) -> int:
    """发给模型的字符数：正文、推理内容和工具调用参数都算。"""

    size = len(str(message.get("content") or "")) + len(str(message.get("reasoning_content") or ""))
    calls = message.get("tool_calls")
    for call in calls if isinstance(calls, list) else ():
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            size += len(str(function.get("arguments") or ""))
    return size


def _compact_call(call: Any) -> Any:
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return call
    arguments = str(function.get("arguments") or "")
    if len(arguments) <= _COMPACTED_PREVIEW_CHARS:
        return call
    # 参数仍要是合法 JSON，有的接口会解析历史里的调用参数。
    summary = json.dumps(
        {
            "omitted": f"较早调用的参数已省略，原长 {len(arguments)} 字",
            "preview": arguments[:_COMPACTED_PREVIEW_CHARS],
        },
        ensure_ascii=False,
    )
    return {**call, "function": {**function, "arguments": summary}}


def compact_messages(messages: list[dict[str, Any]]) -> None:
    """上下文过长时把较早的工具输出、调用参数和推理内容换成开头预览。

    保留最近的原文和全部调用结构。压缩的消息换成新字典、不改动原对象，
    执行记录里保存的仍是完整的原始消息。
    """

    total = sum(_message_size(item) for item in messages)
    if total <= _COMPACT_THRESHOLD_CHARS:
        return
    tool_indexes = [index for index, item in enumerate(messages) if item.get("role") == "tool"]
    for index in tool_indexes[:-_KEEP_RECENT_TOOL_MESSAGES]:
        content = str(messages[index].get("content") or "")
        if len(content) <= _COMPACTED_PREVIEW_CHARS:
            continue
        messages[index] = {
            **messages[index],
            "content": content[:_COMPACTED_PREVIEW_CHARS]
            + f"\n…（较早的输出已省略，原长 {len(content)} 字）",
        }
    assistant_indexes = [
        index for index, item in enumerate(messages) if item.get("role") == "assistant"
    ]
    for index in assistant_indexes[:-_KEEP_RECENT_ASSISTANT_MESSAGES]:
        item      = messages[index]
        updated   = dict(item)
        reasoning = str(item.get("reasoning_content") or "")
        if len(reasoning) > _COMPACTED_PREVIEW_CHARS:
            updated["reasoning_content"] = (
                reasoning[:_COMPACTED_PREVIEW_CHARS] + "…（较早的推理已省略）"
            )
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            updated["tool_calls"] = [_compact_call(call) for call in calls]
        messages[index] = updated


def _append(
    messages: list[dict[str, Any]],
    transcript: list[dict[str, Any]] | None,
    message: dict[str, Any],
) -> None:
    messages.append(message)
    if transcript is not None:
        transcript.append(message)


def _take_notes(task: Any) -> str:
    notes = list(task.notes)
    task.notes.clear()
    return _NOTES_PREFIX + "\n".join(notes) if notes else ""


async def _complete(
    env: ToolEnv,
    messages: list[dict[str, Any]],
    tools: dict[str, Tool] | None,
) -> dict[str, Any]:
    ai = env.context.capabilities.ai
    if ai is None:
        raise AIError("AI capability is unavailable")
    result = await ai.complete(
        AGENT_ROUTE,
        messages,
        tools       = [tool.schema() for tool in tools.values()] if tools else None,
        tool_choice = "auto" if tools else None,
    )
    return response_message(result.response)


async def _wrap_up(
    env: ToolEnv,
    messages: list[dict[str, Any]],
    transcript: list[dict[str, Any]] | None,
    reason: str,
) -> Outcome:
    prompt = (
        _WRAP_UP_PROMPT
        if reason.startswith("超出")
        else (
            f"（系统提示）执行已停止：{reason}。根据已有工具回报直接交付结果，明确已完成、后台已接收与尚未完成的部分。"
        )
    )
    _append(messages, transcript, {"role": "user", "content": prompt})
    try:
        reply = await _complete(env, messages, None)
        _append(messages, transcript, _assistant_message(reply, []))
        text = _message_content(reply).strip()
    except (AIError, RuntimeError):
        text = ""
    env.task.error = reason
    fallback       = (
        "这件事没能在预算内做完，最后的结果还没整理出来。"
        if reason.startswith("超出")
        else "我先停在这里，最后的结果还没整理出来。已执行的操作保存在任务记录里。"
    )
    return Outcome(text or fallback, status="failed")


async def run_agent_loop(
    env: ToolEnv,
    tools: dict[str, Tool],
    messages: list[dict[str, Any]],
    *,
    transcript: list[dict[str, Any]] | None = None,
) -> Outcome:
    """推进任务直到 final_answer、模型直接给出回复、出错或预算耗尽。

    ``transcript`` 收到每一条新消息的原样副本，压缩只影响发给模型的 ``messages``。
    """

    task             = env.task
    empty_replies    = 0
    failures         = 0
    blocked_attempts = 0
    stalled_rounds   = 0
    while True:
        if task.steps >= env.config.max_steps:
            return await _wrap_up(env, messages, transcript, "超出步数预算")
        if env.remaining() <= 5:
            return await _wrap_up(env, messages, transcript, "超出时间预算")
        notes = _take_notes(task)
        if notes:
            _append(messages, transcript, {"role": "user", "content": notes})
            env.user_updates.append(notes)
            env.closed_scopes.clear()
            env.observations.clear()
            env.wait_blocked_reason = ""
            failures = blocked_attempts = stalled_rounds = 0
        compact_messages(messages)
        task.steps += 1
        task.activity = "思考下一步"
        try:
            available = tools
            if env.wait_blocked_reason or env.task.command_results:
                available = {name: tool for name, tool in tools.items() if name != "wait"}
            message = await _complete(env, messages, available)
        except (AIError, RuntimeError) as exc:
            logger.warning(
                "xiaoqing_agent model call failed task=%s error_type=%s",
                task.task_id,
                type(exc).__name__,
            )
            task.error = f"模型调用失败: {type(exc).__name__}"
            return Outcome("模型这会儿调不通，任务没能做完，稍后再试试吧。", status="failed")

        calls = parse_tool_calls(message)
        _append(messages, transcript, _assistant_message(message, calls))
        if not calls:
            text = _message_content(message).strip()
            if text and not task.notes:
                return Outcome(text)
            if text:
                continue
            empty_replies += 1
            if empty_replies > _MAX_EMPTY_REPLIES:
                task.error = "模型没有给出结果"
                return Outcome("这件事我没能推进下去，换个说法再试试？", status="failed")
            _append(messages, transcript, {"role": "user", "content": _EMPTY_REPLY_PROMPT})
            continue

        final: Outcome | None = None
        stop_reason           = ""
        progressed            = False
        for index, call in enumerate(calls):
            if final is not None:
                content = "已交付，忽略后续调用"
            elif task.notes:
                # 这一批调用是按旧要求排的：补充一到，还没开始的都先不做，交给模型重排。
                content = _SKIPPED_FOR_NOTES
            elif stop_reason:
                content = "执行已停止，后续调用已跳过：" + stop_reason
            else:
                result  = await execute_tool(tools, env, call.name, call.arguments)
                content = result.content
                final   = result.final
                if (
                    final is not None
                    and result.state != "blocked"
                    and call.name == "xiaoqing_command"
                    and index < len(calls) - 1
                ):
                    # delivery 收尾发生在当前调用批次结束；后面已经安排的请求仍需处理。
                    final = None
                if result.state == "blocked":
                    blocked_attempts += 1
                    failures += 1
                elif result.state == "error":
                    failures += 1
                elif result.progress and call.name not in {
                    "wait",
                    "send_message",
                    "update_plan",
                    "xiaoqing_commands",
                }:
                    failures = blocked_attempts = 0
                    progressed = True
                if blocked_attempts >= 2 or failures >= 3:
                    stop_reason = "工具回报重复或连续失败，当前路径已停止"
                if final is not None and task.notes:
                    # 交付前发起人又补充了要求：先不交付，下一步把补充交给模型。
                    content = _FINAL_DEFERRED
                    final   = None
            _append(
                messages,
                transcript,
                {
                    "role"        : "tool",
                    "tool_call_id": call.call_id,
                    "name"        : call.name,
                    "content"     : content,
                },
            )
        if final is not None:
            return final
        stalled_rounds = stalled_rounds + 1 if env.closed_scopes and not progressed else 0
        if stalled_rounds >= 2:
            stop_reason = "已停止的路径没有取得新进展"
        if stop_reason:
            return await _wrap_up(env, messages, transcript, stop_reason)

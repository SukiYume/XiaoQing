"""Concrete least-privilege services granted to selected built-in plugins."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import materialize_snapshot_value
from .delivery import DeliverySegments

if TYPE_CHECKING:
    from .ai import AICompletionResult, AIModelInfo


def _secret_path_parts(path: str) -> list[str]:
    parts = str(path or "").split(".")
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("invalid secret path")
    return parts


@dataclass(frozen=True)
class SecretAdminService:
    _authorized: Callable[[], bool]
    _snapshot: Callable[[], Mapping[str, Any]]
    _writer: Callable[[str, Any], None]

    def _ensure_authorized(self) -> None:
        if not self._authorized():
            raise PermissionError("global secret administration requires a Bot admin private chat")

    def get(self, path: str) -> Any:
        self._ensure_authorized()
        current: Any = self._snapshot()
        for part in _secret_path_parts(path):
            if not isinstance(current, Mapping) or part not in current:
                raise KeyError(f"secret path does not exist: {path}")
            current = current[part]
        return materialize_snapshot_value(current)

    def set(self, path: str, value: Any) -> None:
        self._ensure_authorized()
        _secret_path_parts(path)
        self._writer(path, value)


@dataclass(frozen=True)
class OneBotMediaService:
    _request: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]

    async def get_message(self, message_id: int | str) -> dict[str, Any]:
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, (int, str))
            or not str(message_id).strip()
        ):
            raise ValueError("message_id is required")
        response = await self._request("get_msg", {"message_id": message_id})
        return self._response_data(response)

    async def get_image(
        self,
        *,
        file_id: str | None = None,
        file: str | None    = None,
    ) -> dict[str, Any]:
        provided = [("file_id", file_id), ("file", file)]
        if any(value is not None and not isinstance(value, str) for _, value in provided):
            raise ValueError("file_id and file must be strings")
        selected = [(key, value.strip()) for key, value in provided if isinstance(value, str)]
        if len(selected) != 1 or not selected[0][1]:
            raise ValueError("exactly one of file_id or file is required")
        key, value = selected[0]
        response = await self._request("get_image", {key: value})
        return self._response_data(response)

    @staticmethod
    def _response_data(response: dict[str, Any] | None) -> dict[str, Any]:
        if (
            not isinstance(response, dict)
            or response.get("status") != "ok"
            or response.get("retcode") != 0
        ):
            return {}
        data = response.get("data")
        return dict(data) if isinstance(data, dict) else {}


@dataclass(frozen=True)
class ConfigSubscriptionService:
    _subscriber: Callable[
        [Callable[[Mapping[str, Any]], Any]],
        Callable[[], None],
    ]

    def subscribe(
        self,
        callback: Callable[[Mapping[str, Any]], Any],
    ) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("config subscription callback must be callable")
        return self._subscriber(callback)


@dataclass(frozen=True)
class CodexArxivSummaryService:
    """A fixed Codex operation with authorization rechecked on every call."""

    _authorized: Callable[[], bool]
    _enqueue: Callable[..., Awaitable[str]]

    async def enqueue_or_replay(
        self,
        *,
        date: str,
        links: list[str],
    ) -> str:
        if not self._authorized():
            raise PermissionError("Codex arXiv capability is no longer authorized")
        normalized_date = str(date).strip()
        if not normalized_date:
            raise ValueError("arXiv summary date is required")
        if (
            not isinstance(links, list)
            or not links
            or any(not isinstance(link, str) or not link.strip() for link in links)
        ):
            raise ValueError("arXiv summary links must be non-empty strings")
        return await self._enqueue(
            date  = normalized_date,
            links = list(links),
        )


# 小青 agent 的能力和对应命令同一套权限：每次调用时重新确认发起人此刻仍有这项权限。
_AGENT_UNAUTHORIZED    = "agent capability is no longer authorized for this user"
_MAX_AGENT_TIMEOUT     = 86400.0
_MAX_COMMAND_CHARS     = 4000
_MAX_SENDER_NAME_CHARS = 64
_OPERATION_ID          = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _agent_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("agent timeout must be a number of seconds")
    seconds = float(value)
    if not 0 < seconds <= _MAX_AGENT_TIMEOUT:
        raise ValueError("agent timeout must be within (0, 86400] seconds")
    return seconds


def _cancel_returned_work(call: asyncio.Future[Any]) -> None:
    if call.cancelled() or call.exception() is not None:
        return
    result = call.result()
    if isinstance(result, asyncio.Future):
        result.cancel()


async def _invoke_settled(
    invoke: Callable[[dict[str, Any]], Awaitable[Any]],
    request: dict[str, Any],
) -> Any:
    """调用服务；服务可以交回 Future，把耗时等待留在提供方插件的执行通道之外。

    串行插件（如 QingSSH）若在服务回调里等远端命令结束，会占住整个插件；提供方
    只在通道内启动工作并交回 Future，这里在通道外等它。调用方被取消时，不论取消
    落在通道内还是等待阶段，交回的工作都一并取消。通道内的调用本身很短，不打断它：
    打断会让已经启动的工作丢在通道里，没人能再取消。
    """

    call = asyncio.ensure_future(invoke(request))
    try:
        result = await asyncio.shield(call)
    except asyncio.CancelledError:
        call.add_done_callback(_cancel_returned_work)
        raise
    if not isinstance(result, asyncio.Future):
        return result
    try:
        return await result
    except asyncio.CancelledError:
        result.cancel()
        raise


def _require(authorized: Callable[[], bool]) -> None:
    """每次调用前复核：发起人此刻仍有这项能力对应的命令权限。"""

    if not authorized():
        raise PermissionError(_AGENT_UNAUTHORIZED)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


@dataclass(frozen=True)
class AgentTurnService:
    """xiaoqing_chat → xiaoqing_agent 的固定桥；返回 ``None`` 表示本轮照常聊天。"""

    _authorized: Callable[[], bool]
    _invoke: Callable[[dict[str, Any]], Awaitable[Any]]
    _report: Callable[[dict[str, Any]], Awaitable[Any]] | None = None

    async def handle_turn(self, turn: dict[str, Any]) -> dict[str, Any] | None:
        _require(self._authorized)
        if not isinstance(turn, Mapping):
            raise TypeError("agent turn must be a mapping")
        result = await self._invoke(dict(turn))
        if result is None:
            return None
        if not isinstance(result, dict):
            raise TypeError("agent turn result must be a mapping or None")
        return result

    async def report_delivery(self, task_id: str, delivered: bool | None) -> None:
        """把当轮回复的发送结果交还 agent；调用方没有声明这条桥时什么也不做。"""

        if self._report is None:
            return
        _require(self._authorized)
        if delivered is not None and not isinstance(delivered, bool):
            raise TypeError("delivery result must be True, False or None")
        await self._report({"task_id": _required_text(task_id, "task id"), "delivered": delivered})


@dataclass(frozen=True)
class CodexAgentTaskService:
    """xiaoqing_agent → codex：提交一个任务并等到它结束。"""

    _authorized: Callable[[], bool]
    _invoke: Callable[[dict[str, Any]], Awaitable[Any]]

    async def run_task(
        self,
        *,
        session: str,
        prompt: str,
        cwd: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        _require(self._authorized)
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("Codex cwd must be a string or None")
        result = await _invoke_settled(
            self._invoke,
            {
                "session": _required_text(session, "Codex session"),
                "prompt": _required_text(prompt, "Codex prompt"),
                "cwd": (cwd.strip() or None) if isinstance(cwd, str) else None,
                "timeout_seconds": _agent_timeout(timeout_seconds),
            },
        )
        if not isinstance(result, dict):
            raise TypeError("Codex agent task result must be a mapping")
        return result


@dataclass(frozen=True)
class SSHAgentCommandService:
    """xiaoqing_agent → qingssh：列出服务器或执行一次性远端命令。"""

    _authorized: Callable[[], bool]
    _invoke: Callable[[dict[str, Any]], Awaitable[Any]]

    async def list_servers(self) -> list[dict[str, Any]]:
        _require(self._authorized)
        result = await self._invoke({"op": "list"})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TypeError("SSH server listing must be a list of mappings")
        return result

    async def run(
        self,
        *,
        server: str,
        command: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        _require(self._authorized)
        result = await _invoke_settled(
            self._invoke,
            {
                "op": "run",
                "server": _required_text(server, "SSH server"),
                "command": _required_text(command, "SSH command"),
                "timeout_seconds": _agent_timeout(timeout_seconds),
            },
        )
        if not isinstance(result, dict):
            raise TypeError("SSH agent command result must be a mapping")
        return result


@dataclass(frozen=True)
class LocalToolsService:
    """agent 的本机工具（代码执行、文件读写）每次调用前复核发起人仍是 Bot 管理员。"""

    _authorized: Callable[[], bool]

    def ensure_authorized(self) -> None:
        _require(self._authorized)


@dataclass(frozen=True)
class CommandRunnerService:
    """以发起人身份执行一条已发布命令，权限与场景和本人直接发送时相同；不经过会话、闲聊和 URL 阶段。"""

    _authorized: Callable[[], bool]
    _run: Callable[..., Awaitable[list[dict[str, Any]]]]

    async def run(
        self,
        command_text: str,
        *,
        sender_name: str  = "",
        operation_id: str = "",
    ) -> list[dict[str, Any]]:
        _require(self._authorized)
        text = _required_text(command_text, "command text")
        if len(text) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command text must not exceed {_MAX_COMMAND_CHARS} characters")
        if operation_id and _OPERATION_ID.fullmatch(operation_id) is None:
            raise ValueError("operation id must be 1-64 letters, digits, '.', '_' or '-'")
        name = " ".join(str(sender_name or "").split())[:_MAX_SENDER_NAME_CHARS]
        result = await self._run(text, sender_name=name, operation_id=operation_id)
        # 命令带着发送回执（比如取图的预留）时原样交回，由调用方随实际发送结算。
        return result if isinstance(result, DeliverySegments) else list(result)


@dataclass(frozen=True)
class VoiceSynthesisService:
    """Fixed smalltalk-to-voice service; no callback name is caller-controlled."""

    _invoke: Callable[[str], Awaitable[list[dict[str, Any]] | None]]

    async def synthesize_text(self, text: str) -> list[dict[str, Any]] | None:
        normalized = str(text)
        if not normalized.strip():
            raise ValueError("voice synthesis text is required")
        return await self._invoke(normalized)


@dataclass(frozen=True)
class ChatReplyService:
    """Fixed smalltalk-to-chat provider service."""

    _invoke: Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]

    async def reply(
        self,
        text: str,
        event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        normalized = str(text)
        if not normalized.strip():
            raise ValueError("chat reply text is required")
        if not isinstance(event, dict):
            raise TypeError("chat reply event must be a mapping")
        return await self._invoke(normalized, dict(event))


@dataclass(frozen=True)
class AIService:
    """只允许插件通过自己的命名 route 使用全局模型注册表。"""

    _complete: Callable[..., Awaitable[AICompletionResult]]
    _list_models: Callable[..., tuple[AIModelInfo, ...]]

    async def complete(
        self,
        route: str,
        messages: list[dict[str, Any]],
        *,
        required_modalities: tuple[str, ...]    = ("text",),
        pinned_model: str | None                = None,
        temperature: float | None               = None,
        top_p: float | None                     = None,
        max_tokens: int | None                  = None,
        timeout_seconds: float | None           = None,
        total_timeout_seconds: float | None     = None,
        max_retry: int | None                   = None,
        retry_interval_seconds: float | None    = None,
        tools: list[dict[str, Any]] | None      = None,
        tool_choice: Any                        = None,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> AICompletionResult:
        normalized_route = str(route or "").strip()
        if not normalized_route:
            raise ValueError("AI route is required")
        return await self._complete(
            route_name             = normalized_route,
            messages               = messages,
            required_modalities    = required_modalities,
            pinned_model           = pinned_model,
            temperature            = temperature,
            top_p                  = top_p,
            max_tokens             = max_tokens,
            timeout_seconds        = timeout_seconds,
            total_timeout_seconds  = total_timeout_seconds,
            max_retry              = max_retry,
            retry_interval_seconds = retry_interval_seconds,
            tools                  = tools,
            tool_choice            = tool_choice,
            extra_payload          = extra_payload,
        )

    def list_models(
        self,
        route: str,
        *,
        required_modalities: tuple[str, ...] = ("text",),
    ) -> tuple[AIModelInfo, ...]:
        normalized_route = str(route or "").strip()
        if not normalized_route:
            raise ValueError("AI route is required")
        return self._list_models(
            route_name          = normalized_route,
            required_modalities = required_modalities,
        )

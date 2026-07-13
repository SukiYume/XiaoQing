"""Concrete least-privilege services granted to selected built-in plugins."""

from __future__ import annotations

import copy
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


def _secret_path_parts(path: str) -> list[str]:
    parts = str(path or "").split(".")
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError("invalid secret path")
    return parts


@dataclass(frozen=True)
class SecretAdminService:
    _authorized: Callable[[], bool]
    _snapshot: Callable[[], dict[str, Any]]
    _writer: Callable[[str, Any], None]

    def _ensure_authorized(self) -> None:
        if not self._authorized():
            raise PermissionError("global secret administration requires a Bot admin private chat")

    def get(self, path: str) -> Any:
        self._ensure_authorized()
        current: Any = self._snapshot()
        for part in _secret_path_parts(path):
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"secret path does not exist: {path}")
            current = current[part]
        return copy.deepcopy(current)

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
        file: str | None = None,
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
        [Callable[[dict[str, Any]], Any]],
        Callable[[], None],
    ]

    def subscribe(
        self,
        callback: Callable[[dict[str, Any]], Any],
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
            date=normalized_date,
            links=list(links),
        )


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

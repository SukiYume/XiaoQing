"""Typed models for core data structures."""

import json
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .message import normalize_inbound_message
from .plugin_execution import PluginConcurrency


class OneBotEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    time: Optional[int] = Field(default=None, description="事件时间戳")
    self_id: Optional[int] = Field(default=None, description="机器人 QQ 号")
    post_type: Optional[str] = Field(default=None, description="事件类型")
    message_type: Optional[str] = None
    sub_type: Optional[str] = None
    message_id: Optional[int] = None
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    message: Optional[Union[list[dict[str, Any]], str]] = None
    raw_message: Optional[str] = None

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, v: Any) -> Optional[Union[list[dict[str, Any]], str]]:
        """处理 message 字段的各种格式，某些 OneBot 实现可能发送空字符串而非列表"""
        if v is None or v == "":
            return v
        if isinstance(v, str):
            # 非空字符串：尝试解析为 JSON 列表，如果失败则返回 None
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list) and all(
                    isinstance(item, dict) and "type" in item for item in parsed
                ):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

            # 如果解析失败或不是列表，视为纯文本消息
            return [{"type": "text", "data": {"text": v}}]
        if isinstance(v, list):
            return v
        return None

    @model_validator(mode="after")
    def _fill_message_from_raw_message(self) -> "OneBotEvent":
        normalized = normalize_inbound_message(
            {"message": self.message, "raw_message": self.raw_message}
        )
        self.message = normalized["message"]
        return self


class PluginCommandManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    triggers: list[str]
    help: str
    admin_only: bool = False
    priority: int = 0
    usage: str | None = None


class PluginScheduleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handler: str
    cron: dict[str, Any]
    id: Optional[str] = None
    group_ids: Optional[list[int]] = None
    description: str | None = None
    enabled: bool = True

    @field_validator("group_ids", mode="before")
    @classmethod
    def _reject_boolean_group_ids(cls, value: Any) -> Any:
        if isinstance(value, list) and any(isinstance(item, bool) for item in value):
            raise ValueError("schedule group_ids must not contain booleans")
        return value

    @field_validator("group_ids")
    @classmethod
    def _validate_group_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("schedule group_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("schedule group_ids must not contain duplicates")
        return value


class PluginDependencyManifest(BaseModel):
    """A Python module dependency checked before the plugin is imported."""

    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool = True
    description: str | None = None


PluginServiceName = Literal[
    "voice.synthesize_text",
    "chat.reply",
    "codex.enqueue_arxiv_summary",
]


class PluginServiceManifest(BaseModel):
    """One narrowly declared inter-plugin service export.

    Service names and their consumers are intentionally closed over the small
    set of bridges maintained by the core.  Adding a bridge therefore requires
    an explicit core/schema change instead of making an arbitrary module
    attribute remotely callable.
    """

    model_config = ConfigDict(extra="forbid")

    name: PluginServiceName
    callback: str
    callers: list[str]
    required_capability: Literal["codex_arxiv_summary"] | None = None

    @field_validator("callback")
    @classmethod
    def _validate_callback(cls, value: str) -> str:
        if not value.isidentifier() or value.startswith("_"):
            raise ValueError("service callback must be a public Python identifier")
        return value

    @field_validator("callers")
    @classmethod
    def _validate_callers(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("service callers must not be empty")
        if any(not item.isidentifier() or item.startswith("_") for item in value):
            raise ValueError("service callers must be public plugin identifiers")
        if len(value) != len(set(value)):
            raise ValueError("service callers must not contain duplicates")
        return value


_SERVICE_CONTRACTS: dict[
    PluginServiceName,
    tuple[str, frozenset[str], str | None],
] = {
    "voice.synthesize_text": ("voice", frozenset({"smalltalk"}), None),
    "chat.reply": ("chat", frozenset({"smalltalk"}), None),
    "codex.enqueue_arxiv_summary": (
        "codex",
        frozenset({"arxiv_filter"}),
        "codex_arxiv_summary",
    ),
}


class PluginManifest(BaseModel):
    """Versioned, strict manifest for a plugin's runtime contract.

    `description` and `author` are operational metadata emitted at load time;
    `dependencies` names importable Python modules, not plugin load-order edges.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_version: Literal[1] = 1
    version: str = "0.0.0"
    description: str | None = None
    author: str | None = None
    entry: str = "main.py"
    commands: list[PluginCommandManifest] = Field(default_factory=list)
    schedule: list[PluginScheduleManifest] = Field(default_factory=list)
    dependencies: list[PluginDependencyManifest] = Field(default_factory=list)
    services: list[PluginServiceManifest] = Field(default_factory=list)
    concurrency: PluginConcurrency = "parallel"
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_service_contracts(self) -> "PluginManifest":
        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("plugin services must not contain duplicate names")
        for service in self.services:
            owner, callers, required_capability = _SERVICE_CONTRACTS[service.name]
            if self.name != owner:
                raise ValueError(f"service {service.name} may only be exported by {owner}")
            if frozenset(service.callers) != callers:
                expected = ", ".join(sorted(callers))
                raise ValueError(f"service {service.name} callers must be exactly: {expected}")
            if service.required_capability != required_capability:
                raise ValueError(
                    f"service {service.name} required_capability must be {required_capability!r}"
                )
        return self

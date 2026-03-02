"""Typed models for core data structures."""

import json
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            
            # 如果解析失败或不是列表，视为纯文本消息
            return [{"type": "text", "data": {"text": v}}]
        if isinstance(v, list):
            return v
        return None

class PluginCommandManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    triggers: list[str]
    help: str
    admin_only: bool = False
    priority: int = 0

class PluginScheduleManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    handler: str
    cron: dict[str, Any]
    id: Optional[str] = None
    group_ids: Optional[list[int]] = None

class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    version: str = "0.0.0"
    entry: str = "main.py"
    commands: list[PluginCommandManifest] = Field(default_factory=list)
    schedule: list[PluginScheduleManifest] = Field(default_factory=list)
    concurrency: str = "parallel"
    enabled: bool = True

"""结构化写入日程的输入；绕开自然语言解析，由调用方直接给出类型、时间和节点。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from dateutil.rrule import rrulestr
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

_WALL_TIME_FORMAT = "%Y-%m-%dT%H:%M"
_OFFSET_RE        = re.compile(r"提前[1-9]\d*(?:分钟|小时|天|周)")


def _wall_time(value: str) -> str:
    try:
        datetime.strptime(value, _WALL_TIME_FORMAT)
    except ValueError:
        raise ValueError("时间格式需为用户当地墙钟时间 YYYY-MM-DDTHH:MM") from None
    return value


WallTime = Annotated[str, AfterValidator(_wall_time)]


class EventNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name    : str = Field(min_length=1, max_length=200, description="节点名称。")
    time    : WallTime = Field(description="节点时间，用户当地墙钟时间 YYYY-MM-DDTHH:MM。")
    end_time: WallTime | None = Field(default=None, description="节点结束时间，可省略。")

    @model_validator(mode="after")
    def check_range(self) -> Self:
        if self.end_time is not None and self.end_time <= self.time:
            raise ValueError(f"节点“{self.name}”的 end_time 须晚于 time")
        return self


class EventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["single", "multi_node", "recurring"] = Field(
        description="single 单次日程；multi_node 多个独立时间节点；recurring 按 rrule 重复。"
    )
    title         : str = Field(min_length=1, max_length=200, description="日程标题。")
    content       : str = Field(default="", description="详细内容，省略时采用标题。")
    location      : str = ""
    notes         : str = ""
    category      : str = ""
    start_time    : WallTime | None = Field(default=None, description="single、recurring 必填。")
    end_time      : WallTime | None = None
    nodes         : list[EventNode] = Field(default_factory=list, description="multi_node 至少两个。")
    rrule         : str = Field(default="", description="RFC5545 RRULE，recurring 必填。")
    remind_offsets: list[str] = Field(
        default_factory = list,
        description     = "提前提醒，例如“提前1天”“提前30分钟”；应用于每个实例或节点。",
    )

    @model_validator(mode="after")
    def check_kind(self) -> Self:
        if self.kind == "multi_node":
            if len(self.nodes) < 2:
                raise ValueError("multi_node 需要至少 2 个 nodes")
            if self.rrule or self.start_time or self.end_time:
                raise ValueError(
                    "multi_node 只用 nodes 表示时间，不能带 rrule、start_time、end_time"
                )
        else:
            if self.nodes:
                raise ValueError(f"{self.kind} 不能带 nodes")
            if self.start_time is None:
                raise ValueError(f"{self.kind} 需要 start_time")
            if self.end_time is not None and self.end_time <= self.start_time:
                raise ValueError("end_time 须晚于 start_time")
        if self.kind == "single" and self.rrule:
            raise ValueError("single 不能带 rrule")
        if self.kind == "recurring":
            if not self.rrule:
                raise ValueError("recurring 需要 rrule")
            try:
                start = datetime.strptime(str(self.start_time), _WALL_TIME_FORMAT)
                rrulestr(self.rrule, dtstart=start)
            except (ValueError, TypeError):
                raise ValueError("rrule 不是有效的 RFC5545 规则") from None
        for offset in self.remind_offsets:
            if not _OFFSET_RE.fullmatch(offset):
                raise ValueError(f"提醒“{offset}”需写成“提前正整数分钟/小时/天/周”")
        return self

    def parsed_fields(self) -> dict[str, Any]:
        """与 AI 解析输出同结构的字段，交给解析器的结果整理逻辑。"""

        return {
            "title"         : self.title,
            "start_time"    : self.start_time,
            "end_time"      : self.end_time,
            "location"      : self.location,
            "category"      : self.category,
            "remind_offsets": self.remind_offsets,
            "rrule"         : self.rrule,
            "milestones"    : [node.model_dump(exclude_none=True) for node in self.nodes],
            "notes"         : self.notes,
            "parse_source"  : "record",
        }

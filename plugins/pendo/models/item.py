"""数据库读写共享的条目数据模型。"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..utils.identifiers import new_internal_id, public_id


class ItemType(Enum):
    """条目类型"""

    EVENT = "event"  # 日程
    TASK = "task"  # 待办
    NOTE = "note"  # 笔记/想法
    DIARY = "diary"  # 日记
    LEDGER = "ledger"  # 记账


class TaskStatus(Enum):
    """任务状态"""

    OPEN = "open"  # 未完成
    DONE = "done"  # 已完成
    CANCELLED = "cancelled"  # 已取消


@dataclass
class Item:
    """统一的条目基类"""

    # 通用字段
    id: str = field(default_factory=new_internal_id)
    type: ItemType = ItemType.NOTE
    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "未分类"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    owner_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)  # 来源上下文(群id、私聊等)
    visibility: str = "private"  # private/group_scope
    attachments: list[dict[str, str]] = field(default_factory=list)
    ai_meta: dict[str, Any] = field(default_factory=dict)  # AI生成的摘要、关键词等
    deleted: bool = False  # 软删除标记
    deleted_at: str | None = None  # 软删除时间戳
    version: int = 0  # optimistic-concurrency revision

    @property
    def display_id(self) -> str:
        """返回可安全用于聊天展示和命令输入的短标识。"""

        return public_id(self.id)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        # 将所有 Enum 值转换为其 .value（如 ItemType, TaskStatus 等）
        for key, value in data.items():
            if isinstance(value, Enum):
                data[key] = value.value
        return data


@dataclass
class EventItem(Item):
    """日程条目"""

    type: ItemType = ItemType.EVENT

    # Event特有字段
    start_time: str | None = None  # ISO格式时间
    end_time: str | None = None
    timezone: str = "Asia/Shanghai"
    location: str = ""
    participants: list[str] = field(default_factory=list)
    remind_times: list[str] = field(default_factory=list)  # 提醒时间点列表
    reminder_rules: list[dict[str, Any]] = field(default_factory=list)
    event_role: str = "single"  # single | multi_node_child | recurring_occurrence
    event_collection_id: str | None = None
    event_collection_kind: str | None = None  # multi_node | recurring
    event_index: int | None = None
    event_node_key: str | None = None
    source_item_id: str | None = None
    notes: str = ""


@dataclass
class TaskItem(Item):
    """待办条目"""

    type: ItemType = ItemType.TASK

    # Task特有字段
    plan_date: str | None = None  # 计划处理日期 YYYY-MM-DD
    deadline_at: str | None = None  # 硬截止时间 ISO datetime
    priority: int = 3  # 优先级 1-5 (默认3=中)
    status: TaskStatus = TaskStatus.OPEN
    remind_times: list[str] = field(default_factory=list)  # 提醒时间点列表
    reminder_rules: list[dict[str, Any]] = field(default_factory=list)
    repeat_rule: str | None = None
    completed_at: str | None = None  # 完成时间
    cancelled_at: str | None = None  # 取消时间


@dataclass
class NoteItem(Item):
    """笔记/想法条目"""

    type: ItemType = ItemType.NOTE

    # Note特有字段
    references: list[dict[str, str]] = field(default_factory=list)  # 引用的其他条目或消息
    last_viewed: str | None = None
    related_items: list[str] = field(default_factory=list)  # 相关条目ID


@dataclass
class DiaryItem(Item):
    """日记条目"""

    type: ItemType = ItemType.DIARY

    # Diary特有字段
    mood: str | None = None  # 情绪(如: happy, sad, calm等)
    mood_score: int | None = None  # 情绪评分(1-10)
    weather: str | None = None
    location: str = ""  # 字段必须始终存在，确保 asdict() 可稳定持久化位置。
    template_id: str | None = None  # 使用的模板ID
    diary_date: str | None = None  # 日记对应的日期(YYYY-MM-DD)
    entry_time: str | None = None  # 记录发生/写下的具体时间(ISO datetime)
    template_answers: list[dict[str, str]] = field(default_factory=list)  # 模板问题与回答
    is_favorite: bool = False  # 是否收藏，便于回看


@dataclass
class LedgerItem(Item):
    """记账条目"""

    type: ItemType = ItemType.LEDGER

    # Ledger特有字段
    amount: float = 0.0  # 金额镜像（由 amount_cents 派生，正数）
    amount_cents: int = 0  # 金额（分，正整数，账本计算主字段）
    currency: str = "CNY"  # 币种
    transaction_type: str = "expense"  # expense | income | transfer
    ledger_category: str = "其他"  # 账目分类（餐饮、交通等）
    ledger_date: str | None = None  # 账目日期 YYYY-MM-DD
    account_name: str = "现金"  # 账户/钱包
    counter_account_name: str = ""  # 转账目标账户
    merchant: str = ""  # 商户/付款方/收款方
    remark: str = ""  # 备注


# 类型映射
ITEM_TYPE_CLASS_MAP: dict[ItemType, type[Item]] = {
    ItemType.EVENT: EventItem,
    ItemType.TASK: TaskItem,
    ItemType.NOTE: NoteItem,
    ItemType.DIARY: DiaryItem,
    ItemType.LEDGER: LedgerItem,
}


def get_item_type_value(item_type: Any, default: str = "item") -> str:
    """Normalize enum/string item types to a stable string value."""
    if isinstance(item_type, ItemType):
        return item_type.value

    value = getattr(item_type, "value", None)
    if isinstance(value, str):
        return value

    if isinstance(item_type, str) and item_type:
        return item_type

    return default

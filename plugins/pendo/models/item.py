"""
统一的Item数据模型
根据AGENTS.md第四章节定义的统一模型

当前状态说明：
==================
本文件定义了完整的类型体系（ItemType枚举、Item dataclass继承体系、工厂方法等），
Database层已经支持dataclass实例的读写操作（见services/db.py）：
- get_item() 返回 Item dataclass 实例
- get_items() 返回 Item dataclass 列表
- insert_item() 接受 dict 或 Item dataclass 实例
- update_item() 接受 dict 或 Item dataclass 实例

保留原因：
==========
1. ItemType枚举已在全代码库中统一使用（修复了CodeReview 4.9问题）
2. dataclass为Database层提供类型安全的读写操作
3. 提供类型提示，增强IDE自动补全和静态类型检查支持
4. Handler层可以直接使用dataclass实例获得类型安全

未来优化路径：
=============
1. 修改Handler层方法接受和返回dataclass实例，而非dict
2. 使用mypy/pyright等静态类型检查工具确保类型安全
3. 在新功能中优先使用dataclass，验证稳定性

使用说明：
=========
Database层已自动处理dataclass与数据库行的转换：
- 从数据库读取：_row_to_item() 自动创建对应的子类实例（EventItem/TaskItem等）
- 写入数据库：to_dict() 自动转换为dict并处理枚举类型
- Handler层可以灵活选择使用dict或dataclass实例
"""
from enum import Enum
from typing import Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
import uuid

class ItemType(Enum):
    """条目类型"""
    EVENT = "event"      # 日程
    TASK = "task"        # 待办
    NOTE = "note"        # 笔记/想法
    DIARY = "diary"      # 日记

class TaskStatus(Enum):
    """任务状态"""
    TODO = "todo"              # 未开始
    IN_PROGRESS = "in_progress"  # 进行中
    DONE = "done"              # 已完成
    CANCELLED = "cancelled"    # 已取消

class Priority(Enum):
    """优先级 (1=紧急, 2=高, 3=中, 4=低)"""
    URGENT = 1    # 紧急
    HIGH = 2      # 高
    MEDIUM = 3    # 中
    LOW = 4       # 低

@dataclass
class Item:
    """统一的条目基类"""
    # 通用字段
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: ItemType = ItemType.NOTE
    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "未分类"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    owner_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)  # 来源上下文(群id、私聊等)
    visibility: str = "private"  # private/group_scope
    attachments: list[dict[str, str]] = field(default_factory=list)
    ai_meta: dict[str, Any] = field(default_factory=dict)  # AI生成的摘要、关键词等
    deleted: bool = False  # 软删除标记
    deleted_at: Optional[str] = None  # 软删除时间戳
    
    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        # 将所有 Enum 值转换为其 .value（如 ItemType, TaskStatus 等）
        for key, value in data.items():
            if isinstance(value, Enum):
                data[key] = value.value
        return data
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Item':
        """从字典创建"""
        if 'type' in data and isinstance(data['type'], str):
            data['type'] = ItemType(data['type'])
        return cls(**data)

@dataclass
class EventItem(Item):
    """日程条目"""
    type: ItemType = ItemType.EVENT
    
    # Event特有字段
    start_time: Optional[str] = None  # ISO格式时间
    end_time: Optional[str] = None
    timezone: str = "Asia/Shanghai"
    location: str = ""
    participants: list[str] = field(default_factory=list)
    rrule: Optional[str] = None  # 重复规则(iCal RRULE格式)
    parent_id: Optional[str] = None  # 重复事件的父ID
    remind_policy_id: Optional[str] = None  # 提醒策略ID
    remind_times: list[str] = field(default_factory=list)  # 提醒时间点列表
    milestones: list[dict] = field(default_factory=list)
    # 格式: [{"name": "注册截止", "time": "2026-04-06T00:00:00"}, ...]
    notes: str = ""

@dataclass
class TaskItem(Item):
    """待办条目"""
    type: ItemType = ItemType.TASK
    
    # Task特有字段
    due_time: Optional[str] = None  # 截止时间
    priority: int = 3  # 优先级 1-4 (默认3=中)
    status: TaskStatus = TaskStatus.TODO
    estimate: int = 0  # 预估时长(分钟)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # 依赖的其他task id
    progress: int = 0  # 进度百分比(0-100)
    remind_times: list[str] = field(default_factory=list)  # 提醒时间点列表
    completed_at: Optional[str] = None  # 完成时间

@dataclass  
class NoteItem(Item):
    """笔记/想法条目"""
    type: ItemType = ItemType.NOTE
    
    # Note特有字段
    references: list[dict[str, str]] = field(default_factory=list)  # 引用的其他条目或消息
    last_viewed: Optional[str] = None
    related_items: list[str] = field(default_factory=list)  # 相关条目ID

@dataclass
class DiaryItem(Item):
    """日记条目"""
    type: ItemType = ItemType.DIARY

    # Diary特有字段
    mood: Optional[str] = None  # 情绪(如: happy, sad, calm等)
    mood_score: Optional[int] = None  # 情绪评分(1-10)
    weather: Optional[str] = None
    location: str = ""  # L-7修复：缺失的 location 字段，缺少时 asdict() 不序列化导致无法持久化
    template_id: Optional[str] = None  # 使用的模板ID
    diary_date: Optional[str] = None  # 日记对应的日期(YYYY-MM-DD)

# 类型映射
ITEM_TYPE_CLASS_MAP = {
    ItemType.EVENT: EventItem,
    ItemType.TASK: TaskItem,
    ItemType.NOTE: NoteItem,
    ItemType.DIARY: DiaryItem,
}

def create_item(item_type: ItemType, **kwargs) -> Item:
    """工厂方法：根据类型创建对应的Item实例"""
    item_class = ITEM_TYPE_CLASS_MAP.get(item_type, Item)
    return item_class(**kwargs)

"""管理员审计与关键业务操作的持久化日志模型。"""

from dataclasses import dataclass, field
from datetime import datetime

from ..utils.time import utc_now


@dataclass
class OperationLog:
    """记录操作主体、目标、参数摘要、结果和发生时间。"""

    id: int
    group_id: int
    user_id: str
    target_user_id: str | None = None
    operation_type: str = ""
    params: str = ""
    result: str = "success"
    created_at: datetime = field(default_factory=utc_now)

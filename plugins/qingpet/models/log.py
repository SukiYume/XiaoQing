from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from ..utils.time import utc_now

@dataclass
class OperationLog:
    id: int
    group_id: int
    user_id: str
    target_user_id: Optional[str] = None
    operation_type: str = ""
    params: str = ""
    result: str = "success"
    created_at: datetime = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = utc_now()

"""
类型定义（精简版）
"""
from typing import Any, Literal, TypedDict

# ==================== 命令结果类型 ====================

class SuccessResult(TypedDict):
    """成功结果"""
    status: Literal['success']
    message: str
    item_id: str | None
    data: dict[str, Any] | None

class ErrorResult(TypedDict):
    """错误结果"""
    status: Literal['error']
    message: str
    error_code: str | None

class NeedConfirmResult(TypedDict):
    """需要确认的结果"""
    status: Literal['need_confirm']
    message: str
    data: dict[str, Any]
    confirm_key: str

class NeedMoreInfoResult(TypedDict):
    """需要更多信息的结果"""
    status: Literal['need_more_info']
    message: str
    missing_fields: list[str]
    current_data: dict[str, Any]

# 命令执行结果的联合类型
CommandResult = SuccessResult | ErrorResult | NeedConfirmResult | NeedMoreInfoResult

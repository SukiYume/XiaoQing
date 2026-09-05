"""Pendo 运行路径实际使用的业务异常。"""

import uuid


class PendoException(Exception):
    """同时保存内部诊断信息与可公开给用户的固定消息。"""

    def __init__(
        self,
        message: str,
        user_message: str | None = None,
        error_code: str | None   = None,
    ) -> None:
        super().__init__(message)
        self.user_message = user_message or message
        self.error_code   = error_code or uuid.uuid4().hex[:8].upper()

    def get_user_message(self) -> str:
        """返回不含内部堆栈与实现细节的公开消息。"""
        return self.user_message


class ItemNotFoundException(PendoException):
    """指定条目不存在。"""

    def __init__(self, item_id: str) -> None:
        super().__init__(f"Item not found: {item_id}", f"❌ 找不到ID为 {item_id} 的条目")
        self.item_id = item_id


class ItemAlreadyDeletedException(PendoException):
    """指定条目已经被软删除。"""

    def __init__(self, item_id: str) -> None:
        super().__init__(f"Item already deleted: {item_id}", f"❌ ID为 {item_id} 的条目已被删除")


class AmbiguousIdentifierException(PendoException):
    """用户提供的短标识匹配到多个内部实体。"""

    def __init__(self, reference: str, matched_ids: list[str]) -> None:
        candidates = "、".join(f"`{item_id}`" for item_id in matched_ids[:5])
        suffix     = f"\n\n候选完整 ID：{candidates}" if candidates else ""
        super().__init__(
            f"Ambiguous identifier {reference}: {matched_ids}",
            f"⚠️ 短标识 `{reference}` 匹配到多个条目，请使用完整 ID{suffix}",
        )
        self.reference   = reference
        self.matched_ids = matched_ids


class ItemVersionConflictException(PendoException):
    """条目在读取后已被其他请求修改。"""

    def __init__(self, item_id: str) -> None:
        super().__init__(
            f"Item version conflict: {item_id}",
            "⚠️ 条目已被其他请求修改，请刷新后重试",
        )


class InvalidTimeFormatException(PendoException):
    """用户提供的时间无法按要求解析。"""

    def __init__(self, time_str: str, expected_format: str | None = None) -> None:
        format_hint = f"\n期望格式: {expected_format}" if expected_format else ""
        super().__init__(
            f"Invalid time format: {time_str}",
            f"❌ 时间格式不正确: {time_str}{format_hint}",
        )


class PastTimeException(PendoException):
    """不允许使用已经过去的时间。"""

    def __init__(self, time_str: str) -> None:
        super().__init__(f"Time is in the past: {time_str}", f"❌ 时间 {time_str} 已过去")


class InvalidDateRangeException(PendoException):
    """日期范围的起止顺序或格式无效。"""

    def __init__(self, start: str, end: str) -> None:
        super().__init__(
            f"Invalid date range: {start} to {end}",
            f"❌ 日期范围无效: {start} 到 {end}",
        )


class NaturalLanguageParseException(PendoException):
    """自然语言输入无法转换为结构化条目。"""

    def __init__(self, input_text: str, reason: str | None = None) -> None:
        reason_msg = f": {reason}" if reason else ""
        super().__init__(
            f"Failed to parse: {input_text}{reason_msg}",
            f"❓ 无法理解你的输入{reason_msg}",
        )


class MissingRequiredFieldException(PendoException):
    """创建或修改条目时缺少必填字段。"""

    def __init__(self, field_name: str, field_desc: str | None = None) -> None:
        description = field_desc or field_name
        super().__init__(f"Missing required field: {field_name}", f"❓ 请提供{description}")
        self.field_name = field_name


class InvalidFieldValueException(PendoException):
    """字段值不在允许范围内。"""

    def __init__(
        self,
        field_name: str,
        value: str,
        valid_values: list[str] | None = None,
    ) -> None:
        valid_msg = f"\n有效值: {', '.join(valid_values)}" if valid_values else ""
        super().__init__(
            f"Invalid value for {field_name}: {value}",
            f"❌ {field_name} 的值 '{value}' 无效{valid_msg}",
        )

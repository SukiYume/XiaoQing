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
        candidates = "、".join(f"{item_id}" for item_id in matched_ids[:5])
        suffix     = f"\n\n候选完整 ID：{candidates}" if candidates else ""
        super().__init__(
            f"Ambiguous identifier {reference}: {matched_ids}",
            f"⚠️ 短标识 {reference} 匹配到多个条目，请使用完整 ID{suffix}",
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


class MissingRequiredFieldException(PendoException):
    """创建或修改条目时缺少必填字段。"""

    def __init__(self, field_name: str, field_desc: str | None = None) -> None:
        description = field_desc or field_name
        super().__init__(f"Missing required field: {field_name}", f"❓ 请提供{description}")
        self.field_name = field_name

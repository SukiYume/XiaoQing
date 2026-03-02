"""
Pendo 自定义异常类（精简版）
"""
import uuid
import logging

logger = logging.getLogger(__name__)


class PendoException(Exception):
    """Pendo 基础异常类"""
    
    def __init__(self, message: str, user_message: str = None, error_code: str = None):
        super().__init__(message)
        self.user_message = user_message or message
        self.error_code = error_code or uuid.uuid4().hex[:8].upper()
    
    def get_user_message(self) -> str:
        return self.user_message
    
    def log_error(self):
        logger.error("[%s] %s: %s", self.error_code, self.__class__.__name__, str(self))


# ==================== 数据相关异常 ====================

class ItemNotFoundException(PendoException):
    """条目未找到异常"""
    def __init__(self, item_id: str):
        super().__init__(
            f"Item not found: {item_id}",
            f"❌ 找不到ID为 {item_id} 的条目"
        )
        self.item_id = item_id


class ItemAlreadyDeletedException(PendoException):
    """条目已被删除异常"""
    def __init__(self, item_id: str):
        super().__init__(f"Item already deleted: {item_id}", f"❌ ID为 {item_id} 的条目已被删除")


class DuplicateItemException(PendoException):
    """重复条目异常"""
    def __init__(self, item_type: str, identifier: str):
        super().__init__(f"Duplicate {item_type}: {identifier}", f"❌ 已存在相同的{item_type}: {identifier}")


# ==================== 权限相关异常 ====================

class PermissionDeniedException(PendoException):
    """权限不足异常"""
    def __init__(self, action: str = "执行此操作"):
        super().__init__(f"Permission denied for action: {action}", f"❌ 你没有权限{action}")


class OwnershipException(PendoException):
    """所有权异常"""
    def __init__(self, item_id: str):
        super().__init__(f"User does not own item: {item_id}", f"❌ 你不是 {item_id} 的所有者，无法修改")


# ==================== 时间相关异常 ====================

class InvalidTimeFormatException(PendoException):
    """时间格式错误异常"""
    def __init__(self, time_str: str, expected_format: str = None):
        format_hint = f"\n期望格式: {expected_format}" if expected_format else ""
        super().__init__(f"Invalid time format: {time_str}", f"❌ 时间格式不正确: {time_str}{format_hint}")


class TimeConflictException(PendoException):
    """时间冲突异常"""
    def __init__(self, new_event: dict, conflicting_events: list):
        conflict_list = "\n".join([f"  • {e.get('title', '未命名')}" for e in conflicting_events[:3]])
        super().__init__(
            f"Time conflict for event: {new_event.get('title')}",
            f"⚠️ 时间冲突\n冲突的日程:\n{conflict_list}\n回复 '是' 继续创建"
        )
        self.new_event = new_event
        self.conflicting_events = conflicting_events


class PastTimeException(PendoException):
    """过去时间异常"""
    def __init__(self, time_str: str):
        super().__init__(f"Time is in the past: {time_str}", f"❌ 时间 {time_str} 已过去")


class InvalidDateRangeException(PendoException):
    """无效日期范围异常"""
    def __init__(self, start: str, end: str):
        super().__init__(f"Invalid date range: {start} to {end}", f"❌ 日期范围无效: {start} 到 {end}")


# ==================== 解析相关异常 ====================

class NaturalLanguageParseException(PendoException):
    """自然语言解析异常"""
    def __init__(self, input_text: str, reason: str = None):
        reason_msg = f": {reason}" if reason else ""
        super().__init__(f"Failed to parse: {input_text}{reason_msg}", f"❓ 无法理解你的输入{reason_msg}")


class MissingRequiredFieldException(PendoException):
    """缺少必填字段异常"""
    def __init__(self, field_name: str, field_desc: str = None):
        desc = field_desc or field_name
        super().__init__(f"Missing required field: {field_name}", f"❓ 请提供{desc}")
        self.field_name = field_name


class InvalidFieldValueException(PendoException):
    """无效字段值异常"""
    def __init__(self, field_name: str, value: str, valid_values: list = None):
        valid_msg = f"\n有效值: {', '.join(map(str, valid_values))}" if valid_values else ""
        super().__init__(f"Invalid value for {field_name}: {value}", f"❌ {field_name} 的值 '{value}' 无效{valid_msg}")


# ==================== 数据库相关异常 ====================

class DatabaseException(PendoException):
    """数据库异常"""
    def __init__(self, message: str, user_message: str = None):
        super().__init__(message, user_message or "❌ 数据库错误，请稍后重试")


class DatabaseConnectionException(DatabaseException):
    """数据库连接异常"""
    def __init__(self, details: str = None):
        super().__init__(f"Database connection failed: {details}", "❌ 无法连接到数据库")


class DatabaseQueryException(DatabaseException):
    """数据库查询异常"""
    def __init__(self, query: str, error: str):
        super().__init__(f"Query failed: {error}")


# ==================== AI相关异常 ====================

class AIServiceException(PendoException):
    """AI服务异常"""
    def __init__(self, service_name: str, details: str = None):
        super().__init__(f"AI service error ({service_name}): {details}", "❌ AI服务暂时不可用")


class AIParseException(AIServiceException):
    """AI解析异常"""
    def __init__(self, input_text: str):
        super().__init__("AI Parser", f"Failed to parse: {input_text}")


# ==================== 搜索相关异常 ====================

class SearchException(PendoException):
    """搜索异常"""
    pass


class EmptyQueryException(SearchException):
    """空查询异常"""
    def __init__(self):
        super().__init__("Empty search query", "❌ 搜索关键词不能为空")


class TooManyResultsException(SearchException):
    """搜索结果过多异常"""
    def __init__(self, count: int, max_count: int):
        super().__init__(f"Too many results: {count}", f"⚠️ 找到 {count} 个结果，请使用更具体的关键词")


# ==================== 导出导入相关异常 ====================

class ExportException(PendoException):
    """导出异常"""
    def __init__(self, format_type: str, details: str = None):
        super().__init__(f"Export failed ({format_type}): {details}", "❌ 导出失败")


class ImportException(PendoException):
    """导入异常"""
    def __init__(self, filename: str, details: str = None):
        super().__init__(f"Import failed: {filename}", f"❌ 导入文件 {filename} 失败")


class InvalidFileFormatException(ImportException):
    """无效文件格式异常"""
    def __init__(self, filename: str, expected_format: str):
        super().__init__(filename, f"期望格式: {expected_format}")


# ==================== 配置相关异常 ====================

class ConfigurationException(PendoException):
    """配置异常"""
    def __init__(self, config_key: str, details: str = None):
        super().__init__(f"Configuration error: {config_key}", f"❌ 配置错误: {config_key}")


# ==================== 提醒相关异常 ====================

class ReminderException(PendoException):
    """提醒异常"""
    pass


class ReminderNotScheduledException(ReminderException):
    """提醒未调度异常"""
    def __init__(self, item_id: str):
        super().__init__(f"No reminder for item: {item_id}", "⚠️ 该条目没有设置提醒")


class InvalidReminderPolicyException(ReminderException):
    """无效提醒策略异常"""
    def __init__(self, policy_name: str):
        super().__init__(f"Invalid policy: {policy_name}", f"❌ 提醒策略 '{policy_name}' 不存在")

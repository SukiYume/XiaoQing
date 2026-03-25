"""
输入验证和清洗工具

提供统一的输入验证功能，确保数据安全性和一致性
"""
from datetime import datetime
import re
from typing import Any, Optional

def sanitize_text(text: str, max_length: int = 10000) -> str:
    """清洗文本输入
    
    Args:
        text: 输入文本
        max_length: 最大长度限制
        
    Returns:
        清洗后的文本
    """
    if not text:
        return ""
    
    # 转换为字符串
    text = str(text)
    
    # 限制长度
    if len(text) > max_length:
        text = text[:max_length]
    
    # 移除控制字符（保留换行和制表符）
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    
    # 规范化Unicode字符
    text = text.strip()
    
    return text

def validate_category(category: str, max_length: int = 50) -> str:
    """验证分类名
    
    Args:
        category: 分类名
        max_length: 最大长度
        
    Returns:
        验证后的分类名
        
    Raises:
        ValueError: 分类名无效
    """
    if not category:
        raise ValueError("分类名不能为空")
    
    # 清洗并限制长度
    category = sanitize_text(category, max_length)
    
    # 验证字符集（只允许中文、英文、数字、下划线、短横线、空格）
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_\-\s]+$', category):
        raise ValueError("分类名只能包含中文、英文、数字、下划线、短横线和空格")
    
    return category.strip()

def validate_tag(tag: str, max_length: int = 20) -> str:
    """验证标签名
    
    Args:
        tag: 标签名
        max_length: 最大长度
        
    Returns:
        验证后的标签名
        
    Raises:
        ValueError: 标签名无效
    """
    if not tag:
        raise ValueError("标签名不能为空")
    
    # 清洗并限制长度
    tag = sanitize_text(tag, max_length)
    
    # 验证字符集（不允许空格）
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_\-]+$', tag):
        raise ValueError("标签名只能包含中文、英文、数字、下划线和短横线")
    
    return tag

def validate_title(title: str, max_length: int = 200) -> str:
    """验证标题
    
    Args:
        title: 标题
        max_length: 最大长度
        
    Returns:
        验证后的标题
        
    Raises:
        ValueError: 标题无效
    """
    if not title:
        raise ValueError("标题不能为空")
    
    # 清洗并限制长度
    title = sanitize_text(title, max_length)
    
    return title.strip()

def sanitize_search_keyword(keyword: str) -> str:
    """清洗搜索关键词
    
    Args:
        keyword: 搜索关键词
        
    Returns:
        清洗后的关键词
    """
    if not keyword:
        return ""
    
    # 移除FTS特殊字符
    keyword = re.sub(r'["*]', '', keyword)
    
    # 限制长度
    if len(keyword) > 100:
        keyword = keyword[:100]
    
    return keyword.strip()

def validate_diary_content(content: str, max_length: int = 50000) -> str:
    """验证日记内容
    
    Args:
        content: 日记内容
        max_length: 最大长度
        
    Returns:
        验证后的内容
    """
    if not content:
        return ""
    
    # 清洗并限制长度
    content = sanitize_text(content, max_length)
    
    return content

def validate_location(location: str, max_length: int = 200) -> str:
    """验证地点
    
    Args:
        location: 地点
        max_length: 最大长度
        
    Returns:
        验证后的地点
    """
    if not location:
        return ""
    
    # 清洗并限制长度
    location = sanitize_text(location, max_length)
    
    return location.strip()

def validate_priority(priority: Any) -> int:
    """验证优先级
    
    Args:
        priority: 优先级值
        
    Returns:
        验证后的优先级（1-5）
        
    Raises:
        ValueError: 优先级无效
    """
    try:
        priority = int(priority)
    except (ValueError, TypeError):
        raise ValueError("优先级必须是数字")
    
    if not 1 <= priority <= 5:
        raise ValueError("优先级必须在1-5之间")
    
    return priority

def validate_item_data(data: dict[str, Any]) -> dict[str, Any]:
    """验证条目数据
    
    Args:
        data: 条目数据字典
        
    Returns:
        验证后的数据字典
        
    Raises:
        ValueError: 数据无效
    """
    validated = {}
    
    # 验证标题
    if 'title' in data:
        validated['title'] = validate_title(data['title'])
    
    # 验证内容
    if 'content' in data:
        validated['content'] = sanitize_text(data['content'], 50000)
    
    # 验证分类
    if 'category' in data and data['category']:
        validated['category'] = validate_category(data['category'])
    
    # 验证标签
    if 'tags' in data and isinstance(data['tags'], list):
        validated['tags'] = [validate_tag(tag) for tag in data['tags'] if tag]
    
    # 验证优先级
    if 'priority' in data and data['priority'] is not None:
        validated['priority'] = validate_priority(data['priority'])
    
    # 验证地点
    if 'location' in data and data['location']:
        validated['location'] = validate_location(data['location'])
    
    # 复制其他字段
    for key, value in data.items():
        if key not in validated and value is not None:
            validated[key] = value
    
    return validated


def normalize_ledger_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 ledger 字段。"""
    normalized = dict(data)

    amount = normalized.get("amount")
    if not partial or "amount" in normalized:
        if amount is None or float(amount) <= 0:
            raise ValueError("Ledger amount must be greater than 0")
        normalized["amount"] = float(amount)

    direction = normalized.get("direction")
    if direction is None and not partial:
        direction = "expense"
    if direction is not None:
        if direction not in {"expense", "income"}:
            raise ValueError("Invalid ledger direction")
        normalized["direction"] = direction

    category = normalized.get("ledger_category")
    if category is None and not partial:
        category = "其他"
    if category is not None:
        normalized["ledger_category"] = str(category).strip() or "其他"

    ledger_date = normalized.get("ledger_date")
    if not ledger_date and not partial:
        ledger_date = datetime.now().strftime("%Y-%m-%d")
    if ledger_date:
        try:
            normalized["ledger_date"] = datetime.strptime(str(ledger_date), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Invalid ledger_date, expected YYYY-MM-DD") from exc

    return normalized


def _normalize_iso_datetime(value: Any, field_name: str) -> str:
    """将输入规范化为秒级 ISO datetime。"""
    text = sanitize_text(str(value), 40)
    if not text:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name}, expected ISO datetime") from exc
    return parsed.isoformat(timespec="seconds")


def normalize_event_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 event 字段。"""
    normalized = dict(data)

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    category = normalized.get("category")
    if category is None and not partial:
        category = "未分类"
    if category is not None:
        normalized["category"] = validate_category(category or "未分类")

    if "location" in normalized:
        normalized["location"] = validate_location(normalized.get("location") or "")
    elif not partial:
        normalized["location"] = ""

    if "notes" in normalized:
        normalized["notes"] = sanitize_text(normalized.get("notes") or "", 50000)
    elif not partial:
        normalized["notes"] = ""

    timezone = normalized.get("timezone")
    if timezone is None and not partial:
        timezone = "Asia/Shanghai"
    if timezone is not None:
        normalized["timezone"] = sanitize_text(str(timezone), 80) or "Asia/Shanghai"

    rrule = normalized.get("rrule")
    if rrule is not None:
        normalized["rrule"] = sanitize_text(str(rrule), 500)

    reminders = normalized.get("remind_times")
    if reminders is None and not partial:
        reminders = []
    if reminders is not None:
        if not isinstance(reminders, list):
            raise ValueError("remind_times must be a list")
        normalized["remind_times"] = sorted({
            _normalize_iso_datetime(value, "remind_times")
            for value in reminders
            if value not in (None, "")
        })

    milestones = normalized.get("milestones")
    if milestones is None and not partial:
        milestones = []
    if milestones is not None:
        if not isinstance(milestones, list):
            raise ValueError("milestones must be a list")
        cleaned_milestones = []
        seen_times: set[str] = set()
        for row in milestones:
            if not isinstance(row, dict):
                raise ValueError("milestones must contain objects")
            name = sanitize_text(str(row.get("name") or ""), 120)
            if not name:
                raise ValueError("Milestone name cannot be empty")
            time_value = _normalize_iso_datetime(row.get("time"), "milestones.time")
            if time_value in seen_times:
                raise ValueError("Duplicate milestone time is not allowed")
            seen_times.add(time_value)
            cleaned_milestones.append({"name": name, "time": time_value})

        cleaned_milestones.sort(key=lambda row: row["time"])
        if cleaned_milestones and len(cleaned_milestones) < 2:
            raise ValueError("Milestone events require at least 2 milestones")
        normalized["milestones"] = cleaned_milestones

    milestones = normalized.get("milestones") or []
    if milestones:
        normalized["start_time"] = milestones[0]["time"]
        normalized["end_time"] = milestones[-1]["time"]
    else:
        start_time = normalized.get("start_time")
        if not start_time:
            raise ValueError("Event start_time is required")
        normalized["start_time"] = _normalize_iso_datetime(start_time, "start_time")

        end_time = normalized.get("end_time")
        if end_time in (None, ""):
            normalized["end_time"] = None
        else:
            normalized["end_time"] = _normalize_iso_datetime(end_time, "end_time")
            if normalized["end_time"] < normalized["start_time"]:
                raise ValueError("Event end_time must be after start_time")

        normalized["milestones"] = []

    return normalized


def normalize_task_fields(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """规范化并验证 task 字段。"""
    normalized = dict(data)

    title = normalized.get("title")
    if not partial or title is not None:
        normalized["title"] = validate_title(title or "")

    category = normalized.get("category")
    if category is None and not partial:
        category = "未分类"
    if category is not None:
        normalized["category"] = validate_category(category or "未分类")

    if "content" in normalized:
        normalized["content"] = sanitize_text(normalized.get("content") or "", 50000)
    elif not partial:
        normalized["content"] = ""

    due_time = normalized.get("due_time")
    if due_time in (None, ""):
        normalized["due_time"] = None
    elif due_time is not None:
        normalized["due_time"] = _normalize_iso_datetime(due_time, "due_time")

    priority = normalized.get("priority")
    if priority is None and not partial:
        priority = 3
    if priority is not None:
        normalized["priority"] = validate_priority(priority)

    status = normalized.get("status")
    if status is None and not partial:
        status = "todo"
    if status is not None:
        status = sanitize_text(str(status), 30)
        if status not in {"todo", "in_progress", "done", "cancelled"}:
            raise ValueError("Invalid task status")
        normalized["status"] = status

    progress = normalized.get("progress")
    if progress is not None:
        try:
            progress_value = int(progress)
        except (TypeError, ValueError) as exc:
            raise ValueError("Task progress must be an integer") from exc
        if not 0 <= progress_value <= 100:
            raise ValueError("Task progress must be between 0 and 100")
        normalized["progress"] = progress_value

    if "estimate" in normalized and normalized.get("estimate") not in (None, ""):
        try:
            estimate = int(normalized["estimate"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Task estimate must be an integer") from exc
        if estimate < 0:
            raise ValueError("Task estimate must be non-negative")
        normalized["estimate"] = estimate

    completed_at = normalized.get("completed_at")
    status_value = normalized.get("status")
    if status_value == "done":
        if completed_at in (None, ""):
            normalized["completed_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            normalized["completed_at"] = _normalize_iso_datetime(completed_at, "completed_at")
    elif status_value is not None:
        normalized["completed_at"] = None

    return normalized

"""
输入验证和清洗工具

提供统一的输入验证功能，确保数据安全性和一致性
"""
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
        验证后的优先级（1-4）
        
    Raises:
        ValueError: 优先级无效
    """
    try:
        priority = int(priority)
    except (ValueError, TypeError):
        raise ValueError("优先级必须是数字")
    
    if not 1 <= priority <= 4:
        raise ValueError("优先级必须在1-4之间")
    
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

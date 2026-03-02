"""
消息格式化工具

提供统一的消息格式化函数以保持一致的用户界面风格
"""

# 统一的分割线长度
DIVIDER_LENGTH = 20
DIVIDER = "━" * DIVIDER_LENGTH

def format_list_item(text: str, level: int = 0, marker: str = "•") -> str:
    """
    格式化列表项
    
    Args:
        text: 列表项内容
        level: 缩进级别
        marker: 列表标记符号
        
    Returns:
        格式化后的列表项
    """
    indent = "  " * level
    return f"{indent}{marker} {text}"

def format_section(title: str, *lines: str, use_divider: bool = True) -> str:
    """
    格式化消息节
    
    Args:
        title: 节标题
        *lines: 节内容（多行）
        use_divider: 是否使用分割线
        
    Returns:
        格式化后的消息节
    """
    parts = [title]
    if use_divider:
        parts.append(DIVIDER)
    parts.extend(lines)
    return "\n".join(parts)

def format_info_field(label: str, value: str, indent: int = 0) -> str:
    """
    格式化信息字段（键值对）
    
    Args:
        label: 字段标签
        value: 字段值
        indent: 缩进级别
        
    Returns:
        格式化后的字段
    """
    prefix = "  " * indent
    return f"{prefix}{label}: {value}"

def format_server_info(name: str, host: str, port: int = 22, username: str = "root", 
                      extra_info: str = "") -> str:
    """
    格式化服务器信息
    
    Args:
        name: 服务器名称
        host: 主机地址
        port: 端口号
        username: 用户名
        extra_info: 额外信息
        
    Returns:
        格式化后的服务器信息
    """
    lines = [
        format_info_field("名称", name),
        format_info_field("主机", f"{host}:{port}"),
        format_info_field("用户", username),
    ]
    if extra_info:
        lines.append(extra_info)
    return "\n".join(lines)

def format_error(message: str) -> str:
    """格式化错误消息"""
    return f"❌ {message}"

def format_success(message: str) -> str:
    """格式化成功消息"""
    return f"✅ {message}"

def format_warning(message: str) -> str:
    """格式化警告消息"""
    return f"⚠️ {message}"

def format_info(message: str) -> str:
    """格式化提示消息"""
    return f"💡 {message}"

def format_status(message: str) -> str:
    """格式化状态消息"""
    return f"🖥️ {message}"

def format_server_added(name: str, host: str, port: int, username: str, auth_type: str) -> str:
    """
    格式化服务器添加成功消息
    
    Args:
        name: 服务器名称
        host: 主机地址
        port: 端口号
        username: 用户名
        auth_type: 认证类型（password/key/agent）
        
    Returns:
        格式化后的成功消息
    """
    auth_display = {
        "password": "密码",
        "key": "密钥",
        "agent": "SSH Agent"
    }.get(auth_type, auth_type)
    
    return (
        f"✅ 服务器添加成功！\n"
        f"{DIVIDER}\n"
        f"名称: {name}\n"
        f"主机: {host}:{port}\n"
        f"用户: {username}\n"
        f"认证: {auth_display}\n"
        f"{DIVIDER}\n"
        f"使用 /ssh {name} 连接"
    )

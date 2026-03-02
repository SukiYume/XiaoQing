"""
输入验证工具

提供各种输入验证函数以提高代码健壮性
"""

import re

def validate_server_name(name: str) -> tuple[bool, str]:
    """
    验证服务器名称
    
    Args:
        name: 服务器名称
        
    Returns:
        (is_valid, error_message)
    """
    if not name:
        return False, "服务器名称不能为空"
    
    if len(name) > 50:
        return False, "服务器名称长度不能超过 50 个字符"
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return False, "服务器名称只能包含字母、数字、下划线和连字符"
    
    return True, ""

def validate_port(port_str: str) -> tuple[bool, int, str]:
    """
    验证并解析端口号
    
    Args:
        port_str: 端口号字符串
        
    Returns:
        (is_valid, port_number, error_message)
    """
    try:
        port = int(port_str)
        if 1 <= port <= 65535:
            return True, port, ""
        return False, 0, "端口号必须在 1-65535 之间"
    except ValueError:
        return False, 0, "端口号必须是有效的数字"

def validate_command(command: str) -> tuple[bool, str]:
    """
    验证命令长度
    
    Args:
        command: 要执行的命令
        
    Returns:
        (is_valid, error_message)
    """
    if not command:
        return False, "命令不能为空"
    
    if len(command) > 10000:
        return False, "命令过长（最大 10000 字符）"
    
    return True, ""

def validate_hostname(hostname: str) -> tuple[bool, str]:
    """
    验证主机名或 IP 地址格式
    
    Args:
        hostname: 主机名或 IP 地址
        
    Returns:
        (is_valid, error_message)
    """
    if not hostname:
        return False, "主机地址不能为空"
    
    if len(hostname) > 253:
        return False, "主机地址过长"
    
    # 简单的 IP 或域名格式检查
    # IPv4 pattern
    ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    # 域名 pattern (simplified)
    domain_pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
    
    if re.match(ipv4_pattern, hostname):
        # 验证 IPv4 每个段是否在 0-255
        parts = hostname.split('.')
        if all(0 <= int(part) <= 255 for part in parts):
            return True, ""
        return False, "IP 地址格式无效"
    
    if re.match(domain_pattern, hostname):
        return True, ""
    
    # 可能是 IPv6 或其他格式，简单允许
    if ':' in hostname:  # IPv6
        return True, ""
    
    return False, "主机地址格式无效"

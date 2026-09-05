"""服务器名称、端口、命令和主机地址的输入验证。"""

import ipaddress
import re

_SERVER_NAME_RE  = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_USERNAME_RE     = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)


def validate_server_name(name: str) -> tuple[bool, str]:
    """验证服务器别名，返回是否合法及错误说明。"""
    if not isinstance(name, str) or not name:
        return False, "服务器名称不能为空"

    if len(name) > 50:
        return False, "服务器名称长度不能超过 50 个字符"

    if _SERVER_NAME_RE.fullmatch(name) is None:
        return False, "服务器名称只能包含字母、数字、下划线和连字符"

    return True, ""


def validate_port(port_str: str) -> tuple[bool, int, str]:
    """把纯十进制文本解析为有效 TCP 端口。"""
    if not isinstance(port_str, str) or re.fullmatch(r"[0-9]+", port_str) is None:
        return False, 0, "端口号必须是有效的数字"

    port = int(port_str)
    if 1 <= port <= 65535:
        return True, port, ""
    return False, 0, "端口号必须在 1-65535 之间"


def validate_username(username: str) -> tuple[bool, str]:
    """验证聊天命令提供的 SSH 用户名，避免空白和参数注入。"""

    if not isinstance(username, str) or not username:
        return False, "SSH 用户名不能为空"
    if len(username) > 64:
        return False, "SSH 用户名长度不能超过 64 个字符"
    if _USERNAME_RE.fullmatch(username) is None:
        return False, "SSH 用户名只能包含字母、数字、点、下划线和连字符"
    return True, ""


def validate_command(command: str) -> tuple[bool, str]:
    """拒绝空命令和超过输入上限的命令。"""
    if not isinstance(command, str) or not command.strip():
        return False, "命令不能为空"

    if len(command) > 10000:
        return False, "命令过长（最大 10000 字符）"

    return True, ""


def validate_hostname(hostname: str) -> tuple[bool, str]:
    """验证 IPv4、IPv6 或由合法标签组成的 DNS 主机名。"""
    if not isinstance(hostname, str) or not hostname:
        return False, "主机地址不能为空"

    if len(hostname) > 253:
        return False, "主机地址过长"

    if hostname != hostname.strip() or any(character.isspace() for character in hostname):
        return False, "主机地址不能包含空白字符"

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if ":" in hostname:
            return False, "IPv6 地址格式无效"
    else:
        return True, ""

    labels = hostname.split(".")
    if labels and all(_DOMAIN_LABEL_RE.fullmatch(label) is not None for label in labels):
        # 四段纯数字应按 IPv4 校验，不能回退成普通 DNS 名称。
        if len(labels) == 4 and all(label.isascii() and label.isdigit() for label in labels):
            return False, "IP 地址格式无效"
        return True, ""

    return False, "主机地址格式无效"

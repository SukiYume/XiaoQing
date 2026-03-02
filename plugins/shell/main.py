"""
终端命令执行插件

仅管理员可用。包含以下安全措施：
1. 命令白名单（可配置）
2. 执行超时
3. 输出截断
4. 基本的命令注入防护

安全策略说明：
- 仅允许执行白名单中的命令
- 默认超时 30 秒
- 输出最大 4000 字符
- 禁止命令链接符（&&, ||, ;, |）除非在白名单
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import sys
from typing import Any, Optional

from core.plugin_base import segments, text
from core.args import parse

logger = logging.getLogger(__name__)

# 从配置文件导入常量
from .config import (
    DEFAULT_WHITELIST,
    DANGEROUS_PATTERNS,
    DEFAULT_TIMEOUT,
    MAX_OUTPUT_LENGTH,
)

def init(context=None) -> None:
    """插件初始化"""
    logger.info("Shell plugin initialized")

# ============================================================
# 配置获取
# ============================================================

def _get_config(context) -> dict[str, Any]:
    """获取插件配置"""
    return context.secrets.get("plugins", {}).get("shell", {})

def _get_whitelist(context) -> set[str]:
    """
    获取命令白名单。
    
    支持两种模式（通过 secrets.json 的 whitelist_mode 配置）：
    - "replace": 完全替换默认白名单（默认行为）
    - "extend": 在默认白名单基础上追加自定义命令
    
    示例配置:
    {
        "plugins": {
            "shell": {
                "whitelist": ["custom_cmd1", "custom_cmd2"],
                "whitelist_mode": "extend"
            }
        }
    }
    """
    config = _get_config(context)
    custom_list = config.get("whitelist", [])
    mode = config.get("whitelist_mode", "replace")  # 默认为 replace 保持向后兼容
    
    if not custom_list:
        return DEFAULT_WHITELIST
    
    custom_set = set(custom_list)
    
    if mode == "extend":
        # 扩展模式：合并默认白名单和自定义命令
        return DEFAULT_WHITELIST | custom_set
    else:
        # 替换模式：仅使用自定义命令
        return custom_set

def _get_timeout(context) -> int:
    """获取执行超时"""
    config = _get_config(context)
    return int(config.get("timeout", DEFAULT_TIMEOUT))

def _is_whitelist_disabled(context) -> bool:
    """检查是否禁用白名单（危险）

    Note: Even when whitelist is disabled, DANGEROUS_PATTERNS blacklist
    is always enforced as a safety net.
    """
    config = _get_config(context)
    disabled = config.get("disable_whitelist", False)
    if disabled:
        logger.warning("Shell whitelist is disabled - DANGEROUS_PATTERNS blacklist still enforced")
    return disabled

# ============================================================
# 安全检查
# ============================================================

def _split_command(cmd_line: str) -> Optional[list[str]]:
    """安全拆分命令参数"""
    try:
        parts = shlex.split(cmd_line)
    except ValueError:
        return None
    return parts if parts else None

def _extract_command(cmd_line: str) -> Optional[str]:
    """提取命令名"""
    parts = _split_command(cmd_line)
    if not parts:
        return None
    return parts[0].split("/")[-1]

def _check_dangerous_patterns(cmd_line: str) -> Optional[str]:
    """检查危险模式"""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_line):
            return f"包含危险模式: {pattern}"
    return None

def _validate_command(cmd_line: str, context) -> Optional[str]:
    """
    验证命令是否可执行。

    返回:
        None 表示可以执行
        str 表示拒绝原因
    """
    if not cmd_line.strip():
        return "命令不能为空"

    # 检查危险模式
    danger = _check_dangerous_patterns(cmd_line)
    if danger:
        return danger

    # 提取命令名
    cmd_name = _extract_command(cmd_line)
    if not cmd_name:
        return "无法解析命令"

    # 白名单检查
    if not _is_whitelist_disabled(context):
        whitelist = _get_whitelist(context)
        if cmd_name not in whitelist:
            return f"命令 '{cmd_name}' 不在白名单中"

    return None

# ============================================================
# 命令执行
# ============================================================

def _smart_decode(data: bytes) -> str:
    """
    智能解码字节数据。
    
    Windows 中文系统命令输出通常是 GBK 编码，
    先尝试 GBK，失败则 fallback 到 UTF-8。
    """
    if not data:
        return ""
    
    # Windows 系统优先尝试 GBK
    if sys.platform == "win32":
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            pass
    
    # 尝试 UTF-8
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    
    # 最后使用 latin-1（不会失败）
    return data.decode("latin-1")

async def _execute_command(args: list[str], timeout: int) -> tuple[int, str, str]:
    """
    异步执行命令。

    返回: (返回码, stdout, stderr)
    """
    proc = await asyncio.create_subprocess_exec(
        args[0],
        *args[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        
        # 智能解码：Windows 中文系统使用 GBK，否则使用 UTF-8
        stdout_str = _smart_decode(stdout_bytes)
        stderr_str = _smart_decode(stderr_bytes)
        
        return proc.returncode or 0, stdout_str, stderr_str
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"命令执行超时（{timeout}秒）"

def _truncate(text: str, max_len: int = MAX_OUTPUT_LENGTH) -> str:
    """截断输出"""
    if len(text) <= max_len:
        return text
    half = max_len // 2 - 20
    return text[:half] + f"\n\n... 省略 {len(text) - max_len} 字符 ...\n\n" + text[-half:]

# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        parsed = parse(args)
        cmd_line = args.strip()
        
        # 子命令路由
        if not parsed or not cmd_line:
            return segments(_show_help(context))
        
        first = parsed.first.lower()
        
        # 帮助命令
        if first in {"help", "帮助", "?", "-h", "--help"}:
            return segments(_show_help(context))
        
        # 列出白名单
        if first in {"list", "列表", "-l", "--list"}:
            return _list_whitelist(context)
        
        # 执行命令
        # 验证命令
        error = _validate_command(cmd_line, context)
        if error:
            return segments(f"❌ 拒绝执行: {error}")
        
        cmd_args = _split_command(cmd_line)
        if not cmd_args:
            return segments("❌ 无法解析命令参数")
        
        # 执行命令
        logger.info("Shell exec: %s (user: %s)", cmd_line, event.get("user_id"))
        timeout = _get_timeout(context)
        
        code, stdout, stderr = await _execute_command(cmd_args, timeout)
        
        # 格式化输出
        output_parts = []
        if stdout:
            output_parts.append(f"📤 stdout:\n{_truncate(stdout)}")
        if stderr:
            output_parts.append(f"⚠️ stderr:\n{_truncate(stderr)}")
        if not output_parts:
            output_parts.append("(无输出)")
        
        status = "✅" if code == 0 else "❌"
        header = f"{status} 返回码: {code}\n"
        logger.info("Shell exec completed: code=%d, user=%s", code, event.get("user_id"))
        return segments(header + "\n".join(output_parts))
        
    except Exception as e:
        logger.exception("Shell handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help(context) -> str:
    """显示帮助信息"""
    whitelist_status = "已禁用（危险模式）" if _is_whitelist_disabled(context) else "已启用"
    timeout = _get_timeout(context)
    
    return (
        "💻 Shell 命令执行插件\n"
        "═══════════════════════\n\n"
        "📌 基本用法:\n\n"
        "1️⃣ /shell <命令>\n"
        "   执行终端命令\n\n"
        "2️⃣ /shell help\n"
        "   显示此帮助信息\n\n"
        "3️⃣ /shell list\n"
        "   查看允许的命令白名单\n\n"
        f"🔒 安全设置:\n"
        f"   • 白名单模式: {whitelist_status}\n"
        f"   • 执行超时: {timeout}秒\n"
        f"   • 输出限制: {MAX_OUTPUT_LENGTH}字符\n"
        f"   • 命令链接符: 已禁用\n\n"
        "💡 示例:\n"
        "   /shell ls -la\n"
        "   /shell pwd\n"
        "   /shell python --version\n"
        "   /shell ping -c 3 google.com\n\n"
        "⚠️ 注意: 此插件仅管理员可用\n"
        "═══════════════════════"
    )

def _list_whitelist(context) -> list[dict[str, Any]]:
    """列出白名单"""
    if _is_whitelist_disabled(context):
        return segments("⚠️ 白名单已禁用（危险模式）")

    whitelist = sorted(_get_whitelist(context))
    lines = ["允许的命令:"]
    lines.append(", ".join(whitelist))
    return segments("\n".join(lines))

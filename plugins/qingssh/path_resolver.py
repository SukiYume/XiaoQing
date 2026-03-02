"""
路径解析器

集中管理 SSH 会话中的路径解析和命令构建逻辑。

核心原则：
- CWD 始终存储为服务器上的绝对路径（通过 pwd 获取）
- cd 命令附加 pwd 以获取真实绝对路径
- 所有命令（包括 cd）都使用 CWD 前缀确保正确的工作目录
"""

import shlex
from typing import Optional


def is_cd_command(text: str) -> bool:
    """判断是否为 cd 命令"""
    stripped = text.strip()
    return stripped == "cd" or stripped.startswith("cd ")


def build_command(
    text: str,
    cwd: Optional[str] = None,
    env_vars: Optional[dict] = None,
) -> str:
    """
    构建带工作目录和环境变量前缀的完整 shell 命令。

    所有命令（包括 cd）都会添加 cwd 前缀。
    cd 命令会额外附加 pwd 以获取执行后的绝对路径。

    Args:
        text: 用户输入的原始命令
        cwd: 当前工作目录（绝对路径或 None）
        env_vars: 环境变量字典

    Returns:
        构建好的完整命令字符串
    """
    parts = []

    # 1. cd 到当前工作目录
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")

    # 2. 设置环境变量
    if env_vars:
        env_exports = " ".join(
            f"{k}={shlex.quote(v)}" for k, v in env_vars.items()
        )
        parts.append(f"export {env_exports}")

    # 3. 用户命令
    parts.append(text.strip())

    # 4. 如果是 cd 命令，附加 pwd 获取绝对路径
    if is_cd_command(text):
        parts.append("pwd")

    return " && ".join(parts)


def extract_cwd_from_output(accumulated_output: str) -> Optional[str]:
    """
    从 cd 命令的输出中提取 pwd 结果（绝对路径）。

    pwd 输出是最后一行以 / 开头的路径。

    Args:
        accumulated_output: 命令的完整输出

    Returns:
        绝对路径字符串，若无法提取则返回 None
    """
    if not accumulated_output:
        return None

    # 从后往前找第一行以 / 开头的非空行
    lines = accumulated_output.strip().splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("/"):
            return stripped

    return None


def resolve_remote_path(filename: str, cwd: Optional[str] = None) -> str:
    """
    将文件名解析为远程服务器上的完整路径。

    Args:
        filename: 文件名
        cwd: 当前工作目录（绝对路径）

    Returns:
        完整的远程文件路径
    """
    # 已经是绝对路径
    if filename.startswith("/"):
        return filename

    # 有 CWD 时拼接
    if cwd:
        return f"{cwd.rstrip('/')}/{filename}"

    return filename

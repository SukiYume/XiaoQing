"""集中处理 SSH 会话中的工作目录、环境变量和远程路径。

核心原则：
- CWD 始终保存远端绝对路径；
- 独立的 `cd` 执行后追加带唯一标记的工作目录探针，用真实结果更新 CWD；
- 每条命令都显式应用当前 CWD 和环境变量。
"""

import re
import shlex
from collections.abc import Mapping

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_CWD_MARKER = "__XQ_CWD__"
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "&", "|", ">", ">>", "<", "<<"})


def is_cd_command(text: str) -> bool:
    """判断输入是否为独立的 `cd` 命令。"""

    if "\n" in text or "\r" in text:
        return False
    try:
        tokens = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return False
    if not tokens or tokens[0] != "cd":
        return False
    return not any(
        token in _SHELL_CONTROL_TOKENS
        or any(operator in token for operator in _SHELL_CONTROL_TOKENS)
        for token in tokens[1:]
    )


def build_command(
    text: str,
    cwd: str | None = None,
    env_vars: Mapping[str, str] | None = None,
) -> str:
    """构造应用当前目录和环境变量的完整远端 shell 命令。"""

    parts: list[str] = []

    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")

    if env_vars:
        invalid_names = [name for name in env_vars if _ENV_NAME_RE.fullmatch(name) is None]
        if invalid_names:
            raise ValueError("environment variable name is invalid")
        env_exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_vars.items())
        parts.append(f"export {env_exports}")

    parts.append(text.strip())

    if is_cd_command(text):
        parts.append(f"printf '%s%s\\n' '{_CWD_MARKER}' \"$(pwd -P)\"")

    return " && ".join(parts)


def extract_cwd_from_output(accumulated_output: str | None) -> str | None:
    """从唯一标记探针输出中提取远端绝对工作目录。"""
    if not accumulated_output:
        return None

    lines = accumulated_output.splitlines()
    for line in reversed(lines):
        if not line.startswith(_CWD_MARKER):
            continue
        path = line[len(_CWD_MARKER) :].rstrip("\r")
        if path.startswith("/"):
            return path

    return None


def strip_cwd_markers(text: str) -> str:
    """从用户可见输出中移除内部 CWD 探针行。"""

    if not text or _CWD_MARKER not in text:
        return text
    return "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith(_CWD_MARKER)
    )


def resolve_remote_path(filename: str, cwd: str | None = None) -> str:
    """相对文件名使用当前远端目录解析，绝对路径原样返回。"""
    if filename.startswith("/"):
        return filename

    if cwd:
        return f"{cwd.rstrip('/')}/{filename}"

    return filename

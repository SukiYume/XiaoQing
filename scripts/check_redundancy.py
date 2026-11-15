"""重复代码检查：非测试代码里不能有两个函数体一模一样的函数。

每次改动都可能在别处留下一份差不多的实现；写法完全相同的函数是其中最确定的一种，这里随测试
把它拦下。比较的是去掉文档字符串后的语法树，名字、参数和注释不同不影响判断；函数体不到
``MIN_BODY_LINES`` 行的小函数和只有 ``...`` / ``pass`` / ``raise NotImplementedError`` 的接口
声明不算。

确实要各留一份的写进 ``ALLOWED``，并写明原因。

用法::

    python scripts/check_redundancy.py            # 有重复时退出码为 1
    python scripts/check_redundancy.py --list     # 连同已允许的一起列出
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_DIR       = Path(__file__).resolve().parents[1]
SCAN_ROOTS     = ("core", "plugins", "scripts", "main.py")
MIN_BODY_LINES = 8

# 有意各留一份的函数：名字 → 原因。
ALLOWED: dict[str, str] = {
    "_attach_windows_job"   : "生产启动脚本和 UAT 脚本都要能单独运行，不互相导入",
    "_terminate_windows_job": "生产启动脚本和 UAT 脚本都要能单独运行，不互相导入",
}


@dataclass(frozen=True)
class Function:
    path : str
    line : int
    name : str
    lines: int


def source_files(root: Path = REPO_DIR) -> list[Path]:
    """要检查的 Python 文件：已跟踪和未忽略的，工作区里已经删掉的不算。"""

    listed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", *SCAN_ROOTS],
        cwd            = root,
        capture_output = True,
        text           = True,
        encoding       = "utf-8",
        check          = True,
    ).stdout
    return [
        root / line
        for line in listed.splitlines()
        if line.endswith(".py") and (root / line).is_file()
    ]


def _body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body  = list(node.body)
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        body = body[1:]
    return body


def _is_declaration(body: list[ast.stmt]) -> bool:
    """只有 ``...``、``pass`` 或 ``raise NotImplementedError`` 的接口声明。"""

    if len(body) != 1:
        return not body
    only = body[0]
    if isinstance(only, ast.Pass):
        return True
    if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
        return only.value.value is Ellipsis
    if isinstance(only, ast.Raise) and only.exc is not None:
        target = only.exc.func if isinstance(only.exc, ast.Call) else only.exc
        return isinstance(target, ast.Name) and target.id == "NotImplementedError"
    return False


def duplicate_groups(files: list[Path], root: Path = REPO_DIR) -> list[list[Function]]:
    """函数体完全一样的函数，每组至少两个；大的组在前。"""

    groups: dict[str, list[Function]] = defaultdict(list)
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = _body(node)
            if _is_declaration(body):
                continue
            lines = (body[-1].end_lineno or body[-1].lineno) - body[0].lineno + 1
            if lines < MIN_BODY_LINES:
                continue
            digest = hashlib.sha256(
                "\n".join(ast.dump(statement) for statement in body).encode("utf-8")
            ).hexdigest()
            groups[digest].append(
                Function(path.relative_to(root).as_posix(), node.lineno, node.name, lines)
            )
    found = [
        sorted(group, key=lambda f: (f.path, f.line)) for group in groups.values() if len(group) > 1
    ]
    return sorted(found, key=lambda group: (-group[0].lines * len(group), group[0].path))


def unexpected(groups: list[list[Function]]) -> list[list[Function]]:
    return [group for group in groups if not all(function.name in ALLOWED for function in group)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="连同已允许的一起列出")
    args   = parser.parse_args(argv)
    groups = duplicate_groups(source_files())
    shown  = groups if args.list else unexpected(groups)
    for group in shown:
        reason = ALLOWED.get(group[0].name, "") if all(f.name in ALLOWED for f in group) else ""
        print(f"{group[0].lines} 行 × {len(group)}" + (f"（已允许：{reason}）" if reason else ""))
        for function in group:
            print(f"  {function.path}:{function.line}  {function.name}")
    bad = unexpected(groups)
    if bad:
        print(
            f"发现 {len(bad)} 组函数体完全相同的函数：留一份、其余改成调用它；确实要各留一份的写进 ALLOWED。"
        )
        return 1
    if not args.list:
        print("没有函数体完全相同的函数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

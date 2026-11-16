"""重复代码检查：非测试代码里不能有两个函数体一模一样的函数。

每次改动都可能在别处留下一份差不多的实现；写法完全相同的函数是其中最确定的一种，这里随测试
把它拦下（见文件末尾的门禁测试）。比较的是去掉文档字符串后的语法树，名字、参数和注释不同不影响
判断；函数体不到 ``MIN_BODY_LINES`` 行的小函数和只有 ``...`` / ``pass`` /
``raise NotImplementedError`` 的接口声明不算。

确实要各留一份的写进 ``ALLOWED``，并写明原因。
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_DIR       = Path(__file__).resolve().parents[2]
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


_BODY = "\n".join(f"    total += value * {n}" for n in range(MIN_BODY_LINES))


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def test_identical_bodies_are_grouped_whatever_the_name_and_docstring(tmp_path: Path) -> None:
    first = _write(
        tmp_path,
        "a.py",
        f'def scale(value):\n    """第一份。"""\n    total = 0\n{_BODY}\n    return total\n',
    )
    second = _write(
        tmp_path,
        "b.py",
        f"class Box:\n    def grow(self, value):\n        # 换了名字、放进类里、没有文档字符串\n"
        f"        total = 0\n{_BODY.replace('    total', '        total')}\n        return total\n",
    )
    other = _write(
        tmp_path, "c.py", f"def other(value):\n    total = 1\n{_BODY}\n    return total\n"
    )
    groups = duplicate_groups([first, second, other], tmp_path)
    assert [[(f.path, f.name) for f in group] for group in groups] == [
        [("a.py", "scale"), ("b.py", "grow")]
    ]
    assert unexpected(groups) == groups


def test_small_functions_and_interface_declarations_are_not_duplicates(tmp_path: Path) -> None:
    stubs = "\n".join(
        f"def stub{n}(a, b):\n    {body}\n"
        for n, body in enumerate(
            ["...", "pass", "raise NotImplementedError", "raise NotImplementedError()"] * 2
        )
    )
    small = (
        "def one(x):\n    y = x + 1\n    return y\n\n\ndef two(x):\n    y = x + 1\n    return y\n"
    )
    files = [_write(tmp_path, "stubs.py", stubs), _write(tmp_path, "small.py", small)]
    assert duplicate_groups(files, tmp_path) == []


def test_allowed_names_need_a_reason_and_do_not_fail_the_check(tmp_path: Path) -> None:
    name   = next(iter(ALLOWED))
    text   = f"def {name}(value):\n    total = 0\n{_BODY}\n    return total\n"
    files  = [_write(tmp_path, "x.py", text), _write(tmp_path, "y.py", text)]
    groups = duplicate_groups(files, tmp_path)
    assert len(groups) == 1 and unexpected(groups) == []
    assert all(reason.strip() for reason in ALLOWED.values())


def test_repository_has_no_unexplained_duplicate_functions() -> None:
    """门禁：新增一份和已有函数一模一样的实现时在这里失败。留一份、其余改成调用它。"""

    found = unexpected(duplicate_groups(source_files()))
    assert found == [], "\n".join(
        f"{group[0].lines} 行：" + "、".join(f"{f.path}:{f.line} {f.name}" for f in group)
        for group in found
    )

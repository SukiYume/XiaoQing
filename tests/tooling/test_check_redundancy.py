"""重复代码检查：函数体一模一样的函数会被找出来；仓库里不能有没登记原因的重复。"""

from __future__ import annotations

from pathlib import Path

from scripts.check_redundancy import (
    ALLOWED,
    MIN_BODY_LINES,
    duplicate_groups,
    source_files,
    unexpected,
)

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

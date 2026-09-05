"""统一代码排版：紧凑换行与相邻赋值对齐，支持 CI 只读检查。

先通过 Ruff 规范缩进、导入外的表达式排版，再用词法位置对齐相邻赋值。
字符串、注释和跨行表达式内容保持原样；AST 校验保障格式操作的语义边界。
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pygments.lexers import get_lexer_by_name
from pygments.token import Operator, Text

ROOT            = Path(__file__).resolve().parents[1]
LINE_LENGTH     = 100
SOURCE_SUFFIXES = {".py", ".js", ".html", ".css", ".ps1", ".sh", ".vbs"}


def align_assignments(source: str) -> str:
    """仅对齐同缩进且连续出现的单赋值行，保留独立逻辑块的边界。"""
    lines = source.splitlines(keepends=True)
    candidates: dict[int, list[int]] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.OP and token.string == "=":
            row, column = token.start
            candidates.setdefault(row - 1, []).append(column)
    assignments: list[tuple[int, int]] = []
    for row, columns in candidates.items():
        if len(columns) != 1:
            continue
        column = columns[0]
        prefix = lines[row][:column]
        if not re.fullmatch(r"\s*[\w.]+(?:\[[^\n]+\])?(?:\s*:\s*[^=]+)?\s*", prefix):
            continue
        assignments.append((row, column))
    return _align_lines(source, assignments)


def _align_lines(source: str, assignments: list[tuple[int, int]]) -> str:
    """各语言共用局部对齐算法；候选位置由各自词法规则提供。"""
    lines = source.splitlines(keepends=True)
    groups: list[list[tuple[int, int]]] = []
    for row, column in assignments:
        indent = len(lines[row]) - len(lines[row].lstrip())
        if groups:
            previous, _ = groups[-1][-1]
            previous_indent = len(lines[previous]) - len(lines[previous].lstrip())
            if row == previous + 1 and indent == previous_indent:
                groups[-1].append((row, column))
                continue
        groups.append([(row, column)])
    for group in groups:
        if len(group) < 2:
            continue
        width        = max(len(lines[row][:column].rstrip()) for row, column in group)
        replacements = {
            row: lines[row][:column].rstrip().ljust(width)
            + " = "
            + lines[row][column + 1 :].lstrip()
            for row, column in group
        }
        if all(len(line.rstrip("\r\n")) <= LINE_LENGTH for line in replacements.values()):
            for row, line in replacements.items():
                lines[row] = line
    return "".join(lines)


def align_script(source: str, suffix: str) -> str:
    """按语言词法边界对齐声明，模板正文和 Shell 赋值语法保持原样。"""
    if suffix not in {".js", ".ps1"}:
        return source
    lexer  = get_lexer_by_name("javascript" if suffix == ".js" else "powershell")
    tokens = list(lexer.get_tokens_unprocessed(source))
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    pattern = (
        r"\s*(?:const|let|var)\s+[\w$]+\s*"
        if suffix == ".js"
        else r"\s*(?:\[[\w.\[\]]+\])?\$[\w:]+\s*"
    )
    assignments: list[tuple[int, int]] = []
    for offset, kind, value in tokens:
        if kind in Operator and value == "=":
            row    = bisect_right(offsets, offset) - 1
            column = offset - offsets[row]
            if re.fullmatch(pattern, lines[row][:column]):
                assignments.append((row, column))
    result = _align_lines(source, assignments)
    before = [(kind, value) for _, kind, value in tokens if kind not in Text.Whitespace]
    after  = [
        (kind, value)
        for _, kind, value in lexer.get_tokens_unprocessed(result)
        if kind not in Text.Whitespace
    ]
    if before != after:
        raise ValueError("脚本对齐改变了有效词法内容")
    return result


def format_source(source: str, path: Path) -> str:
    """通过同一规范生成工作区与 CI 的期望文本，并检查语法树一致性。"""
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--line-length",
            str(LINE_LENGTH),
            "--stdin-filename",
            str(path),
            "--config",
            str(ROOT / "pyproject.toml"),
            "-",
        ],
        input          = source,
        text           = True,
        encoding       = "utf-8",
        capture_output = True,
        check          = True,
    )
    result = align_assignments(process.stdout)
    if ast.dump(ast.parse(source)) != ast.dump(ast.parse(result)):
        raise ValueError(f"格式化改变了语法树：{path}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检查格式，保持文件原样")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    if args.paths:
        paths = sorted(
            {
                path
                for target in args.paths
                for path in (target.rglob("*") if target.is_dir() else [target])
                if path.suffix in SOURCE_SUFFIXES
            }
        )
    else:
        tracked = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        paths = sorted(
            {
                ROOT / name.decode("utf-8")
                for name in tracked.split(b"\0")
                if Path(name.decode("utf-8")).suffix in SOURCE_SUFFIXES
            }
        )

    def process(path: Path) -> tuple[Path, str, bool]:
        original = path.read_text(encoding="utf-8-sig")
        formatted = (
            format_source(original, path)
            if path.suffix == ".py"
            else align_script(original, path.suffix)
        )
        return path, formatted, formatted != original

    changed = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for path, formatted, differs in pool.map(process, paths):
            if not differs:
                continue
            changed += 1
            if args.check:
                print(f"需要格式化：{path.relative_to(ROOT)}")
            else:
                path.write_text(formatted, encoding="utf-8", newline="\n")
    print(f"检查 {len(paths)} 个代码文件，{'待调整' if args.check else '已调整'} {changed} 个。")
    return int(args.check and changed > 0)


if __name__ == "__main__":
    raise SystemExit(main())

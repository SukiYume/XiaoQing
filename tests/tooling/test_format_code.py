"""项目格式规则保留 Python 语义，并稳定对齐局部声明。"""

import ast
from pathlib import Path

from scripts.format_code import format_source


def test_colon_alignment_preserves_slices_strings_and_is_idempotent():
    source = """class Settings:
    x: int = 1
    longer: str = "http://example.com:a"

data = {
    "短": 1,
    "longer": values[1:3],
    tuple(values[2:4]): "literal: = unchanged",
}
if data:
    result = values[1:2]
"""
    result = format_source(source, Path("example.py"))
    assert ast.dump(ast.parse(result)) == ast.dump(ast.parse(source))
    assert "x     : int" in result
    rows = [
        line
        for line in result.splitlines()
        if line.startswith(('    "短"', '    "longer"', "    tuple"))
    ]
    assert len({line.index(": ") for line in rows}) == 1
    assert "values[1:3]" in result
    assert "literal: = unchanged" in result
    assert format_source(result, Path("example.py")) == result

"""Guard the reviewed plugins against reintroducing private shell lexers."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("relative_path", "core_symbol"),
    [
        ("plugins/dict/main.py", "tokenize"),
        ("plugins/choice/main.py", "tokenize"),
        ("plugins/github/main.py", "tokenize"),
        ("plugins/guess_number/main.py", "tokenize"),
        ("plugins/adnmb/main.py", "parse"),
        ("plugins/color/main.py", "parse"),
        ("plugins/qingssh/handlers.py", "tokenize"),
    ],
)
def test_reviewed_plugins_use_core_argument_layer(
    relative_path: str,
    core_symbol: str,
) -> None:
    """The seven reviewed parsers share core lexing and do not import shlex."""

    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    core_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.args"
        for alias in node.names
    }
    shlex_imports = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(alias.name == "shlex" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "shlex")
    ]

    assert core_symbol in core_imports
    assert shlex_imports == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "plugins/adnmb/main.py",
        "plugins/bot_core/main.py",
        "plugins/pendo/commands/operations.py",
        "plugins/pendo/services/ai_parser.py",
        "plugins/qingpet/commands/admin_commands.py",
        "plugins/qingpet/commands/advanced_commands.py",
        "plugins/qingpet/commands/basic_commands.py",
        "plugins/qingpet/commands/new_commands.py",
        "plugins/qingssh/ssh_manager.py",
        "plugins/xiaoqing_chat/memory/review_sessions.py",
    ],
)
def test_reviewed_integer_consumers_use_core_parser(relative_path: str) -> None:
    """Numeric consumers cannot restore the unsafe ``isdigit``/conversion split."""

    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    core_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.args"
        for alias in node.names
    }
    digit_predicates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"isdigit", "isdecimal", "isnumeric"}
    ]

    assert "parse_int" in core_imports
    assert digit_predicates == []

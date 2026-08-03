from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOUNDED_RUNTIME_FILES = (
    "core/ai.py",
    "core/onebot.py",
    "plugins/adnmb/adapi.py",
    "plugins/ads_paper/ads_client.py",
    "plugins/apod/main.py",
    "plugins/arxiv_filter/arxiv_today.py",
    "plugins/arxiv_filter/train_model/data_prep/step2_fetch_all_astro_ph.py",
    "plugins/arxiv_filter/train_model/data_prep/step3_build_dataset.py",
    "plugins/astro_tools/obj.py",
    "plugins/chat/main.py",
    "plugins/chime/main.py",
    "plugins/earthquake/main.py",
    "plugins/github/main.py",
    "plugins/signin/yingshi.py",
    "plugins/twitter/main.py",
    "plugins/voice/main.py",
    "plugins/wolframalpha/main.py",
)
HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "request"})
HTTP_CLIENT_NAMES = frozenset(
    {
        "aiohttp",
        "client",
        "http_client",
        "http_session",
        "requests",
        "session",
    }
)
UNBOUNDED_RESPONSE_MEMBERS = frozenset({"content", "iter_content", "json", "read", "text"})


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    values: list[str] = []
    while isinstance(node, ast.Attribute):
        values.append(node.attr.casefold())
        node = node.value
    if isinstance(node, ast.Name):
        values.append(node.id.casefold())
    return tuple(reversed(values))


def _looks_like_response_name(chain: tuple[str, ...]) -> bool:
    return any(
        part == "resp"
        or part.startswith("resp_")
        or part == "response"
        or part.startswith("response_")
        for part in chain
    )


@pytest.mark.parametrize("relative_path", BOUNDED_RUNTIME_FILES)
def test_runtime_http_paths_have_no_direct_unbounded_response_reads(
    relative_path: str,
) -> None:
    path = ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attribute_chain(node)
        if not chain:
            continue
        if node.attr.casefold() in HTTP_VERBS and any(
            part in HTTP_CLIENT_NAMES for part in chain[:-1]
        ):
            violations.append(f"line {node.lineno}: direct HTTP call {'.'.join(chain)}")
        if node.attr.casefold() in UNBOUNDED_RESPONSE_MEMBERS and _looks_like_response_name(
            chain[:-1]
        ):
            violations.append(f"line {node.lineno}: unbounded response read {'.'.join(chain)}")

    assert not violations, f"{relative_path}:\n" + "\n".join(violations)
    assert "bounded_http" in source or "safe_http" in source

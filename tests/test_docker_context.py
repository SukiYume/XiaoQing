from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _dockerignore_rules() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.rstrip("/")
    if fnmatchcase(path, normalized):
        return True
    return pattern.endswith("/") and (path == normalized or path.startswith(f"{normalized}/"))


def _is_ignored(path: str) -> bool:
    ignored = False
    for rule in _dockerignore_rules():
        negated = rule.startswith("!")
        pattern = rule[1:] if negated else rule
        if _matches(path, pattern):
            ignored = not negated
    return ignored


def test_docker_context_uses_a_runtime_source_allowlist() -> None:
    rules = _dockerignore_rules()
    assert rules[0] == "**"

    allowed_runtime_files = [
        "main.py",
        "requirements.txt",
        "requirements/python-3.13.lock",
        "pyproject.toml",
        "core/app.py",
        "plugins/codex/plugin.json",
        "plugins/pendo/web/static/js/api.js",
        "plugins/color/color.json",
        "config/config.json.example",
        "config/secrets.json.example",
    ]
    assert all(not _is_ignored(path) for path in allowed_runtime_files)


def test_docker_context_excludes_secrets_state_and_deprecated_code() -> None:
    excluded_local_files = [
        ".git/config",
        ".env",
        "code_review.md",
        "tests/test_app.py",
        "logs/bot.log",
        "config/config.json",
        "config/secrets.json",
        "plugins/minecraft/config.json",
        "plugins/pendo/data/pendo.db",
        "plugins/xiaoqing_chat/cache/state.json",
        "plugins/xiaoqing_chat/figures/example.png",
        "plugins/covid_deprecated/post_ucas_covid.py",
        "plugins/arxiv_filter/best_model-v1/model.bin",
        "plugins/arxiv_filter/train_model/month/cache/page.json",
        "plugins/arxiv_filter/train_model/arxiv_papers_with_abstract.csv",
        "plugins/pendo/runtime.log",
    ]
    assert all(_is_ignored(path) for path in excluded_local_files)


def test_dockerfile_never_copies_the_entire_workspace() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . ." not in dockerfile
    assert "COPY core/ ./core/" in dockerfile
    assert "COPY plugins/ ./plugins/" in dockerfile
    assert "COPY requirements/python-3.13.lock" in dockerfile
    assert "COPY config/*.example ./config/" in dockerfile

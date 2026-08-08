"""Regression tests for the installed console entry point."""

from __future__ import annotations

import sys

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib

import main as main_module

ROOT = REPOSITORY_ROOT


def test_main_module_is_declared_for_wheel_and_sdist() -> None:
    with (ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    assert pyproject["project"]["scripts"]["xiaoqing"] == "main:cli"
    assert "main" in pyproject["tool"]["setuptools"]["py-modules"]


def test_cli_help_exits_before_loading_runtime(monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "core.app", None)

    with pytest.raises(SystemExit) as exc_info:
        main_module.cli(["--help"])

    assert exc_info.value.code == 0
    assert "XiaoQing QQ Bot framework" in capsys.readouterr().out

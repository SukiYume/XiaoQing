"""Regression tests for license metadata and source availability."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_mit_license_text_and_metadata_are_present() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    with (ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]

    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted" in license_text
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]

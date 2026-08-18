"""Architecture gates for the shared external image and text boundaries."""

from __future__ import annotations

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT

_SHARED_IMAGE_CONSUMERS = (
    "plugins/adnmb/adapi.py",
    "plugins/apod/main.py",
    "plugins/codex/artifacts.py",
    "plugins/earthquake/main.py",
    "plugins/flickr/main.py",
    "plugins/jupyter/jupyter_manager.py",
    "plugins/twitter/main.py",
    "plugins/url_parser/main.py",
)
_SHARED_TEXT_CONSUMERS = (
    "plugins/adnmb/adapi.py",
    "plugins/chime/main.py",
    "plugins/earthquake/main.py",
    "plugins/flickr/main.py",
    "plugins/github/main.py",
    "plugins/minecraft/main.py",
    "plugins/qingssh/output_relay.py",
    "plugins/signin/yingshi.py",
)


def test_image_consumers_do_not_reimplement_pillow_validation() -> None:
    violations: list[str] = []
    for relative_path in _SHARED_IMAGE_CONSUMERS:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        if "from PIL" in source or "Image.open(" in source:
            violations.append(relative_path)
        assert "core.image_validation" in source, relative_path
    assert violations == []


def test_external_text_consumers_use_the_single_dual_budget_boundary() -> None:
    missing = [
        relative_path
        for relative_path in _SHARED_TEXT_CONSUMERS
        if "bounded_external_text" not in (ROOT / relative_path).read_text(encoding="utf-8")
    ]
    assert missing == []

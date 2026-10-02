"""Pendo 登录页和应用外壳的可访问性语义测试。"""

import pytest
from bs4 import BeautifulSoup

from tests.helpers.paths import REPOSITORY_ROOT


@pytest.fixture
def shell():
    html = REPOSITORY_ROOT / "plugins" / "pendo" / "web" / "static" / "index.html"
    return BeautifulSoup(html.read_text(encoding="utf-8"), "html.parser")


def test_shell_form_controls_have_accessible_names(shell) -> None:
    for control in shell.select("input, textarea, select"):
        if control.get("type") == "hidden":
            continue
        labels = shell.find_all("label", attrs={"for": control.get("id")})
        assert any(label.get_text(strip=True) for label in labels) or any(
            control.get(attribute) for attribute in ("aria-label", "aria-labelledby", "title")
        ), str(control)
    for element in shell.select("[aria-labelledby], [aria-describedby]"):
        for attribute in ("aria-labelledby", "aria-describedby"):
            for target in element.get(attribute, "").split():
                assert shell.find(id=target) is not None, target


def test_shell_exposes_keyboard_dialog_and_live_region_semantics(shell) -> None:
    buttons = shell.find_all("button")
    assert buttons
    assert all(button.get("type") == "button" for button in buttons)
    assert shell.find("main").get("tabindex") == "-1"
    assert shell.find(id="login-error").get("role") == "alert"
    assert shell.find(id="toast-container").get("role") == "status"
    dialog = shell.find(id="modal-overlay")
    assert dialog.get("role") == "dialog"
    assert dialog.get("aria-modal") == "true"
    assert shell.find(attrs={"autocomplete": "one-time-code"}) is not None

"""Pendo Web 静态外壳、表单控件和订阅生命周期的可访问性回归。"""

from __future__ import annotations

import re
from typing import Final

from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
STATIC_ROOT: Final = ROOT / "plugins" / "pendo" / "web" / "static"


CONTROL_RE: Final = re.compile(r"<(?:input|textarea|select)\b[^>]*>", re.IGNORECASE | re.DOTALL)


def _has_accessible_name(source: str, tag: str) -> bool:
    lowered = tag.lower()
    if 'type="hidden"' in lowered or "type='hidden'" in lowered:
        return True
    if 'style="display:none' in lowered or "style='display:none" in lowered:
        return True
    if "aria-label=" in lowered or "aria-labelledby=" in lowered or "title=" in lowered:
        return True
    id_match = re.search(r'id="([^"]+)"', tag)
    if not id_match:
        return False
    return bool(re.search(rf"<label[^>]+for=\"{re.escape(id_match.group(1))}\"", source))


def test_static_form_controls_have_accessible_names() -> None:
    """每个可见原生输入控件都必须有可计算的可访问名称。"""

    missing: list[str] = []
    for path in [*STATIC_ROOT.rglob("*.js"), STATIC_ROOT / "index.html"]:
        source = path.read_text(encoding="utf-8")
        for match in CONTROL_RE.finditer(source):
            tag = match.group(0)
            if _has_accessible_name(source, tag):
                continue
            line = source.count("\n", 0, match.start()) + 1
            missing.append(f"{path.relative_to(ROOT)}:{line}: {tag.strip()[:160]}")

    assert missing == []


def test_index_shell_exposes_navigation_dialog_and_live_region_semantics() -> None:
    """入口页的动态错误、提示、主内容和模态框必须向辅助技术声明职责。"""

    source = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    buttons = re.findall(r"<button\b[^>]*>", source, flags=re.IGNORECASE | re.DOTALL)

    assert buttons and all("type=" in button.lower() for button in buttons)
    assert 'id="content" class="content" tabindex="-1"' in source
    assert re.search(r'<p\b(?=[^>]*id="login-error")(?=[^>]*role="alert")[^>]*>', source, re.DOTALL)
    assert re.search(
        r'<div\b(?=[^>]*id="toast-container")(?=[^>]*role="status")[^>]*>',
        source,
        re.DOTALL,
    )
    assert re.search(
        r'<div\b(?=[^>]*id="modal-overlay")(?=[^>]*role="dialog")[^>]*>',
        source,
        re.DOTALL,
    )
    assert 'aria-modal="true"' in source
    assert 'autocomplete="one-time-code"' in source

    for raw_targets in re.findall(r'aria-(?:labelledby|describedby)="([^"]+)"', source):
        for target in raw_targets.split():
            assert f'id="{target}"' in source


def test_shared_styles_follow_runtime_component_contracts() -> None:
    """全局样式只保留真实组件契约，并为键盘与减弱动画偏好提供反馈。"""

    source = (STATIC_ROOT / "css" / "app.css").read_text(encoding="utf-8")

    for selector in (
        ".pselect-trigger:focus-visible",
        ".modal-header h3",
        ".toast.toast-show",
        ".toast-dismiss",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert selector in source

    for stale_selector in (
        ".btn-success",
        ".form-row",
        ".summary-cards",
        ".calendar-wrapper",
        ".modal-header h2",
        ".toast.removing",
        ".toast-close",
        "@keyframes toastSlideIn",
        "@keyframes toastSlideOut",
    ):
        assert stale_selector not in source

    section_numbers = [
        int(number) for number in re.findall(r"^\s+(\d+)\. ", source, flags=re.MULTILINE)
    ]
    assert section_numbers == list(range(1, len(section_numbers) + 1))


def test_data_change_subscriptions_share_an_idempotent_lifecycle() -> None:
    """各页面复用可幂等取消的数据变更订阅，避免重复监听。"""

    ui_source = (STATIC_ROOT / "js" / "utils" / "ui.js").read_text(encoding="utf-8")
    assert "export function subscribeDataChanges" in ui_source
    assert "if (!active) return;" in ui_source

    for page_name in ("dashboard", "diary", "events", "ledger", "notes", "search", "tasks"):
        source = (STATIC_ROOT / "js" / "pages" / f"{page_name}.js").read_text(encoding="utf-8")
        assert "subscribeDataChanges" in source
        assert "_unsubscribeDataChanges?.();" in source
        assert "addEventListener('pendo-data-changed'" not in source

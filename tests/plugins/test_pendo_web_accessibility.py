"""Accessibility regressions for pendo Web form controls."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "plugins" / "pendo" / "web" / "static"


CONTROL_RE = re.compile(r"<(?:input|textarea|select)\b[^>]*>", re.IGNORECASE | re.DOTALL)


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


def test_static_form_controls_have_accessible_names():
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


def test_shared_form_builder_binds_visible_labels_to_controls():
    source = (STATIC_ROOT / "js" / "components" / "form.js").read_text(encoding="utf-8")

    assert 'id="${fieldId}"' in source
    assert 'for="${fieldId}"' in source
    assert 'aria-label="${ariaLabel}"' in source
    assert "labelledBy: labelId" in source

"""用于安全识别条目边界和展示字段的小型结构化 BibTeX 解析器。"""

from __future__ import annotations

import re
from dataclasses import dataclass


class BibTeXParseError(ValueError):
    """Raised when a non-empty BibTeX document has malformed entry structure."""


@dataclass(frozen=True, slots=True)
class BibTeXEntry:
    entry_type: str
    citation_key: str
    text: str

    @property
    def title(self) -> str:
        return extract_bibtex_field(self.text, "title")


_ENTRY_HEADER       = re.compile(r"@\s*([A-Za-z]+)\s*([({])")
_FIELD_START        = re.compile(r"([A-Za-z][A-Za-z0-9_:-]*)\s*=\s*")
_NON_CITATION_TYPES = frozenset({"comment", "preamble", "string"})


def _find_entry_end(text: str, opening_index: int) -> int:
    opening     = text[opening_index]
    closing     = "}" if opening == "{" else ")"
    depth       = 1
    quoted      = False
    escaped     = False
    brace_depth = 0
    index       = opening_index + 1
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == '"':
            quoted = not quoted
            index += 1
            continue
        if not quoted:
            if opening == "(" and char == "{":
                brace_depth += 1
            elif opening == "(" and char == "}" and brace_depth:
                brace_depth -= 1
            elif brace_depth == 0:
                if char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        return index + 1
        index += 1
    raise BibTeXParseError("unterminated BibTeX entry")


def _citation_key(entry_text: str, opening_index: int, entry_type: str) -> str:
    if entry_type in _NON_CITATION_TYPES:
        return ""
    index = opening_index + 1
    while index < len(entry_text) and entry_text[index].isspace():
        index += 1
    comma = entry_text.find(",", index)
    if comma < 0:
        raise BibTeXParseError("BibTeX citation entry is missing its key separator")
    key = entry_text[index:comma].strip()
    if not key or any(char.isspace() for char in key):
        raise BibTeXParseError("invalid BibTeX citation key")
    return key


def parse_bibtex_entries(text: str) -> list[BibTeXEntry]:
    """Parse entry boundaries without splitting on ``@`` inside field values."""

    document                   = str(text or "")
    entries: list[BibTeXEntry] = []
    position                   = 0
    while True:
        marker = document.find("@", position)
        if marker < 0:
            break
        header = _ENTRY_HEADER.match(document, marker)
        if header is None:
            position = marker + 1
            continue
        entry_type    = header.group(1).lower()
        opening_index = header.end() - 1
        end           = _find_entry_end(document, opening_index)
        entry_text    = document[marker:end]
        local_opening = opening_index - marker
        entries.append(
            BibTeXEntry(
                entry_type   = entry_type,
                citation_key = _citation_key(entry_text, local_opening, entry_type),
                text         = entry_text,
            )
        )
        position = end
    if document.strip() and not entries:
        raise BibTeXParseError("document contains no valid BibTeX entries")
    return entries


def citation_entries(text: str) -> list[BibTeXEntry]:
    return [
        entry for entry in parse_bibtex_entries(text) if entry.entry_type not in _NON_CITATION_TYPES
    ]


def _value_end(text: str, start: int) -> int:
    if start >= len(text):
        return start
    opening = text[start]
    if opening in "{(":
        return _find_entry_end(text, start)
    if opening == '"':
        escaped = False
        for index in range(start + 1, len(text)):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return index + 1
        raise BibTeXParseError("unterminated quoted BibTeX field")
    comma = text.find(",", start)
    return len(text) if comma < 0 else comma


def extract_bibtex_field(entry_text: str, field_name: str) -> str:
    """Extract one field value while preserving nested braces during scanning."""

    target = str(field_name or "").strip().lower()
    if not target:
        return ""
    for match in _FIELD_START.finditer(entry_text):
        if match.group(1).lower() != target:
            continue
        start = match.end()
        end   = _value_end(entry_text, start)
        value = entry_text[start:end].strip()
        if len(value) >= 2 and (
            (value[0] == "{" and value[-1] == "}") or (value[0] == '"' and value[-1] == '"')
        ):
            value = value[1:-1]
        return " ".join(value.split())
    return ""

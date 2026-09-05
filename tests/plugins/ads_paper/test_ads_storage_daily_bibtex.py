from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.ads_paper.bibtex import BibTeXParseError, citation_entries, parse_bibtex_entries
from plugins.ads_paper.storage import PaperStorage, PaperStorageCorruptionError


@pytest.mark.parametrize(
    ("filename", "payload", "mutation"),
    [
        ("paper_notes.json", b'{"paper": [', lambda storage: storage.add_paper_note("p", "n", 1)),
        (
            "writing_ideas.json",
            b"\xff\xfeinvalid",
            lambda storage: storage.add_writing_idea("intro", "idea", 1),
        ),
        (
            "research_topics.json",
            b"[]",
            lambda storage: storage.add_topic("FRB", 1),
        ),
        (
            "deadlines.json",
            b'{"deadlines": NaN}',
            lambda storage: storage.add_deadline("submit", "2026-07-13", 1),
        ),
        (
            "paper_notes.json",
            b'{"paper": "not-a-list"}',
            lambda storage: storage.add_paper_note("paper", "note", 1),
        ),
        (
            "research_topics.json",
            b'{"keywords": [null]}',
            lambda storage: storage.add_topic("FRB", 1),
        ),
    ],
)
def test_corrupt_storage_is_quarantined_and_never_overwritten(
    tmp_path: Path,
    filename: str,
    payload: bytes,
    mutation,
) -> None:
    path = tmp_path / filename
    path.write_bytes(payload)
    storage = PaperStorage(tmp_path)

    with pytest.raises(PaperStorageCorruptionError):
        mutation(storage)

    assert path.read_bytes() == payload
    digest     = hashlib.sha256(payload).hexdigest()[:12]
    quarantine = tmp_path / f"{filename}.corrupt-{digest}"
    assert quarantine.read_bytes() == payload


@pytest.mark.asyncio
async def test_daily_uses_utc_entry_date_window_and_deterministic_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugins.ads_paper import ai_commands

    storage = PaperStorage(tmp_path)
    assert storage.add_topic("fast radio burst", 1)
    captured = {}

    class Client:
        async def search_papers(self, query, max_results, *, fields, sort):
            captured.update(
                query       = query,
                max_results = max_results,
                fields      = fields,
                sort        = sort,
            )
            return [
                {"bibcode": "B", "title": ["Today B"], "entdate": "2026-07-13"},
                {"bibcode": "OLD", "title": ["Old"], "entdate": "2026-07-12"},
                {
                    "bibcode": "A",
                    "title": ["Today A"],
                    "entdate": "2026-07-13",
                    "date": "2021-01-01T00:00:00.000Z",
                },
                {"bibcode": "UNKNOWN", "title": ["Missing date"]},
            ]

        @staticmethod
        def format_paper_info(paper):
            return paper["title"][0]

    monkeypatch.setattr(ai_commands, "_utc_today", lambda: date(2026, 7, 13))
    result = await ai_commands.cmd_daily(Client(), storage, 1)
    text   = result[0]["data"]["text"]

    assert captured["query"] == ('("fast radio burst") AND entdate:[2026-07-13 TO NOW]')
    assert captured["sort"] == "entdate desc,bibcode asc"
    assert "entdate" in captured["fields"]
    assert "Today A" in text and "Today B" in text
    assert text.index("Today A") < text.index("Today B")
    assert "Old" not in text and "Missing date" not in text


_COMPLEX_BIBTEX = r"""
% a leading comment with ignored@example.com
@string{journal = "A@B Journal"}

@article{KeyOne,
  title = {A {Nested} Title},
  author = {Doe, Jane and Roe, Richard},
  email = {jane@example.com},
  url = {https://example.com/a@b},
  note = {literal @ sign and {nested {braces}}}
}

@inproceedings(KeyTwo,
  title = "Quoted @ Title",
  comment = "text with @ and an escaped quote \" here",
  year = 2026
)
"""


def test_bibtex_parser_preserves_entry_boundaries_with_nested_and_at_values() -> None:
    all_entries = parse_bibtex_entries(_COMPLEX_BIBTEX)
    entries     = citation_entries(_COMPLEX_BIBTEX)

    assert [entry.entry_type for entry in all_entries] == ["string", "article", "inproceedings"]
    assert [entry.citation_key for entry in entries] == ["KeyOne", "KeyTwo"]
    assert entries[0].title == "A {Nested} Title"
    assert entries[1].title == "Quoted @ Title"
    assert "jane@example.com" in entries[0].text
    assert "https://example.com/a@b" in entries[0].text
    assert "literal @ sign" in entries[0].text

    reparsed = citation_entries("\n\n".join(entry.text for entry in all_entries))
    assert reparsed == entries


def test_bibtex_parser_preserves_percent_inside_braced_field() -> None:
    text = """@article{Percent,
  title = {A 90% Confidence Constraint on the FRB Rate},
  year = 2020,
}
"""

    entries = citation_entries(text)

    assert len(entries) == 1
    assert entries[0].citation_key == "Percent"
    assert entries[0].title == "A 90% Confidence Constraint on the FRB Rate"


@pytest.mark.asyncio
async def test_refs_command_uses_structural_bibtex_entries(tmp_path: Path) -> None:
    from plugins.ads_paper.ai_commands import cmd_refs

    path = tmp_path / "references_1.bib"
    path.write_text(_COMPLEX_BIBTEX, encoding="utf-8")

    context = SimpleNamespace(request_id=None, secrets={})
    result = await cmd_refs(PaperStorage(tmp_path), context, 1)
    text   = result[0]["data"]["text"]

    assert "文献库 (2 条引用)" in text
    assert "A {Nested} Title" in text
    assert "Quoted @ Title" in text


def test_invalid_existing_bibtex_blocks_append_without_overwrite(tmp_path: Path) -> None:
    path     = tmp_path / "references_1.bib"
    original = b"@article{Broken, title={unfinished}"
    path.write_bytes(original)
    storage = PaperStorage(tmp_path)

    with pytest.raises(BibTeXParseError):
        storage.add_reference(1, "Good", "@article{Good, title={Valid}}")

    assert path.read_bytes() == original

"""Fail-closed tests for the tracked code-review ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_code_review as review

ROOT = Path(__file__).resolve().parents[1]


def _ledger(*, completed: int = review.EXPECTED_TOTAL, identifiers=None) -> str:
    identifiers = list(range(1, review.EXPECTED_TOTAL + 1)) if identifiers is None else identifiers
    recent = f"CR-{completed:03d}" if completed else "无"
    next_entry = f"CR-{completed + 1:03d}" if completed < review.EXPECTED_TOTAL else "无"
    note = "全部完成" if completed == review.EXPECTED_TOTAL else f"仅 {next_entry} 待按顺序处理"
    lines = [
        "# Review ledger",
        "",
        "### 修复执行进度",
        "",
        f"- **已完成**：{completed} / {review.EXPECTED_TOTAL}（{note}）",
        f"- **最近完成**：{recent}；**下一条**：{next_entry}",
        "- **新增复审批次**：CR-182 至 CR-232，共 51 条；逐条记录实际改动和验证结果。",
        "",
        "### 审查基线",
        "",
        "正文引用 CR-001、CR-232 和 CR-999 不会创建条目。",
        "",
    ]
    for identifier in identifiers:
        lines.extend(
            [
                f"### CR-{identifier:03d} [P{identifier % 4}] Finding {identifier}",
                "",
                f"- **位置**：module/path-{identifier}.py。",
            ]
        )
        if identifier <= 181:
            lines.extend(
                [
                    "- **触发条件**：执行对应边界行为时触发。",
                    "- **根因**：缺少严格且可复现的契约。",
                ]
            )
        else:
            lines.append("- **证据与根因**：复审证据确认缺少严格契约。")
        lines.extend(
            [
                "- **影响**：可能造成可验证的行为回归。",
                "- **修复建议**：建立明确的实现与回归守卫。",
                "- **回归测试**：覆盖成功路径与失败关闭路径。",
            ]
        )
        if identifier <= completed:
            lines.extend(
                [
                    "- **修复状态**：✅ 已修复（2026-07-11）",
                    f"- **实际改动**：implemented reviewed change for entry {identifier:03d}。",
                    f"- **验证结果**：verified regression evidence for entry {identifier:03d}。",
                ]
            )
        else:
            lines.append("- **修复状态**：⏳ 待修复")
        lines.append("")
    return "\n".join(lines) + "\n"


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1
    return source.replace(old, new, 1)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path, payload: bytes | None = None, *, track: bool = True) -> Path:
    _git(tmp_path, "init", "-q")
    review_path = tmp_path / review.REVIEW_PATH
    review_path.write_bytes(payload if payload is not None else _ledger().encode("utf-8"))
    if track:
        _git(tmp_path, "add", "--", review.REVIEW_PATH)
    return tmp_path


def test_complete_ledger_passes_and_ignores_fenced_or_indented_examples() -> None:
    text = _ledger() + (
        "```markdown\n"
        "### CR-999 [P0] fenced example\n"
        "- **修复状态**：⏳ 待修复\n"
        "```\n"
        "    ### CR-998 [P0] indented code example\n"
    )
    result = review.validate_document(text, require_complete=True)
    assert result.errors == ()
    assert result.entries == review.EXPECTED_TOTAL
    assert result.completed == review.EXPECTED_TOTAL
    assert result.next_identifier is None


def test_crlf_ledger_passes() -> None:
    result = review.validate_document(
        _ledger().replace("\n", "\r\n"),
        require_complete=True,
    )
    assert result.errors == ()


def test_current_pending_prefix_passes_default_but_not_complete_mode() -> None:
    text = _ledger(completed=231)
    assert review.validate_document(text).errors == ()
    result = review.validate_document(text, require_complete=True)
    assert any("complete ledger required" in error for error in result.errors)
    assert result.next_identifier == 232


@pytest.mark.parametrize(
    "heading",
    [
        "## CR-999 [P0] wrong level",
        "### CR–999 [P0] Unicode dash",
        "### CR-１２３ [P0] Unicode digits",
        "### cr-999 [P0] lowercase prefix",
    ],
)
def test_noncanonical_cr_headings_outside_code_are_rejected(heading: str) -> None:
    result = review.validate_document(_ledger() + f"\n{heading}\n")
    assert any("malformed CR heading" in error for error in result.errors)


def test_unclosed_markdown_fence_is_rejected() -> None:
    result = review.validate_document(_ledger() + "```markdown\n### CR-999 [P0] hidden\n")
    assert any("unclosed Markdown fence" in error for error in result.errors)


def test_html_comments_are_rejected_even_when_they_hide_a_heading() -> None:
    text = _ledger() + "<!-- hidden\n### CR-999 [P0] fake\n-->\n"
    result = review.validate_document(text)
    assert any("HTML comments are forbidden" in error for error in result.errors)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "out_of_order"])
def test_cr_sequence_rejects_missing_duplicate_or_out_of_order_entries(mode: str) -> None:
    identifiers = list(range(1, review.EXPECTED_TOTAL + 1))
    if mode == "missing":
        identifiers.remove(100)
    elif mode == "duplicate":
        identifiers.insert(100, 100)
    else:
        identifiers[99], identifiers[100] = identifiers[100], identifiers[99]
    result = review.validate_document(_ledger(identifiers=identifiers))
    assert any("sequence must be exactly" in error for error in result.errors)


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("232 / 232（全部完成）", "231 / 232（全部完成）", "completed/total"),
        ("CR-232；**下一条**：无", "CR-231；**下一条**：无", "recent/next"),
        ("CR-232；**下一条**：无", "CR-232；**下一条**：CR-232", "recent/next"),
        ("CR-182 至 CR-232，共 51 条", "CR-183 至 CR-232，共 50 条", "review-batch"),
    ],
)
def test_summary_drift_is_rejected(old: str, new: str, needle: str) -> None:
    result = review.validate_document(_replace_once(_ledger(), old, new), require_complete=True)
    assert any(needle in error for error in result.errors)


def test_malformed_duplicate_summary_prefix_is_rejected() -> None:
    text = _replace_once(
        _ledger(),
        "- **最近完成**：CR-232；**下一条**：无\n",
        "- **最近完成**：CR-232；**下一条**：无\n- **最近完成** malformed\n",
    )
    result = review.validate_document(text, require_complete=True)
    assert any("recent/next" in error for error in result.errors)


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_indented_summary_candidate_is_rejected(indent: str) -> None:
    text = _replace_once(
        _ledger(),
        "- **已完成**：232 / 232（全部完成）",
        f"{indent}- **已完成**：232 / 232（全部完成）",
    )
    result = review.validate_document(text, require_complete=True)
    assert any("completed/total" in error for error in result.errors)


@pytest.mark.parametrize(
    "status",
    ["✅ 已修复但未完成", "✅ 已完成（2026-02-30）"],
)
def test_fixed_status_rejects_suffixes_and_invalid_dates(status: str) -> None:
    text = _ledger().replace("✅ 已修复（2026-07-11）", status, 1)
    result = review.validate_document(text)
    assert any("repair status" in error or "status date" in error for error in result.errors)


@pytest.mark.parametrize(
    "placeholder",
    ["无", "N/A details", "TODO later", "TBD later", "待补最终结果", "待验证后更新", "同上所述"],
)
def test_completed_fields_reject_placeholder_values(placeholder: str) -> None:
    text = _replace_once(
        _ledger(),
        "implemented reviewed change for entry 001。",
        placeholder,
    )
    result = review.validate_document(text)
    assert any("placeholder 实际改动" in error for error in result.errors)


@pytest.mark.parametrize("field", ["实际改动", "验证结果"])
def test_completed_entry_requires_one_actual_change_and_validation(field: str) -> None:
    line = next(line for line in _ledger().splitlines() if line.startswith(f"- **{field}**"))
    result = review.validate_document(_ledger().replace(f"{line}\n", "", 1))
    assert any(f"exactly one {field}" in error for error in result.errors)


def test_entry_cannot_claim_fields_from_an_unrelated_following_section() -> None:
    text = _ledger()
    text = text.replace(
        "- **实际改动**：implemented reviewed change for entry 232。\n",
        "",
        1,
    ).replace(
        "- **验证结果**：verified regression evidence for entry 232。\n",
        "",
        1,
    )
    text += (
        "## Unrelated appendix\n\n"
        "- **实际改动**：this belongs to the appendix, not CR-232。\n"
        "- **验证结果**：this appendix evidence must not close CR-232。\n"
    )
    result = review.validate_document(text, require_complete=True)
    assert any("CR-232 must contain exactly one 实际改动" in error for error in result.errors)
    assert any("CR-232 must contain exactly one 验证结果" in error for error in result.errors)


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_indented_ledger_field_candidate_is_rejected(indent: str) -> None:
    text = _ledger().replace(
        "- **修复状态**：✅ 已修复（2026-07-11）",
        f"{indent}- **修复状态**：✅ 已修复（2026-07-11）",
        1,
    )
    result = review.validate_document(text)
    assert any("malformed ledger field" in error for error in result.errors)


@pytest.mark.parametrize(
    ("identifier", "field"),
    [(1, "触发条件"), (182, "证据与根因"), (232, "位置")],
)
def test_each_generation_requires_its_structural_fields(identifier: int, field: str) -> None:
    lines = _ledger().splitlines()
    start = lines.index(next(line for line in lines if line.startswith(f"### CR-{identifier:03d} ")))
    field_index = next(
        index for index in range(start + 1, len(lines)) if lines[index].startswith(f"- **{field}**")
    )
    del lines[field_index]
    result = review.validate_document("\n".join(lines) + "\n")
    assert any(f"exactly one {field}" in error for error in result.errors)


@pytest.mark.parametrize(
    ("identifier", "field", "value"),
    [(1, "证据与根因", "不属于旧条目的字段。"), (182, "根因", "不属于新条目的字段。")],
)
def test_entries_reject_fields_from_the_other_generation(
    identifier: int,
    field: str,
    value: str,
) -> None:
    lines = _ledger().splitlines()
    start = lines.index(next(line for line in lines if line.startswith(f"### CR-{identifier:03d} ")))
    lines.insert(start + 2, f"- **{field}**：{value}")
    result = review.validate_document("\n".join(lines) + "\n")
    assert any(f"must not contain legacy field {field}" in error for error in result.errors)


def test_pending_entry_must_not_claim_change_or_validation() -> None:
    text = _ledger(completed=231) + ""
    text = _replace_once(
        text,
        "- **修复状态**：⏳ 待修复\n",
        "- **修复状态**：⏳ 待修复\n- **实际改动**：not actually complete。\n",
    )
    result = review.validate_document(text)
    assert any("pending entry must not contain 实际改动" in error for error in result.errors)


def test_staged_new_nonempty_ledger_is_accepted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert review.validate_repository(repository, require_complete=True).errors == ()


def test_untracked_ledger_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path, track=False)
    result = review.validate_repository(repository, require_complete=True)
    assert any("tracked at stage 0" in error for error in result.errors)


def test_tracked_but_ignored_ledger_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / ".gitignore").write_text("code_review.md\n", encoding="utf-8")
    result = review.validate_repository(repository, require_complete=True)
    assert any("still ignored" in error for error in result.errors)


def test_intent_to_add_empty_blob_is_not_treated_as_tracked_content(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / review.REVIEW_PATH).write_text(_ledger(), encoding="utf-8")
    _git(tmp_path, "add", "-N", "--", review.REVIEW_PATH)
    result = review.validate_repository(tmp_path, require_complete=True)
    assert any("non-empty blob" in error for error in result.errors)


def test_non_regular_index_mode_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repository,
        input=b"code_review.md",
        capture_output=True,
        check=True,
    ).stdout.decode("ascii").strip()
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"120000,{blob},{review.REVIEW_PATH}",
    )
    result = review.validate_repository(repository, require_complete=True)
    assert any("tracked at stage 0" in error for error in result.errors)


def test_unstaged_ledger_change_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with (repository / review.REVIEW_PATH).open("a", encoding="utf-8") as target:
        target.write("post-index change\n")
    result = review.validate_repository(repository, require_complete=True)
    assert any("worktree content must match" in error for error in result.errors)


@pytest.mark.parametrize("prefix", [b"\xef\xbb\xbf", b"\x00"])
def test_repository_rejects_bom_and_nul(tmp_path: Path, prefix: bytes) -> None:
    repository = _repository(tmp_path, prefix + _ledger().encode("utf-8"))
    result = review.validate_repository(repository, require_complete=True)
    assert any("cannot read" in error for error in result.errors)


def test_repository_enforces_size_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setattr(review, "MAX_REVIEW_BYTES", 10)
    result = review.validate_repository(repository, require_complete=True)
    assert any("review size" in error for error in result.errors)


def test_real_document_structure_matches_its_current_progress() -> None:
    text = (ROOT / review.REVIEW_PATH).read_text(encoding="utf-8")
    result = review.validate_document(text, require_complete=True)
    assert result.errors == ()
    assert result.entries == review.EXPECTED_TOTAL
    assert result.completed == 232
    assert result.next_identifier is None

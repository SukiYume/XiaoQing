"""Validate the tracked code-review ledger and its progress summary."""

from __future__ import annotations

import argparse
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EXPECTED_TOTAL = 232
REVIEW_PATH = "code_review.md"
MAX_REVIEW_BYTES = 4 * 1024 * 1024
ENTRY_HEADING = re.compile(
    r"^### CR-(?P<identifier>[0-9]{3}) \[(?P<priority>P[0-3])\] "
    r"(?P<title>\S(?:.*\S)?)$"
)
ENTRY_HEADING_CANDIDATE = re.compile(r"^ {0,3}#{1,6}\s+CR", flags=re.IGNORECASE)
SECTION_HEADING = re.compile(r"^#{1,6}\s+")
ENTRY_BOUNDARY = re.compile(r"^ {0,3}#{1,3}(?:\s+|$)")
FENCE_OPEN = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
FIELD_NAMES = (
    "位置",
    "触发条件",
    "根因",
    "证据与根因",
    "影响",
    "修复建议",
    "回归测试",
    "修复状态",
    "实际改动",
    "验证结果",
)
FIELD = re.compile(
    rf"^- \*\*(?P<name>{'|'.join(FIELD_NAMES)})\*\*[：:]\s*(?P<value>.*?)\s*$"
)
FIELD_CANDIDATE = re.compile(
    rf"^ {{0,3}}-\s*\*\*(?:{'|'.join(FIELD_NAMES)})\*\*"
)
FIXED_STATUS = re.compile(
    r"^✅ (?P<state>已修复|已完成)(?:（(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})）)?$"
)
SUMMARY_COMPLETED = re.compile(
    r"^- \*\*已完成\*\*[：:]\s*(?P<completed>[0-9]+)\s*/\s*(?P<total>[0-9]+)"
    r"(?:（(?P<note>.*)）)?$"
)
SUMMARY_POSITION = re.compile(
    r"^- \*\*最近完成\*\*[：:]\s*(?P<recent>CR-[0-9]{3}|无)；"
    r"\*\*下一条\*\*[：:]\s*(?P<next>CR-[0-9]{3}|无)$"
)
SUMMARY_BATCH = re.compile(
    r"^- \*\*新增复审批次\*\*[：:]CR-(?P<start>[0-9]{3}) 至 "
    r"CR-(?P<end>[0-9]{3})，共 (?P<count>[0-9]+) 条；.+$"
)
SUMMARY_CANDIDATES = {
    "completed": re.compile(r"^ {0,3}-\s*\*\*已完成\*\*"),
    "position": re.compile(r"^ {0,3}-\s*\*\*最近完成\*\*"),
    "batch": re.compile(r"^ {0,3}-\s*\*\*新增复审批次\*\*"),
}
PLACEHOLDER_VALUES = frozenset(
    {"-", "n/a", "na", "none", "todo", "tbd", "无", "同上", "待补", "待补充", "待验证"}
)
PLACEHOLDER_PREFIXES = ("todo", "tbd", "n/a", "待补", "待验证", "同上")


@dataclass(frozen=True)
class Entry:
    identifier: int
    priority: str
    title: str
    start: int
    end: int


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    entries: int
    completed: int
    recent: int | None
    next_identifier: int | None


def _structural_mask(lines: list[str], errors: list[str]) -> list[bool]:
    outside: list[bool] = []
    fence_character = ""
    fence_length = 0
    for line in lines:
        if fence_character:
            outside.append(False)
            if re.fullmatch(
                rf"\s*{re.escape(fence_character)}{{{fence_length},}}\s*",
                line,
            ):
                fence_character = ""
                fence_length = 0
            continue

        if "<!--" in line or "-->" in line:
            errors.append("HTML comments are forbidden outside Markdown fences")
            outside.append(False)
            continue

        opening = FENCE_OPEN.match(line)
        if opening is not None:
            fence = opening.group("fence")
            fence_character = fence[0]
            fence_length = len(fence)
            outside.append(False)
            continue
        outside.append(True)
    if fence_character:
        errors.append("document contains an unclosed Markdown fence")
    return outside


def _entries(lines: list[str], outside: list[bool], errors: list[str]) -> list[Entry]:
    candidates = [
        index
        for index, line in enumerate(lines)
        if outside[index] and ENTRY_HEADING_CANDIDATE.match(line)
    ]
    parsed: list[tuple[int, re.Match[str]]] = []
    for index in candidates:
        match = ENTRY_HEADING.fullmatch(lines[index])
        if match is None:
            errors.append(f"line {index + 1}: malformed CR heading")
            continue
        parsed.append((index, match))

    boundaries = [
        index
        for index, line in enumerate(lines)
        if outside[index] and ENTRY_BOUNDARY.match(line)
    ]
    entries = [
        Entry(
            identifier=int(match.group("identifier")),
            priority=match.group("priority"),
            title=match.group("title"),
            start=index,
            end=next(
                (boundary for boundary in boundaries if boundary > index),
                len(lines),
            ),
        )
        for index, match in parsed
    ]
    identifiers = [entry.identifier for entry in entries]
    duplicate_identifiers = sorted(
        identifier for identifier in set(identifiers) if identifiers.count(identifier) > 1
    )
    if duplicate_identifiers:
        errors.append(f"duplicate CR identifiers: {duplicate_identifiers}")
    expected = list(range(1, EXPECTED_TOTAL + 1))
    if identifiers != expected:
        missing = sorted(set(expected) - set(identifiers))
        extra = sorted(set(identifiers) - set(expected))
        errors.append(
            "CR heading sequence must be exactly CR-001 through CR-232 in order: "
            f"missing={missing or 'none'}, extra={extra or 'none'}"
        )
    return entries


def _entry_fields(
    entry: Entry,
    lines: list[str],
    outside: list[bool],
    errors: list[str],
) -> dict[str, list[str]]:
    fields = {name: [] for name in FIELD_NAMES}
    for index in range(entry.start + 1, entry.end):
        if not outside[index]:
            continue
        line = lines[index]
        match = FIELD.fullmatch(line)
        if match is not None:
            fields[match.group("name")].append(match.group("value"))
        elif FIELD_CANDIDATE.match(line):
            errors.append(
                f"CR-{entry.identifier:03d} line {index + 1}: malformed ledger field"
            )
    return fields


def _substantive(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        len(value.strip()) >= 8
        and normalized not in PLACEHOLDER_VALUES
        and not normalized.startswith(PLACEHOLDER_PREFIXES)
    )


def _summary(
    lines: list[str],
    outside: list[bool],
    errors: list[str],
) -> tuple[int | None, int | None, str | None, str | None, str | None]:
    headings = [
        index
        for index, line in enumerate(lines)
        if outside[index] and line == "### 修复执行进度"
    ]
    if len(headings) != 1:
        errors.append("document must contain exactly one '### 修复执行进度' section")
        return None, None, None, None, None
    start = headings[0] + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if outside[index] and SECTION_HEADING.match(lines[index])
        ),
        len(lines),
    )
    section_lines = [lines[index] for index in range(start, end) if outside[index]]
    completed_candidates = [
        line for line in section_lines if SUMMARY_CANDIDATES["completed"].match(line)
    ]
    position_candidates = [
        line for line in section_lines if SUMMARY_CANDIDATES["position"].match(line)
    ]
    batch_candidates = [
        line for line in section_lines if SUMMARY_CANDIDATES["batch"].match(line)
    ]
    completed_match = (
        SUMMARY_COMPLETED.fullmatch(completed_candidates[0])
        if len(completed_candidates) == 1
        else None
    )
    position_match = (
        SUMMARY_POSITION.fullmatch(position_candidates[0])
        if len(position_candidates) == 1
        else None
    )
    batch_match = (
        SUMMARY_BATCH.fullmatch(batch_candidates[0]) if len(batch_candidates) == 1 else None
    )
    if completed_match is None:
        errors.append("progress summary must contain exactly one valid completed/total line")
        completed = total = None
        note = None
    else:
        completed = int(completed_match.group("completed"))
        total = int(completed_match.group("total"))
        note = completed_match.group("note")
    if position_match is None:
        errors.append("progress summary must contain exactly one valid recent/next line")
        recent = next_value = None
    else:
        recent = position_match.group("recent")
        next_value = position_match.group("next")
    if batch_match is None:
        errors.append("progress summary must contain exactly one valid review-batch line")
    elif (
        int(batch_match.group("start")) != 182
        or int(batch_match.group("end")) != EXPECTED_TOTAL
        or int(batch_match.group("count")) != EXPECTED_TOTAL - 181
    ):
        errors.append("review-batch summary must be exactly CR-182 through CR-232 (51 entries)")
    return completed, total, recent, next_value, note


def validate_document(text: str, *, require_complete: bool = False) -> ValidationResult:
    lines = text.splitlines()
    errors: list[str] = []
    outside = _structural_mask(lines, errors)
    entries = _entries(lines, outside, errors)
    fixed_identifiers: list[int] = []
    pending_identifiers: list[int] = []

    for entry in entries:
        fields = _entry_fields(entry, lines, outside, errors)
        required_fields = ["位置", "影响", "修复建议", "回归测试", "修复状态"]
        generation_fields = (
            ["触发条件", "根因"] if entry.identifier <= 181 else ["证据与根因"]
        )
        required_fields.extend(generation_fields)
        for field_name in required_fields:
            values = fields[field_name]
            if len(values) != 1:
                errors.append(
                    f"CR-{entry.identifier:03d} must contain exactly one {field_name} field"
                )
            elif not values[0].strip():
                errors.append(f"CR-{entry.identifier:03d} has an empty {field_name} field")
        unexpected_generation_fields = (
            ["证据与根因"] if entry.identifier <= 181 else ["触发条件", "根因"]
        )
        for field_name in unexpected_generation_fields:
            if fields[field_name]:
                errors.append(
                    f"CR-{entry.identifier:03d} must not contain legacy field {field_name}"
                )

        statuses = fields["修复状态"]
        if len(statuses) != 1:
            continue
        status = statuses[0]
        fixed_match = FIXED_STATUS.fullmatch(status)
        fixed = fixed_match is not None
        pending = status == "⏳ 待修复"
        if not fixed and not pending:
            errors.append(f"CR-{entry.identifier:03d} has an unsupported repair status")
            continue
        if fixed_match is not None and fixed_match.group("date") is not None:
            try:
                date.fromisoformat(fixed_match.group("date"))
            except ValueError:
                errors.append(f"CR-{entry.identifier:03d} has an invalid repair-status date")
        if pending:
            pending_identifiers.append(entry.identifier)
            for field_name in ("实际改动", "验证结果"):
                if fields[field_name]:
                    errors.append(
                        f"CR-{entry.identifier:03d} pending entry must not contain {field_name}"
                    )
            continue

        fixed_identifiers.append(entry.identifier)
        for field_name in ("实际改动", "验证结果"):
            values = fields[field_name]
            if len(values) != 1:
                errors.append(
                    f"CR-{entry.identifier:03d} must contain exactly one {field_name} field"
                )
            elif not _substantive(values[0]):
                errors.append(f"CR-{entry.identifier:03d} has a placeholder {field_name} field")

    expected_fixed = list(range(1, len(fixed_identifiers) + 1))
    if fixed_identifiers != expected_fixed:
        errors.append("fixed CR entries must form one continuous prefix starting at CR-001")
    expected_pending = list(range(len(fixed_identifiers) + 1, EXPECTED_TOTAL + 1))
    if pending_identifiers != expected_pending:
        errors.append("pending CR entries must be the remaining continuous suffix")

    completed = len(fixed_identifiers)
    recent_identifier = fixed_identifiers[-1] if fixed_identifiers else None
    next_identifier = pending_identifiers[0] if pending_identifiers else None
    summary_completed, summary_total, summary_recent, summary_next, summary_note = _summary(
        lines, outside, errors
    )
    if summary_completed != completed or summary_total != EXPECTED_TOTAL:
        errors.append(
            "progress completed/total does not match entry statuses: "
            f"expected={completed}/{EXPECTED_TOTAL}, actual={summary_completed}/{summary_total}"
        )
    expected_recent = f"CR-{recent_identifier:03d}" if recent_identifier is not None else "无"
    expected_next = f"CR-{next_identifier:03d}" if next_identifier is not None else "无"
    if summary_recent != expected_recent or summary_next != expected_next:
        errors.append(
            "progress recent/next does not match entry statuses: "
            f"expected={expected_recent}/{expected_next}, actual={summary_recent}/{summary_next}"
        )
    if require_complete and completed != EXPECTED_TOTAL:
        errors.append(
            f"complete ledger required: expected={EXPECTED_TOTAL}, actual={completed}"
        )
    if require_complete and summary_note != "全部完成":
        errors.append("complete ledger summary note must be exactly '（全部完成）'")

    return ValidationResult(
        errors=tuple(errors),
        entries=len(entries),
        completed=completed,
        recent=recent_identifier,
        next_identifier=next_identifier,
    )


def _git(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot execute git: {exc}") from exc


def validate_repository(
    repository_root: Path,
    *,
    require_complete: bool = False,
) -> ValidationResult:
    repository_root = repository_root.resolve(strict=True)
    errors: list[str] = []
    review_path = repository_root / REVIEW_PATH
    try:
        metadata = review_path.lstat()
        if review_path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OSError("review path is not a regular non-linked file")
        resolved = review_path.resolve(strict=True)
        resolved.relative_to(repository_root)
        payload = resolved.read_bytes()
        if not payload or len(payload) > MAX_REVIEW_BYTES:
            raise OSError(
                f"review size must be between 1 and {MAX_REVIEW_BYTES} bytes"
            )
        if payload.startswith(b"\xef\xbb\xbf"):
            raise UnicodeError("UTF-8 BOM is forbidden")
        if b"\x00" in payload:
            raise UnicodeError("NUL byte is forbidden")
        text = payload.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError) as exc:
        return ValidationResult(
            errors=(f"cannot read {REVIEW_PATH}: {exc}",),
            entries=0,
            completed=0,
            recent=None,
            next_identifier=None,
        )

    try:
        tracked = _git(repository_root, "ls-files", "--stage", "-z", "--", REVIEW_PATH)
        if tracked.returncode != 0:
            errors.append("git ls-files failed while checking the review ledger")
        else:
            records = [record for record in tracked.stdout.decode("utf-8").split("\0") if record]
            valid_record = False
            index_oid: str | None = None
            if len(records) == 1 and "\t" in records[0]:
                metadata_text, path = records[0].split("\t", maxsplit=1)
                parts = metadata_text.split()
                valid_record = (
                    len(parts) == 3
                    and parts[0] == "100644"
                    and parts[2] == "0"
                    and path == REVIEW_PATH
                )
                if valid_record:
                    index_oid = parts[1]
            if not valid_record:
                errors.append(f"{REVIEW_PATH} must be tracked at stage 0 in the Git index")
            elif index_oid is not None:
                object_size = _git(repository_root, "cat-file", "-s", index_oid)
                try:
                    size = int(object_size.stdout.decode("ascii").strip())
                except (UnicodeError, ValueError):
                    size = 0
                if object_size.returncode != 0 or size <= 0:
                    errors.append(f"{REVIEW_PATH} index entry must reference a non-empty blob")

            worktree_diff = _git(repository_root, "diff", "--quiet", "--", REVIEW_PATH)
            if worktree_diff.returncode == 1:
                errors.append(f"{REVIEW_PATH} worktree content must match its staged index content")
            elif worktree_diff.returncode != 0:
                errors.append("git diff failed while checking the review ledger")

        ignored = _git(repository_root, "check-ignore", "--no-index", "-v", "--", REVIEW_PATH)
        if ignored.returncode == 0:
            detail = ignored.stdout.decode("utf-8", errors="replace").strip()
            errors.append(f"{REVIEW_PATH} is still ignored: {detail}")
        elif ignored.returncode != 1:
            errors.append("git check-ignore failed while checking the review ledger")
    except RuntimeError as exc:
        errors.append(str(exc))

    document = validate_document(text, require_complete=require_complete)
    return ValidationResult(
        errors=(*errors, *document.errors),
        entries=document.entries,
        completed=document.completed,
        recent=document.recent,
        next_identifier=document.next_identifier,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Require the final 232/232 state with no next entry.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    result = validate_repository(repository_root, require_complete=args.require_complete)
    if result.errors:
        print("code review verification failed:")
        for error in result.errors:
            print(f"- {error}")
        return 1
    recent = f"CR-{result.recent:03d}" if result.recent is not None else "none"
    next_entry = (
        f"CR-{result.next_identifier:03d}" if result.next_identifier is not None else "none"
    )
    print(
        "code review verified: "
        f"entries={result.entries}, completed={result.completed}, "
        f"recent={recent}, next={next_entry}, tracked=yes, ignored=no"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

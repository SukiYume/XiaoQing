from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.scan_workspace_secrets import main, scan, scan_report

ROOT = Path(__file__).resolve().parents[1]


def _credential(prefix: str = "live") -> str:
    return f"{prefix}_" + "4f9b7c2d8a6e1f03"


def _write_allowlist(path: Path, finding, *, reason: str = "documented compatibility fixture") -> None:
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "path": finding.path,
                        "rule_id": finding.rule_id,
                        "fingerprint": finding.fingerprint,
                        "reason": reason,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_secret_scan_includes_ignored_and_deprecated_named_paths(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\nplugins/*_deprecated/\n", encoding="utf-8")
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "credentials.json").write_text(
        json.dumps({"access_token": _credential()}),
        encoding="utf-8",
    )
    deprecated = tmp_path / "plugins" / "old_deprecated"
    deprecated.mkdir(parents=True)
    (deprecated / "legacy.py").write_text(
        "password = " + repr(_credential("prod")),
        encoding="utf-8",
    )

    findings = scan(tmp_path)

    assert len(findings) == 2
    assert any("credentials.json" in finding for finding in findings)
    assert any("legacy.py" in finding for finding in findings)


def test_secret_scan_streams_past_two_megabytes_to_tail_finding(tmp_path: Path) -> None:
    value = _credential("tail")
    large = tmp_path / "large.log"
    with large.open("wb") as handle:
        for _ in range(2_049):
            handle.write(b"x" * 1024 + b"\n")
        handle.write(f'access_token = "{value}"\n'.encode())

    report = scan_report(tmp_path)

    assert len(report.findings) == 1
    assert report.findings[0].path == "large.log"
    assert report.findings[0].line == 2_050


def test_secret_rules_cover_token_client_secret_and_private_key(tmp_path: Path) -> None:
    token = _credential("token")
    client = _credential("client")
    pem = "-----BEGIN " + "PRIVATE KEY-----"
    (tmp_path / "secrets.env").write_text(
        f'TOKEN="{token}"\nCLIENT_SECRET="{client}"\n{pem}\n',
        encoding="utf-8",
    )

    report = scan_report(tmp_path)

    assert {finding.rule_id for finding in report.findings} == {
        "credential.assignment.token.v1",
        "credential.assignment.client-secret.v1",
        "private-key.pem.v1",
    }


@pytest.mark.parametrize(
    "placeholder",
    [
        "your-api-key-placeholder",
        "<SERVICE_API_KEY>",
        "${SERVICE_API_KEY}",
        "sk-xxx",
        "Bearer <SERVICE_TOKEN>",
        "PASTE_WIDGET_TOKEN_HERE",
    ],
)
def test_secret_scan_ignores_only_complete_documented_placeholders(
    tmp_path: Path,
    placeholder: str,
) -> None:
    (tmp_path / "example.json").write_text(
        json.dumps({"api_key": placeholder}),
        encoding="utf-8",
    )
    assert scan(tmp_path) == []


@pytest.mark.parametrize("embedded_marker", ["test", "example", "xxx"])
def test_placeholder_words_embedded_in_real_key_do_not_bypass_scan(
    tmp_path: Path,
    embedded_marker: str,
) -> None:
    value = _credential(f"live_{embedded_marker}")
    (tmp_path / "secret.json").write_text(
        json.dumps({"api_key": value}),
        encoding="utf-8",
    )

    report = scan_report(tmp_path)

    assert len(report.findings) == 1


def test_allowlist_requires_exact_path_rule_fingerprint_and_reason(tmp_path: Path) -> None:
    secret_path = tmp_path / "legacy.env"
    secret_path.write_text(f'TOKEN="{_credential()}"\n', encoding="utf-8")
    finding = scan_report(tmp_path).findings[0]
    allowlist = tmp_path / "allowlist.json"
    _write_allowlist(allowlist, finding)

    accepted = scan_report(tmp_path, allowlist_path=allowlist)

    assert accepted.ok is True
    assert accepted.findings == ()
    assert accepted.allowed == (finding,)

    payload = json.loads(allowlist.read_text(encoding="utf-8"))
    payload["entries"][0]["rule_id"] = "credential.assignment.password.v1"
    allowlist.write_text(json.dumps(payload), encoding="utf-8")
    rejected = scan_report(tmp_path, allowlist_path=allowlist)
    assert rejected.ok is False
    assert rejected.findings == (finding,)
    assert len(rejected.stale_allowlist) == 1

    payload["entries"][0]["reason"] = ""
    allowlist.write_text(json.dumps(payload), encoding="utf-8")
    malformed = scan_report(tmp_path, allowlist_path=allowlist)
    assert malformed.ok is False
    assert malformed.allowlist_errors


def test_removed_secret_makes_allowlist_entry_stale_and_fails(tmp_path: Path) -> None:
    secret_path = tmp_path / "legacy.env"
    secret_path.write_text(f'TOKEN="{_credential()}"\n', encoding="utf-8")
    finding = scan_report(tmp_path).findings[0]
    allowlist = tmp_path / "allowlist.json"
    _write_allowlist(allowlist, finding)
    secret_path.write_text("TOKEN=<SERVICE_TOKEN>\n", encoding="utf-8")

    report = scan_report(tmp_path, allowlist_path=allowlist)

    assert report.ok is False
    assert report.findings == ()
    assert report.stale_allowlist[0].key == (
        finding.path,
        finding.rule_id,
        finding.fingerprint,
    )


def test_cli_output_has_relative_line_rule_and_hash_but_not_secret(
    tmp_path: Path,
    capsys,
) -> None:
    value = _credential("do_not_print")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "secret.env").write_text(
        f'CLIENT_SECRET="{value}"\n',
        encoding="utf-8",
    )

    exit_code = main(["--workspace", str(tmp_path)])
    stderr = capsys.readouterr().err

    assert exit_code == 1
    assert "nested/secret.env:1" in stderr
    assert "credential.assignment.client-secret.v1" in stderr
    assert "fingerprint=sha256:" in stderr
    assert value not in stderr
    assert str(tmp_path) not in stderr


def test_tracked_and_workspace_modes_are_distinct(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
    (tmp_path / "tracked.env").write_text("TOKEN=<TRACKED_TOKEN>\n", encoding="utf-8")
    (tmp_path / "ignored.env").write_text(
        f'TOKEN="{_credential("ignored")}"\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "tracked.env"],
        check=True,
    )

    assert scan_report(tmp_path, mode="tracked").ok is True
    workspace = scan_report(tmp_path, mode="workspace")
    assert len(workspace.findings) == 1
    assert workspace.findings[0].path == "ignored.env"


def test_current_tracked_repository_is_secret_scan_clean() -> None:
    report = scan_report(ROOT, mode="tracked")

    assert report.ok, report.rendered_problems()

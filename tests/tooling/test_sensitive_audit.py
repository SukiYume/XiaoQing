from __future__ import annotations

import re

import pytest

from core.sensitive_audit import SensitiveAuditSummary, audit_error_type, summarize_sensitive


def test_sensitive_summary_is_correlatable_without_containing_the_payload() -> None:
    canary = "Authorization: Bearer CR220-COMMAND-SECRET"

    first = summarize_sensitive(canary)
    repeated = summarize_sensitive(canary)
    different = summarize_sensitive(canary + "!")

    assert first == repeated
    assert first != different
    assert first.kind == "text"
    assert first.length == len(canary)
    assert first.byte_length == len(canary.encode("utf-8"))
    assert re.fullmatch(r"hmac-sha256:[0-9a-f]{24}", first.fingerprint)
    assert canary not in repr(first)
    assert "CR220-COMMAND-SECRET" not in repr(first)


def test_unicode_and_binary_lengths_have_explicit_units() -> None:
    text = summarize_sensitive("小青")
    binary = summarize_sensitive("小青".encode())

    assert (text.kind, text.length, text.byte_length) == ("text", 2, 6)
    assert (binary.kind, binary.length, binary.byte_length) == ("bytes", 6, 6)
    assert text.fingerprint != binary.fingerprint


def test_mutable_binary_input_is_copied_before_fingerprinting() -> None:
    payload = bytearray(b"secret")
    before = summarize_sensitive(payload)
    payload[:] = b"public"

    assert summarize_sensitive(b"secret") == before
    assert summarize_sensitive(payload) != before


def test_summary_rejects_objects_that_could_execute_unsafe_string_conversion() -> None:
    class ExplodingString:
        def __str__(self) -> str:
            raise AssertionError("must not stringify arbitrary objects")

    with pytest.raises(TypeError, match="text or bytes"):
        summarize_sensitive(ExplodingString())  # type: ignore[arg-type]


def test_summary_dataclass_itself_contains_only_safe_metadata() -> None:
    summary = SensitiveAuditSummary(
        kind="text",
        length=12,
        byte_length=12,
        fingerprint="hmac-sha256:" + ("a" * 24),
    )

    assert summary.kind == "text"
    assert "secret" not in repr(summary).lower()


def test_audit_error_type_is_bounded_and_safe_for_logs() -> None:
    invalid_name_error = type("Bad\nName", (Exception,), {})()

    assert audit_error_type(None) == "-"
    assert audit_error_type(ValueError("bad")) == "ValueError"
    assert audit_error_type(invalid_name_error) == "Exception"


def test_plugin_audit_modules_reexport_the_shared_error_normalizer() -> None:
    from plugins.minecraft.audit import audit_error_type as minecraft_error_type
    from plugins.qingssh.audit import audit_error_type as qingssh_error_type

    assert minecraft_error_type is audit_error_type
    assert qingssh_error_type is audit_error_type

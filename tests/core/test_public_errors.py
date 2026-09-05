from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from core.public_errors import (
    PUBLIC_ERROR_CODE,
    _sanitize_text,
    public_error_message,
    public_error_response,
)

_MAX_RAW_CHARS_FOR_TEST = 32_768


class CaptureLogger:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.fail                                                = fail

    def error(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.fail:
            raise OSError("broken log sink")


def _logged_payload(logger: CaptureLogger) -> tuple[str, dict[str, Any]]:
    assert len(logger.calls) == 1
    args, kwargs = logger.calls[0]
    assert args[0] == "public_error %s"
    assert len(args) == 2
    assert kwargs == {"extra": {"request_id": kwargs["extra"]["request_id"]}}
    serialized = args[1]
    assert isinstance(serialized, str)
    return serialized, json.loads(serialized)


def _raised_error(message: str) -> RuntimeError:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def test_public_message_redacts_credentials_urls_paths_secrets_and_controls() -> None:
    context_secret = "context-secret-canary-9472"
    bearer         = "bearer-canary-1849"
    basic          = "YmFzaWMtY2FuYXJ5LTQy"
    url            = "https://alice:pw@example.test/private?q=url-canary"
    windows_path   = r"C:\Users\alice\private\token.txt"
    posix_path     = "/home/alice/private/token.txt"
    forged_line    = "FORGED LOG LINE"
    error          = _raised_error(
        f"Authorization: Bearer {bearer}; Basic {basic}; {url}; "
        f"{windows_path}; {posix_path}; {context_secret}\n{forged_line}\x00"
    )
    context = SimpleNamespace(
        request_id = "request-123",
        secrets    = {"plugins": {"demo": {"to" + "ken": context_secret}}},
    )
    logger = CaptureLogger()

    message = public_error_message(
        context,
        error,
        logger    = logger,
        component = "demo.handle",
    )

    assert message == (
        f"操作失败，请稍后重试（错误码：{PUBLIC_ERROR_CODE}；request_id：request-123）"
    )
    for canary in (bearer, basic, url, windows_path, posix_path, context_secret, forged_line):
        assert canary not in message

    serialized, payload = _logged_payload(logger)
    for canary in (bearer, basic, url, windows_path, posix_path, context_secret):
        assert canary not in serialized
    assert "<redacted-credential>" in serialized
    assert "<redacted-url>" in serialized
    assert "<redacted-path>" in serialized
    assert "<redacted-secret>" in serialized
    assert "\n" not in serialized
    assert "\r" not in serialized
    assert "\x00" not in serialized
    assert payload["request_id"] == "request-123"
    assert payload["component"] == "demo.handle"
    assert payload["error_code"] == PUBLIC_ERROR_CODE
    assert payload["exception_type"] == "builtins.RuntimeError"
    assert payload["traceback"]
    assert payload["secret_scan_complete"] is True


def test_public_error_response_is_onebot_text_and_logs_exactly_once() -> None:
    logger = CaptureLogger()
    context = SimpleNamespace(request_id="req-200", secrets={})

    response = public_error_response(
        context,
        ValueError("private details"),
        logger    = logger,
        component = "demo.response",
    )

    assert response == [
        {
            "type": "text",
            "data": {
                "text": (
                    f"操作失败，请稍后重试（错误码：{PUBLIC_ERROR_CODE}；request_id：req-200）"
                )
            },
        }
    ]
    assert "private details" not in response[0]["data"]["text"]
    _logged_payload(logger)


def test_malicious_request_id_and_component_are_replaced_not_cleaned_in_place() -> None:
    malicious_request_id = "safe-prefix\nAuthorization: Bearer request-token"
    malicious_component  = "component\r\n/home/operator/secret"
    logger               = CaptureLogger()
    context = SimpleNamespace(request_id=malicious_request_id, secrets={})

    message = public_error_message(
        context,
        RuntimeError("failure"),
        logger    = logger,
        component = malicious_component,
    )

    request_id_match = re.search(r"request_id：([0-9a-f]{12})）", message)
    assert request_id_match is not None
    safe_request_id = request_id_match.group(1)
    serialized, payload = _logged_payload(logger)
    assert malicious_request_id not in message
    assert malicious_request_id not in serialized
    assert malicious_component not in serialized
    assert payload["request_id"] == safe_request_id
    assert payload["component"] == "unknown"
    assert logger.calls[0][1]["extra"]["request_id"] == safe_request_id


def test_request_id_that_contains_a_context_secret_is_replaced() -> None:
    secret = "valid-looking-secret"
    logger = CaptureLogger()
    context = SimpleNamespace(request_id=f"req-{secret}", secrets={"token": secret})

    message = public_error_message(
        context,
        RuntimeError("failure"),
        logger    = logger,
        component = "demo.handle",
    )

    assert secret not in message
    serialized, payload = _logged_payload(logger)
    assert secret not in serialized
    assert re.fullmatch(r"[0-9a-f]{12}", payload["request_id"])


def test_secret_tree_depth_limit_fails_closed_for_exception_messages() -> None:
    deep_secret            = "deep-secret-canary-8831"
    nested: dict[str, Any] = {"secret": deep_secret}
    for index in range(12):
        nested = {f"level_{index}": nested}
    logger = CaptureLogger()
    context = SimpleNamespace(request_id="req-depth", secrets=nested)

    public_error_message(
        context,
        RuntimeError(deep_secret),
        logger    = logger,
        component = "demo.deep",
    )

    serialized, payload = _logged_payload(logger)
    assert deep_secret not in serialized
    assert payload["secret_scan_complete"] is False
    assert payload["exception_message"] == "<omitted: secret scan limit>"


def test_secret_tree_cycle_is_bounded_but_complete() -> None:
    secrets: dict[str, Any] = {"to" + "ken": "cycle-secret-canary"}
    secrets["self"]         = secrets
    logger                  = CaptureLogger()

    public_error_message(
        SimpleNamespace(request_id="req-cycle", secrets=secrets),
        RuntimeError("cycle-secret-canary"),
        logger    = logger,
        component = "demo.cycle",
    )

    serialized, payload = _logged_payload(logger)
    assert "cycle-secret-canary" not in serialized
    assert payload["secret_scan_complete"] is True


def test_long_exception_and_chain_are_bounded() -> None:
    logger = CaptureLogger()
    context = SimpleNamespace(request_id="req-long", secrets={})
    previous: BaseException | None = None
    for index in range(20):
        current           = RuntimeError(f"layer-{index}-" + ("x" * 40_000))
        current.__cause__ = previous
        previous          = current
    assert previous is not None

    public_error_message(
        context,
        previous,
        logger    = logger,
        component = "demo.long",
    )

    serialized, payload = _logged_payload(logger)
    assert len(serialized) < 60_000
    assert len(payload["exception_chain"]) == 8
    assert payload["chain_truncated"] is True
    assert "<omitted: oversized exception>" in serialized


def test_secret_crossing_exception_truncation_boundary_is_fully_omitted() -> None:
    secret = "BOUNDARY_SECRET_CANARY"
    logger = CaptureLogger()
    prefix = "x" * (_MAX_RAW_CHARS_FOR_TEST - 5)
    context = SimpleNamespace(request_id="req-boundary", secrets={"token": secret})

    public_error_message(
        context,
        RuntimeError(prefix + secret + "tail"),
        logger    = logger,
        component = "demo.boundary",
    )

    serialized, payload = _logged_payload(logger)
    assert secret not in serialized
    assert "BOUND" not in serialized
    assert payload["exception_message"] == "<omitted: oversized exception>"


def test_sanitizer_never_keeps_a_prefix_that_crosses_its_input_bound() -> None:
    secret = "BOUNDARYSECRET12345"
    raw    = "x" * (_MAX_RAW_CHARS_FOR_TEST - 5) + secret

    sanitized = _sanitize_text(
        raw,
        secrets = (secret,),
        limit   = len(raw) + 100,
    )

    assert sanitized == "<omitted: oversized diagnostic>"
    assert "BOUND" not in sanitized


def test_short_punctuation_secret_is_redacted_everywhere() -> None:
    logger = CaptureLogger()

    public_error_message(
        SimpleNamespace(request_id="req-short-secret", secrets={"token": "a-"}),
        RuntimeError("oops a- leaked and a- repeated"),
        logger    = logger,
        component = "demo.short_secret",
    )

    serialized, payload = _logged_payload(logger)
    assert "a-" not in serialized
    assert "<redacted-secret>" in serialized
    assert payload["secret_scan_complete"] is True


def test_unprintable_exception_still_returns_and_logs_type_and_traceback() -> None:
    class UnprintableError(RuntimeError):
        def __str__(self) -> str:
            raise ValueError("string conversion failed")

    logger = CaptureLogger()
    error  = UnprintableError()

    response = public_error_response(
        SimpleNamespace(request_id="req-unprintable", secrets={}),
        error,
        logger    = logger,
        component = "demo.unprintable",
    )

    assert response[0]["data"]["text"].endswith("request_id：req-unprintable）")
    serialized, payload = _logged_payload(logger)
    assert "string conversion failed" not in serialized
    assert payload["exception_type"].endswith(".UnprintableError")
    assert payload["exception_message"] == "<unprintable exception>"
    assert "traceback" in payload


def test_logging_failure_cannot_replace_the_safe_response() -> None:
    logger = CaptureLogger(fail=True)

    message = public_error_message(
        SimpleNamespace(request_id="req-log-failure", secrets={}),
        RuntimeError("private"),
        logger    = logger,
        component = "demo.logger",
    )

    assert message.endswith("request_id：req-log-failure）")
    assert "private" not in message
    assert len(logger.calls) == 1


def test_missing_request_id_generates_a_real_correlation_id() -> None:
    logger = CaptureLogger()

    message = public_error_message(
        SimpleNamespace(request_id=None, secrets={}),
        RuntimeError("private"),
        logger    = logger,
        component = "demo.missing_request",
    )

    assert "request_id：None" not in message
    assert re.search(r"request_id：[0-9a-f]{12}）", message)
    _, payload = _logged_payload(logger)
    assert re.fullmatch(r"[0-9a-f]{12}", payload["request_id"])

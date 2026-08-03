"""读取并严格规范化 Codex 插件配置。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.interfaces import PluginSettingsSnapshot

DEFAULT_ARXIV_SUMMARY_LABEL = "astro-ph"
DEFAULT_ARXIV_SUMMARY_METHODOLOGY = "arxiv-summary-methodology.md"
LABEL_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
MAX_CONFIG_STRING_CHARS = 32_768
MAX_ALLOWED_ROOTS = 64
SANDBOX_VALUES = frozenset({"read-only", "workspace-write", "danger-full-access"})
APPROVAL_POLICY_VALUES = frozenset({"untrusted", "on-failure", "on-request", "never"})


@dataclass(frozen=True)
class CodexPluginConfig:
    codex_bin: str
    default_cwd: str
    allowed_cwd_roots: tuple[str, ...]
    max_parallel_jobs: int
    per_session_queue_limit: int
    max_prompt_chars: int
    spawn_timeout_seconds: int
    job_timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_json_line_bytes: int
    max_final_output_bytes: int
    max_qq_text_chars: int
    artifact_scan_max_entries: int
    artifact_scan_max_depth: int
    max_image_artifacts: int
    max_image_bytes: int
    max_image_total_bytes: int
    max_image_pixels: int
    max_image_frames: int
    max_qq_images: int
    sandbox: str
    approval_policy: str
    skip_git_repo_check: bool
    protected_sessions: tuple[str, ...]
    arxiv_summary_label: str
    arxiv_summary_cwd: str
    arxiv_summary_methodology: str
    session_ttl_days: int
    artifact_retention_days: int
    emergency_disk_bytes: int
    emergency_queue_limit: int


def _merged_plugin_config(settings: PluginSettingsSnapshot) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    merged.update(settings.plugin_config("codex"))
    merged.update(settings.plugin_secrets("codex"))
    return merged


def _clean_string(value: Any, default: str, *, max_chars: int = MAX_CONFIG_STRING_CHARS) -> str:
    """只接受 JSON 字符串，拒绝控制字符和隐式 ``str()`` 转换。"""

    if not isinstance(value, str):
        return default
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_chars or any(ord(char) < 32 for char in cleaned):
        return default
    return cleaned


def _first_clean_string(
    *values: Any,
    default: str,
    max_chars: int = MAX_CONFIG_STRING_CHARS,
) -> str:
    for value in values:
        cleaned = _clean_string(value, "", max_chars=max_chars)
        if cleaned:
            return cleaned
    return default


def _string_tuple(value: Any, *, max_items: int = MAX_ALLOWED_ROOTS) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = value
    else:
        return ()

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items[:max_items]:
        text = _clean_string(item, "")
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return tuple(cleaned)


def _bounded_int(
    raw: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = raw.get(key, default)
    if type(value) is not int:
        value = default
    return min(maximum, max(minimum, value))


def _choice(raw: Mapping[str, Any], key: str, default: str, choices: frozenset[str]) -> str:
    value = _clean_string(raw.get(key), default, max_chars=64)
    return value if value in choices else default


def _boolean(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    return value if type(value) is bool else default


def _default_workspace_dir(data_dir: Any) -> str:
    if not isinstance(data_dir, (str, Path)):
        raise ValueError("Codex context.data_dir is required when default_cwd is not configured")
    try:
        return str((Path(data_dir) / "workspaces").resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise ValueError("Codex context.data_dir cannot be resolved") from exc


def load_plugin_config_snapshot(
    settings: PluginSettingsSnapshot,
    *,
    data_dir: Any,
) -> CodexPluginConfig:
    """Validate one already-acquired atomic settings generation."""

    raw = _merged_plugin_config(settings)
    default_cwd = _clean_string(raw.get("default_cwd"), "") or _default_workspace_dir(data_dir)
    arxiv_raw = raw.get("arxiv_summary", {})
    if not isinstance(arxiv_raw, Mapping):
        arxiv_raw = {}
    arxiv_summary_label = _first_clean_string(
        arxiv_raw.get("label"),
        raw.get("arxiv_summary_label"),
        default=DEFAULT_ARXIV_SUMMARY_LABEL,
        max_chars=32,
    )
    if re.fullmatch(LABEL_PATTERN, arxiv_summary_label) is None:
        arxiv_summary_label = DEFAULT_ARXIV_SUMMARY_LABEL
    arxiv_summary_cwd = _first_clean_string(
        arxiv_raw.get("cwd"),
        raw.get("arxiv_summary_cwd"),
        default=default_cwd,
    )
    arxiv_summary_methodology = _first_clean_string(
        arxiv_raw.get("methodology"),
        raw.get("arxiv_summary_methodology"),
        default=DEFAULT_ARXIV_SUMMARY_METHODOLOGY,
        max_chars=1_024,
    )
    allowed_roots = _string_tuple(raw.get("allowed_cwd_roots")) or (default_cwd,)
    protected_sessions = {
        label
        for label in _string_tuple(raw.get("protected_sessions"))
        if re.fullmatch(LABEL_PATTERN, label) is not None
    }
    protected_sessions.add(arxiv_summary_label)
    per_session_queue_limit = _bounded_int(raw, "per_session_queue_limit", 10, 1, 1_000)
    emergency_queue_limit = max(
        per_session_queue_limit,
        _bounded_int(raw, "emergency_queue_limit", 1_000, 10, 10_000),
    )
    max_image_bytes = _bounded_int(
        raw,
        "max_image_bytes",
        20 * 1024**2,
        64 * 1024,
        100 * 1024**2,
    )
    max_image_total_bytes = max(
        max_image_bytes,
        _bounded_int(
            raw,
            "max_image_total_bytes",
            100 * 1024**2,
            64 * 1024,
            512 * 1024**2,
        ),
    )

    return CodexPluginConfig(
        codex_bin=_clean_string(raw.get("codex_bin"), "codex"),
        default_cwd=default_cwd,
        allowed_cwd_roots=allowed_roots,
        max_parallel_jobs=_bounded_int(raw, "max_parallel_jobs", 2, 1, 64),
        per_session_queue_limit=per_session_queue_limit,
        max_prompt_chars=_bounded_int(raw, "max_prompt_chars", 200_000, 1_000, 1_000_000),
        spawn_timeout_seconds=_bounded_int(raw, "spawn_timeout_seconds", 30, 1, 120),
        job_timeout_seconds=_bounded_int(raw, "job_timeout_seconds", 3_600, 30, 604_800),
        max_stdout_bytes=_bounded_int(
            raw, "max_stdout_bytes", 16 * 1024**2, 64 * 1024, 128 * 1024**2
        ),
        max_stderr_bytes=_bounded_int(
            raw, "max_stderr_bytes", 4 * 1024**2, 64 * 1024, 64 * 1024**2
        ),
        max_json_line_bytes=_bounded_int(
            raw, "max_json_line_bytes", 1024**2, 16 * 1024, 8 * 1024**2
        ),
        max_final_output_bytes=_bounded_int(
            raw, "max_final_output_bytes", 8 * 1024**2, 64 * 1024, 64 * 1024**2
        ),
        max_qq_text_chars=_bounded_int(raw, "max_qq_text_chars", 60_000, 2_000, 200_000),
        artifact_scan_max_entries=_bounded_int(raw, "artifact_scan_max_entries", 5_000, 10, 20_000),
        artifact_scan_max_depth=_bounded_int(raw, "artifact_scan_max_depth", 8, 1, 16),
        max_image_artifacts=_bounded_int(raw, "max_image_artifacts", 20, 1, 100),
        max_image_bytes=max_image_bytes,
        max_image_total_bytes=max_image_total_bytes,
        max_image_pixels=_bounded_int(raw, "max_image_pixels", 40_000_000, 1_024, 100_000_000),
        max_image_frames=_bounded_int(raw, "max_image_frames", 120, 1, 500),
        max_qq_images=_bounded_int(raw, "max_qq_images", 10, 1, 20),
        sandbox=_choice(raw, "sandbox", "workspace-write", SANDBOX_VALUES),
        approval_policy=_choice(raw, "approval_policy", "never", APPROVAL_POLICY_VALUES),
        skip_git_repo_check=_boolean(raw, "skip_git_repo_check", True),
        protected_sessions=tuple(sorted(protected_sessions)),
        arxiv_summary_label=arxiv_summary_label,
        arxiv_summary_cwd=arxiv_summary_cwd,
        arxiv_summary_methodology=arxiv_summary_methodology,
        session_ttl_days=_bounded_int(raw, "session_ttl_days", 90, 0, 3_650),
        artifact_retention_days=_bounded_int(raw, "artifact_retention_days", 30, 0, 3_650),
        emergency_disk_bytes=_bounded_int(
            raw,
            "emergency_disk_bytes",
            10 * 1024**3,
            64 * 1024**2,
            1024**4,
        ),
        emergency_queue_limit=emergency_queue_limit,
    )


def load_plugin_config(context: Any) -> CodexPluginConfig:
    """Read and validate the current atomic settings generation."""

    settings = context.get_settings_snapshot()
    return load_plugin_config_snapshot(settings, data_dir=context.data_dir)

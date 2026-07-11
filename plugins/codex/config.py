from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_CWD = "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"
DEFAULT_ARXIV_SUMMARY_LABEL = "astro-ph"
DEFAULT_ARXIV_SUMMARY_METHODOLOGY = "arxiv-summary-methodology.md"
LABEL_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


@dataclass(frozen=True)
class CodexPluginConfig:
    codex_bin: str
    default_cwd: str
    allowed_cwd_roots: tuple[str, ...]
    max_parallel_jobs: int
    per_session_queue_limit: int
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


def _merged_plugin_config(context: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    app_config = getattr(context, "config", {}) or {}
    app_secrets = getattr(context, "secrets", {}) or {}

    config_plugins = app_config.get("plugins", {}) if isinstance(app_config, dict) else {}
    secrets_plugins = app_secrets.get("plugins", {}) if isinstance(app_secrets, dict) else {}

    if isinstance(config_plugins, dict) and isinstance(config_plugins.get("codex"), dict):
        merged.update(config_plugins["codex"])
    if isinstance(secrets_plugins, dict) and isinstance(secrets_plugins.get("codex"), dict):
        merged.update(secrets_plugins["codex"])
    return merged


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _bounded_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def load_plugin_config(context: Any) -> CodexPluginConfig:
    raw = _merged_plugin_config(context)
    default_cwd = str(raw.get("default_cwd") or DEFAULT_CWD)
    arxiv_raw = raw.get("arxiv_summary", {})
    if not isinstance(arxiv_raw, dict):
        arxiv_raw = {}
    arxiv_summary_label = str(
        arxiv_raw.get("label") or raw.get("arxiv_summary_label") or DEFAULT_ARXIV_SUMMARY_LABEL
    )
    arxiv_summary_cwd = str(arxiv_raw.get("cwd") or raw.get("arxiv_summary_cwd") or default_cwd)
    arxiv_summary_methodology = str(
        arxiv_raw.get("methodology")
        or raw.get("arxiv_summary_methodology")
        or DEFAULT_ARXIV_SUMMARY_METHODOLOGY
    )
    allowed = raw.get("allowed_cwd_roots")
    if not allowed:
        allowed = [default_cwd]
    if isinstance(allowed, str):
        allowed = [allowed]
    protected_sessions = set(_as_string_tuple(raw.get("protected_sessions")))
    protected_sessions.add(arxiv_summary_label)

    return CodexPluginConfig(
        codex_bin=str(raw.get("codex_bin") or "codex"),
        default_cwd=default_cwd,
        allowed_cwd_roots=tuple(str(item) for item in allowed),
        max_parallel_jobs=max(1, int(raw.get("max_parallel_jobs", 2))),
        per_session_queue_limit=max(1, int(raw.get("per_session_queue_limit", 10))),
        spawn_timeout_seconds=max(1, min(120, int(raw.get("spawn_timeout_seconds", 30)))),
        job_timeout_seconds=max(30, int(raw.get("job_timeout_seconds", 3600))),
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
        max_image_bytes=_bounded_int(
            raw, "max_image_bytes", 20 * 1024**2, 64 * 1024, 100 * 1024**2
        ),
        max_image_total_bytes=_bounded_int(
            raw, "max_image_total_bytes", 100 * 1024**2, 64 * 1024, 512 * 1024**2
        ),
        max_image_pixels=_bounded_int(raw, "max_image_pixels", 40_000_000, 1_024, 100_000_000),
        max_image_frames=_bounded_int(raw, "max_image_frames", 120, 1, 500),
        max_qq_images=_bounded_int(raw, "max_qq_images", 10, 1, 20),
        sandbox=str(raw.get("sandbox") or "workspace-write"),
        approval_policy=str(raw.get("approval_policy") or "never"),
        skip_git_repo_check=bool(raw.get("skip_git_repo_check", True)),
        protected_sessions=tuple(sorted(protected_sessions)),
        arxiv_summary_label=arxiv_summary_label,
        arxiv_summary_cwd=arxiv_summary_cwd,
        arxiv_summary_methodology=arxiv_summary_methodology,
        session_ttl_days=max(0, int(raw.get("session_ttl_days", 90))),
        artifact_retention_days=max(0, int(raw.get("artifact_retention_days", 30))),
        emergency_disk_bytes=max(
            64 * 1024 * 1024,
            int(raw.get("emergency_disk_bytes", 10 * 1024**3)),
        ),
        emergency_queue_limit=max(10, int(raw.get("emergency_queue_limit", 1000))),
    )

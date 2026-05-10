from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_CWD = "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"
LABEL_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"


@dataclass(frozen=True)
class CodexPluginConfig:
    codex_bin: str
    default_cwd: str
    allowed_cwd_roots: tuple[str, ...]
    max_parallel_jobs: int
    per_session_queue_limit: int
    job_timeout_seconds: int
    sandbox: str
    approval_policy: str
    skip_git_repo_check: bool


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


def load_plugin_config(context: Any) -> CodexPluginConfig:
    raw = _merged_plugin_config(context)
    default_cwd = str(raw.get("default_cwd") or DEFAULT_CWD)
    allowed = raw.get("allowed_cwd_roots")
    if not allowed:
        allowed = [default_cwd]
    if isinstance(allowed, str):
        allowed = [allowed]

    return CodexPluginConfig(
        codex_bin=str(raw.get("codex_bin") or "codex"),
        default_cwd=default_cwd,
        allowed_cwd_roots=tuple(str(item) for item in allowed),
        max_parallel_jobs=max(1, int(raw.get("max_parallel_jobs", 2))),
        per_session_queue_limit=max(1, int(raw.get("per_session_queue_limit", 10))),
        job_timeout_seconds=max(30, int(raw.get("job_timeout_seconds", 3600))),
        sandbox=str(raw.get("sandbox") or "workspace-write"),
        approval_policy=str(raw.get("approval_policy") or "never"),
        skip_git_repo_check=bool(raw.get("skip_git_repo_check", True)),
    )

"""Regression tests for the final Docker image boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_image_is_separate_from_compiler_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    builder, runtime = dockerfile.split("FROM python:3.13-slim AS runtime", maxsplit=1)

    assert builder.startswith("FROM python:3.13-slim AS builder")
    assert "apt-get install" in builder
    assert "gcc" in builder
    assert "apt-get install" not in runtime
    assert "gcc" not in runtime
    assert "COPY --from=builder /opt/xiaoqing-venv /opt/xiaoqing-venv" in runtime
    assert "--require-hashes" in builder


def test_trusted_admin_root_boundary_is_explicitly_documented() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    boundary = (ROOT / "docs" / "container-security.md").read_text(encoding="utf-8")

    assert "USER " not in dockerfile
    assert "intentionally runs as `root`" in boundary
    assert "/var/run/docker.sock" in boundary
    assert "not a security boundary" in boundary

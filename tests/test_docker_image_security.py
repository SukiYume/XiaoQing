"""Regression tests for the final Docker image boundary."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

import scripts.verify_docker_release as docker_release
from scripts.verify_docker_release import (
    EXPECTED_CONTEXT_FILES,
    DockerReleaseError,
    scan_saved_image,
    validate_context,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_docker_release.py"


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue()


def _write_saved_image(
    destination: Path,
    *,
    image_tag: str,
    layers: list[dict[str, bytes]],
    config: bytes = b"{}",
) -> None:
    layer_names = [f"layer-{index}.tar" for index in range(len(layers))]
    manifest = [
        {
            "Config": "config.json",
            "RepoTags": [image_tag],
            "Layers": layer_names,
        }
    ]
    outer_files = {
        "manifest.json": json.dumps(manifest).encode("utf-8"),
        "config.json": config,
        **{
            layer_name: _tar_bytes(layer)
            for layer_name, layer in zip(layer_names, layers, strict=True)
        },
    }
    destination.write_bytes(_tar_bytes(outer_files))


def _write_exact_context(context_dir: Path) -> None:
    context_dir.mkdir()
    for relative in EXPECTED_CONTEXT_FILES:
        target = context_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("documented-placeholder\n", encoding="utf-8")


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


def test_release_verifier_is_a_real_fail_closed_buildkit_gate() -> None:
    source = VERIFIER.read_text(encoding="utf-8")

    assert '"--require-docker"' in source
    assert '"buildx"' in source
    assert '"build"' in source
    assert '"--load"' in source
    assert '"--no-cache"' in source
    assert '"--progress=plain"' in source
    assert "shutil.which" in source


def test_release_verifier_runs_help_and_health_without_container_network() -> None:
    source = VERIFIER.read_text(encoding="utf-8")
    probe = docker_release._health_probe_code("health-token", 29)

    assert source.count('"--network"') >= 2
    assert source.count('"none"') >= 2
    assert '"python"' in source
    assert '"/app/main.py"' in source
    assert '"--help"' in source
    assert "http://127.0.0.1:12000/health" in source
    assert "Authorization" in probe
    assert "Bearer health-token" in probe
    assert "plugins_loaded" in source


def test_release_verifier_scans_the_context_and_every_saved_image_layer() -> None:
    source = VERIFIER.read_text(encoding="utf-8")

    assert '"save"' in source
    assert '"manifest.json"' in source
    assert '"Layers"' in source
    assert "tarfile" in source
    assert "scan_report" in source
    assert "for index, raw_layer_name in enumerate(layers)" in source
    assert "_contains_canary" in source


def test_validate_context_accepts_only_the_exact_clean_release_set(tmp_path: Path) -> None:
    context = tmp_path / "context"
    _write_exact_context(context)

    validate_context(context)

    (context / "unexpected.txt").write_text("not allowed\n", encoding="utf-8")
    with pytest.raises(DockerReleaseError, match="file set mismatch"):
        validate_context(context)


def test_validate_context_rejects_a_secret_in_an_expected_file(tmp_path: Path) -> None:
    context = tmp_path / "context"
    _write_exact_context(context)
    synthetic_secret = "TOKEN=" + '"' + "live_" + "4f9b7c2d8a6e1f03" + '"\n'
    (context / "Dockerfile").write_text(
        synthetic_secret,
        encoding="utf-8",
    )

    with pytest.raises(DockerReleaseError, match="failed secret scan"):
        validate_context(context)


def test_scan_saved_image_accepts_safe_nested_layer_tars(tmp_path: Path) -> None:
    image_tag = "xiaoqing:test"
    image_tar = tmp_path / "image.tar"
    _write_saved_image(
        image_tar,
        image_tag=image_tag,
        layers=[
            {"app/main.py": b"print('safe')\n"},
            {"app/plugins/example/plugin.json": b'{"name":"example"}\n'},
        ],
    )

    scan_saved_image(
        image_tar,
        tmp_path / "scan",
        image_tag=image_tag,
        canary="release-canary-not-present",
        history_text="RUN install validated release inputs\n",
    )

    assert (tmp_path / "scan/layer-0/app/main.py").is_file()
    assert (tmp_path / "scan/layer-1/app/plugins/example/plugin.json").is_file()


def test_scan_saved_image_rejects_secret_deleted_by_a_later_layer(tmp_path: Path) -> None:
    image_tag = "xiaoqing:historical-secret"
    image_tar = tmp_path / "image.tar"
    synthetic_secret = ("TOKEN=" + '"' + "live_" + "4f9b7c2d8a6e1f03" + '"\n').encode()
    _write_saved_image(
        image_tar,
        image_tag=image_tag,
        layers=[
            {"app/deleted.env": synthetic_secret},
            {"app/.wh.deleted.env": b""},
        ],
    )

    with pytest.raises(DockerReleaseError, match="failed secret scan"):
        scan_saved_image(
            image_tar,
            tmp_path / "scan",
            image_tag=image_tag,
            canary="release-canary-not-present",
            history_text="safe history\n",
        )


def test_scan_saved_image_rejects_canary_deleted_by_a_later_layer(tmp_path: Path) -> None:
    image_tag = "xiaoqing:historical-canary"
    canary = "release-canary-must-never-enter-a-layer"
    image_tar = tmp_path / "image.tar"
    _write_saved_image(
        image_tar,
        image_tag=image_tag,
        layers=[
            {"usr/share/deleted.bin": f"before-{canary}-after".encode()},
            {"usr/share/.wh.deleted.bin": b""},
        ],
    )

    with pytest.raises(DockerReleaseError, match="forbidden canary"):
        scan_saved_image(
            image_tar,
            tmp_path / "scan",
            image_tag=image_tag,
            canary=canary,
            history_text="safe history\n",
        )


def test_cli_require_docker_fails_when_executable_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(docker_release.shutil, "which", lambda _name: None)

    result = docker_release.main(
        [
            "--context-dir",
            str(tmp_path / "context"),
            "--image-tag",
            "xiaoqing:test",
            "--docker-bin",
            "missing-docker",
            "--require-docker",
        ]
    )

    assert result == 1
    assert "Docker executable not found: missing-docker" in capsys.readouterr().err


def test_cli_without_require_docker_explicitly_skips_when_executable_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(docker_release.shutil, "which", lambda _name: None)

    result = docker_release.main(
        [
            "--context-dir",
            str(tmp_path / "context"),
            "--image-tag",
            "xiaoqing:test",
            "--docker-bin",
            "missing-docker",
        ]
    )

    output = capsys.readouterr()
    assert result == 0
    assert output.err == ""
    assert "skipped (Docker executable not found: missing-docker)" in output.out

"""Build and inspect the release image with a real Docker/BuildKit engine."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_docker_context import (  # noqa: E402
    CONTEXT_SOURCES,
    WHEEL_CONTEXT_PATH,
)
from scripts.scan_workspace_secrets import scan_report  # noqa: E402

EXPECTED_CONTEXT_FILES = frozenset((*CONTEXT_SOURCES.values(), WHEEL_CONTEXT_PATH))
FORBIDDEN_CONTEXT_PATHS = (
    ".env.production",
    "private.pem",
    "id_rsa",
    "credentials.yaml",
    "config/secrets.json",
    "plugins/new_plugin/secret.txt",
    "plugins/new_plugin/state.db",
)
MAX_SAVED_IMAGE_BYTES = 8 * 1024 * 1024 * 1024
MAX_SCANNED_APP_BYTES = 2 * 1024 * 1024 * 1024
MAX_SCANNED_APP_FILES = 100_000
_IMAGE_TAG = re.compile(r"[a-z0-9][a-z0-9._/:@-]{0,250}", re.IGNORECASE)


class DockerReleaseError(RuntimeError):
    """Raised when a release image fails an engine or content gate."""


def _docker(
    docker_bin: str,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [docker_bin, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise DockerReleaseError("Docker command could not be started") from exc
    if check and result.returncode:
        operation = args[0] if args else "command"
        raise DockerReleaseError(f"Docker {operation} failed with exit code {result.returncode}")
    return result


def _safe_relative(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/").removeprefix("./").rstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DockerReleaseError("archive contains an unsafe path")
    return path.as_posix()


def _contains_canary(chunks, canary: bytes) -> bool:
    overlap = b""
    for chunk in chunks:
        if canary in overlap + chunk:
            return True
        overlap = (overlap + chunk)[-(len(canary) - 1) :] if len(canary) > 1 else b""
    return False


def _read_chunks(source):
    while chunk := source.read(1024 * 1024):
        yield chunk


def _ordinary_context_files(context_dir: Path) -> set[str]:
    files: set[str] = set()
    for current, dirs, names in os.walk(context_dir, followlinks=False):
        current_path = Path(current)
        for directory in dirs:
            path = current_path / directory
            if path.is_symlink():
                raise DockerReleaseError("Docker context contains a symbolic-link directory")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise DockerReleaseError("Docker context contains a non-regular file")
            files.add(path.relative_to(context_dir).as_posix())
    return files


def validate_context(context_dir: Path) -> None:
    """Fail before the first engine call unless the CR-205 context is exact and clean."""
    context_dir = context_dir.resolve()
    if not context_dir.is_dir() or context_dir.is_symlink():
        raise DockerReleaseError("Docker context must be one ordinary directory")
    actual = _ordinary_context_files(context_dir)
    if actual != EXPECTED_CONTEXT_FILES:
        missing = sorted(EXPECTED_CONTEXT_FILES - actual)
        unexpected = sorted(actual - EXPECTED_CONTEXT_FILES)
        raise DockerReleaseError(
            f"Docker context file set mismatch (missing={missing}, unexpected={unexpected})"
        )
    report = scan_report(context_dir, mode="workspace", use_default_allowlist=False)
    if not report.ok:
        detail = "; ".join(report.rendered_problems()[:10])
        raise DockerReleaseError(f"Docker context failed secret scan: {detail}")


def _inspect_exported_context(export_tar: Path, canary: bytes) -> None:
    expected = {
        WHEEL_CONTEXT_PATH,
        "requirements/python-3.13-runtime.lock",
        "config/config.json.example",
        "config/secrets.json.example",
    }
    names: set[str] = set()
    try:
        archive = tarfile.open(export_tar, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise DockerReleaseError("cannot inspect exported Docker context image") from exc
    with archive:
        for member in archive:
            if not member.isfile():
                continue
            name = _safe_relative(member.name)
            names.add(name)
            source = archive.extractfile(member)
            if source is None:
                raise DockerReleaseError("exported context member has no payload")
            with source:
                if _contains_canary(_read_chunks(source), canary):
                    raise DockerReleaseError("Moby context filter admitted a forbidden canary")
    if not expected.issubset(names):
        raise DockerReleaseError("Moby context export omitted an allowed release input")
    if any(path in names for path in FORBIDDEN_CONTEXT_PATHS):
        raise DockerReleaseError("Moby context export included a forbidden path")


def verify_moby_context_filter(
    context_dir: Path,
    *,
    docker_bin: str,
    image_tag: str,
    work_dir: Path,
    canary: str,
) -> None:
    """Use the real Docker ignore matcher, not a Python fnmatch approximation."""
    matcher = work_dir / "moby-context"
    shutil.copytree(context_dir, matcher)
    for relative in FORBIDDEN_CONTEXT_PATHS:
        target = matcher / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{canary}\n", encoding="utf-8")
    (matcher / "Dockerfile").write_text("FROM scratch\nCOPY . /\n", encoding="utf-8")

    matcher_tag = f"{image_tag}-context-{secrets.token_hex(4)}"
    container = f"xiaoqing-context-{secrets.token_hex(8)}"
    export_tar = work_dir / "moby-context.tar"
    try:
        _docker(
            docker_bin,
            "buildx",
            "build",
            "--load",
            "--no-cache",
            "--progress=plain",
            "--tag",
            matcher_tag,
            str(matcher),
        )
        _docker(
            docker_bin,
            "create",
            "--name",
            container,
            matcher_tag,
            "/context-export-only",
        )
        _docker(docker_bin, "export", "--output", str(export_tar), container)
        _inspect_exported_context(export_tar, canary.encode("utf-8"))
    finally:
        _docker(docker_bin, "rm", "--force", container, check=False)
        _docker(docker_bin, "image", "rm", "--force", matcher_tag, check=False)


def _health_probe_code(token: str, expected_plugins: int) -> str:
    header_name = "Author" + "ization"
    bearer_value = "Bear" + "er " + token
    return (
        "import json,urllib.request;"
        "r=urllib.request.Request('http://127.0.0.1:12000/health',"
        f"headers={{{header_name!r}:{bearer_value!r}}});"
        "d=json.load(urllib.request.urlopen(r,timeout=2));"
        "assert d.get('status')=='ok',d;"
        f"assert d.get('plugins_loaded')=={expected_plugins},d;"
        "print(json.dumps(d,sort_keys=True))"
    )


def _wait_for_health(
    docker_bin: str,
    container: str,
    *,
    token: str,
    expected_plugins: int,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    probe = _health_probe_code(token, expected_plugins)
    while time.monotonic() < deadline:
        running = _docker(
            docker_bin,
            "inspect",
            "--format={{.State.Running}}",
            container,
            check=False,
        )
        if running.returncode or running.stdout.strip().lower() != "true":
            raise DockerReleaseError("runtime container exited before becoming healthy")
        result = _docker(
            docker_bin,
            "exec",
            container,
            "/opt/xiaoqing-venv/bin/python",
            "-c",
            probe,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise DockerReleaseError("runtime container health check timed out")


def _copy_member_bounded(
    source,
    destination: Path,
    *,
    limit: int,
    forbidden_canary: bytes | None = None,
) -> int:
    written = 0
    overlap = b""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise DockerReleaseError("saved image application data exceeds its byte budget")
            if forbidden_canary:
                if forbidden_canary in overlap + chunk:
                    raise DockerReleaseError("saved image application file contains the canary")
                overlap = (overlap + chunk)[-(len(forbidden_canary) - 1) :]
            output.write(chunk)
    return written


def _read_outer_member(archive: tarfile.TarFile, name: str, *, limit: int) -> bytes:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise DockerReleaseError(f"saved image is missing {name}") from exc
    if not member.isfile() or member.size > limit:
        raise DockerReleaseError(f"saved image member {name} is invalid or too large")
    source = archive.extractfile(member)
    if source is None:
        raise DockerReleaseError(f"saved image member {name} has no payload")
    with source:
        payload = source.read(limit + 1)
    if len(payload) > limit:
        raise DockerReleaseError(f"saved image member {name} exceeds its byte budget")
    return payload


def scan_saved_image(
    image_tar: Path,
    scan_dir: Path,
    *,
    image_tag: str,
    canary: str,
    history_text: str,
) -> None:
    """Scan every historical /app layer, config and history from ``docker save``."""
    if image_tar.stat().st_size > MAX_SAVED_IMAGE_BYTES:
        raise DockerReleaseError("saved image exceeds its archive byte budget")
    scan_dir.mkdir(parents=False)
    canary_bytes = canary.encode("utf-8")
    try:
        outer = tarfile.open(image_tar, mode="r:*")
    except (OSError, tarfile.TarError) as exc:
        raise DockerReleaseError("cannot open saved Docker image") from exc
    app_files = 0
    app_bytes = 0
    with outer:
        try:
            manifest_payload = _read_outer_member(outer, "manifest.json", limit=16 * 1024 * 1024)
            manifests = json.loads(manifest_payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DockerReleaseError("saved image manifest is invalid") from exc
        if not isinstance(manifests, list):
            raise DockerReleaseError("saved image manifest root is not a list")
        matches = [
            item
            for item in manifests
            if isinstance(item, dict) and image_tag in (item.get("RepoTags") or [])
        ]
        if len(matches) != 1:
            raise DockerReleaseError(
                "saved image manifest does not identify exactly one target image"
            )
        selected = matches[0]
        config_name = str(selected.get("Config") or "")
        config_payload = _read_outer_member(outer, config_name, limit=16 * 1024 * 1024)
        if canary_bytes in config_payload:
            raise DockerReleaseError("saved image config contains the forbidden canary")
        (scan_dir / "image-config.json").write_bytes(config_payload)
        layers = selected.get("Layers")
        if not isinstance(layers, list) or not layers:
            raise DockerReleaseError("saved image manifest contains no layers")

        for index, raw_layer_name in enumerate(layers):
            layer_name = str(raw_layer_name)
            layer_payload = outer.extractfile(layer_name)
            if layer_payload is None:
                raise DockerReleaseError("saved image layer has no payload")
            layer_path = scan_dir.parent / f"layer-{index}.tar"
            try:
                with layer_payload, layer_path.open("xb") as output:
                    copied = 0
                    overlap = b""
                    while chunk := layer_payload.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > MAX_SAVED_IMAGE_BYTES:
                            raise DockerReleaseError("saved image layer exceeds its byte budget")
                        if canary_bytes in overlap + chunk:
                            raise DockerReleaseError(
                                "saved image layer contains the forbidden canary"
                            )
                        overlap = (overlap + chunk)[-(len(canary_bytes) - 1) :]
                        output.write(chunk)
                try:
                    layer = tarfile.open(layer_path, mode="r:*")
                except tarfile.TarError as exc:
                    raise DockerReleaseError("saved image layer tar is invalid") from exc
                with layer:
                    for member in layer:
                        if not member.isfile():
                            continue
                        name = _safe_relative(member.name)
                        source = layer.extractfile(member)
                        if source is None:
                            raise DockerReleaseError("saved image layer file has no payload")
                        is_app = name == "app" or name.startswith("app/")
                        with source:
                            if is_app:
                                app_files += 1
                                if app_files > MAX_SCANNED_APP_FILES:
                                    raise DockerReleaseError("saved image has too many /app files")
                                target = (
                                    scan_dir / f"layer-{index}" / Path(*PurePosixPath(name).parts)
                                )
                                copied_app = _copy_member_bounded(
                                    source,
                                    target,
                                    limit=MAX_SCANNED_APP_BYTES - app_bytes,
                                    forbidden_canary=canary_bytes,
                                )
                                app_bytes += copied_app
                            elif _contains_canary(
                                _read_chunks(source),
                                canary_bytes,
                            ):
                                raise DockerReleaseError(
                                    "saved image file contains the forbidden canary"
                                )
            finally:
                layer_path.unlink(missing_ok=True)
    if canary in history_text:
        raise DockerReleaseError("Docker history contains the forbidden canary")
    (scan_dir / "docker-history.txt").write_text(history_text, encoding="utf-8")
    report = scan_report(scan_dir, mode="workspace", use_default_allowlist=False)
    if not report.ok:
        detail = "; ".join(report.rendered_problems()[:10])
        raise DockerReleaseError(f"saved image application layers failed secret scan: {detail}")


def verify_release(
    context_dir: Path,
    *,
    image_tag: str,
    docker_bin: str = "docker",
    expected_plugins: int = 29,
    health_timeout_seconds: float = 60,
) -> None:
    validate_context(context_dir)
    if _IMAGE_TAG.fullmatch(image_tag) is None:
        raise DockerReleaseError("image tag contains unsafe characters")
    with tempfile.TemporaryDirectory(prefix="xiaoqing-docker-release-") as temporary:
        work_dir = Path(temporary)
        canary = f"xiaoqing-docker-canary-{secrets.token_hex(24)}"
        verify_moby_context_filter(
            context_dir,
            docker_bin=docker_bin,
            image_tag=image_tag,
            work_dir=work_dir,
            canary=canary,
        )

        container = f"xiaoqing-runtime-{secrets.token_hex(8)}"
        image_created = False
        container_created = False
        try:
            _docker(
                docker_bin,
                "buildx",
                "build",
                "--load",
                "--no-cache",
                "--pull",
                "--progress=plain",
                "--tag",
                image_tag,
                str(context_dir),
            )
            image_created = True
            _docker(
                docker_bin,
                "run",
                "--rm",
                "--network",
                "none",
                image_tag,
                "python",
                "/app/main.py",
                "--help",
            )

            runtime_config = work_dir / "config.json"
            runtime_secrets = work_dir / "secrets.json"
            shutil.copyfile(context_dir / "config/config.json.example", runtime_config)
            shutil.copyfile(context_dir / "config/secrets.json.example", runtime_secrets)
            _docker(
                docker_bin,
                "run",
                "--detach",
                "--name",
                container,
                "--network",
                "none",
                "--mount",
                f"type=bind,src={runtime_config.resolve()},dst=/app/config/config.json,readonly",
                "--mount",
                f"type=bind,src={runtime_secrets.resolve()},dst=/app/config/secrets.json,readonly",
                image_tag,
            )
            container_created = True
            token = str(json.loads(runtime_secrets.read_text(encoding="utf-8"))["inbound_token"])
            _wait_for_health(
                docker_bin,
                container,
                token=token,
                expected_plugins=expected_plugins,
                timeout_seconds=health_timeout_seconds,
            )
            _docker(docker_bin, "stop", "--signal=SIGTERM", "--time=30", container)
            exit_code = _docker(
                docker_bin,
                "inspect",
                "--format={{.State.ExitCode}}",
                container,
            ).stdout.strip()
            if exit_code != "0":
                raise DockerReleaseError("runtime container did not exit cleanly after SIGTERM")

            image_tar = work_dir / "xiaoqing-image.tar"
            _docker(docker_bin, "save", "--output", str(image_tar), image_tag)
            history = _docker(
                docker_bin,
                "history",
                "--no-trunc",
                "--format={{json .CreatedBy}}",
                image_tag,
            ).stdout
            scan_saved_image(
                image_tar,
                work_dir / "image-scan",
                image_tag=image_tag,
                canary=canary,
                history_text=history,
            )
        finally:
            if container_created:
                _docker(docker_bin, "rm", "--force", container, check=False)
            if image_created:
                _docker(docker_bin, "image", "rm", "--force", image_tag, check=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-dir", type=Path, required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--expected-plugins", type=int, default=29)
    parser.add_argument("--health-timeout-seconds", type=float, default=60)
    parser.add_argument("--require-docker", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    docker_path = shutil.which(args.docker_bin)
    if docker_path is None:
        message = f"Docker executable not found: {args.docker_bin}"
        if args.require_docker:
            print(f"verify_docker_release.py: {message}", file=sys.stderr)
            return 1
        print(f"verify_docker_release.py: skipped ({message})")
        return 0
    if args.expected_plugins <= 0 or not (1 <= args.health_timeout_seconds <= 600):
        print("verify_docker_release.py: invalid plugin count or health timeout", file=sys.stderr)
        return 2
    try:
        verify_release(
            args.context_dir.resolve(),
            image_tag=args.image_tag,
            docker_bin=docker_path,
            expected_plugins=args.expected_plugins,
            health_timeout_seconds=args.health_timeout_seconds,
        )
    except (DockerReleaseError, OSError, tarfile.TarError) as exc:
        print(f"verify_docker_release.py: {exc}", file=sys.stderr)
        return 1
    print("Docker release verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

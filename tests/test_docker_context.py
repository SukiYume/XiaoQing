from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from scripts import build_docker_context as docker_context

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTEXT_FILES = {
    ".dockerignore",
    "Dockerfile",
    "artifacts/xiaoqing.whl",
    "config/config.json.example",
    "config/secrets.json.example",
    "requirements/python-3.13-runtime.lock",
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "xiaoqing"\nversion = "4.1.0"\n',
        encoding="utf-8",
    )
    for relative in (
        ".dockerignore",
        "Dockerfile",
    ):
        shutil.copyfile(ROOT / relative, repo / relative)
    for relative, content in (
        ("requirements/python-3.13-runtime.lock", "example==1.0 --hash=sha256:00\n"),
        ("config/config.json.example", "{}\n"),
        ("config/secrets.json.example", "{}\n"),
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


def _metadata(*, name: str = "xiaoqing", version: str = "4.1.0") -> bytes:
    return (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n").encode()


def _wheel(
    dist: Path,
    *,
    filename: str = "xiaoqing-4.1.0-py3-none-any.whl",
    metadata_name: str = "xiaoqing",
    metadata_version: str = "4.1.0",
    entries: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / filename
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xiaoqing-4.1.0.dist-info/METADATA",
            _metadata(name=metadata_name, version=metadata_version),
        )
        archive.writestr("main.py", b"print('xiaoqing')\n")
        for member, payload in entries or []:
            archive.writestr(member, payload)
    return path


def _context_files(context: Path) -> set[str]:
    return {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}


def test_context_contains_only_the_fixed_release_file_set(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    canaries = {
        ".env",
        "plugins/new_plugin/secret.txt",
        "plugins/new_plugin/private.pem",
        "plugins/new_plugin/id_rsa",
        "plugins/new_plugin/.env.production",
        "plugins/new_plugin/credentials.yaml",
        "plugins/new_plugin/state.db",
    }
    for relative in canaries:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"workspace-only canary\n")
    wheel = _wheel(tmp_path / "dist")
    context = tmp_path / "context"

    metadata = docker_context.build_context(repo, wheel.parent, context)

    assert _context_files(context) == EXPECTED_CONTEXT_FILES
    assert canaries.isdisjoint(_context_files(context))
    assert (context / "artifacts" / "xiaoqing.whl").read_bytes() == wheel.read_bytes()
    assert metadata.file_count == len(EXPECTED_CONTEXT_FILES)
    assert metadata.wheel_filename == wheel.name


def test_dockerignore_is_an_exact_deny_all_contract() -> None:
    rules = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert rules == [
        "**",
        "!.dockerignore",
        "!Dockerfile",
        "!artifacts/",
        "!artifacts/xiaoqing.whl",
        "!requirements/",
        "!requirements/python-3.13-runtime.lock",
        "!config/",
        "!config/config.json.example",
        "!config/secrets.json.example",
    ]
    assert not any("*" in rule.removeprefix("!") for rule in rules[1:])


def test_dockerfile_installs_only_lock_and_fixed_wheel() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "pip install --no-cache-dir --require-hashes" in dockerfile
    assert "-r requirements/python-3.13-runtime.lock" in dockerfile
    assert "COPY artifacts/xiaoqing.whl ./artifacts/xiaoqing.whl" in dockerfile
    assert "pip install --no-cache-dir --no-deps" in dockerfile
    assert "COPY --from=builder /opt/xiaoqing-app /app" in dockerfile
    assert "COPY config/config.json.example ./config/config.json.example" in dockerfile
    assert "COPY config/secrets.json.example ./config/secrets.json.example" in dockerfile
    for forbidden in ("COPY .", "COPY core", "COPY plugins", "COPY main.py", "COPY pyproject"):
        assert forbidden not in dockerfile


def test_malicious_wheel_secret_cannot_allowlist_itself(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret = "live" + "-credential-value-987654321"
    key = "API" + "_KEY"
    rule_id = "credential.assignment.api-key.v1"
    allowlist = {
        "entries": [
            {
                "path": "plugins/evil.py",
                "rule_id": rule_id,
                "fingerprint": hashlib.sha256(secret.encode()).hexdigest(),
                "reason": "artifact-controlled suppression must never be trusted",
            }
        ]
    }
    wheel = _wheel(
        tmp_path / "dist",
        entries=[
            ("plugins/evil.py", f'{key} = "{secret}"\n'.encode()),
            (".secret-scan-allowlist.json", json.dumps(allowlist).encode()),
        ],
    )
    context = tmp_path / "context"

    with pytest.raises(docker_context.DockerContextError, match="failed secret scan"):
        docker_context.build_context(repo, wheel.parent, context)

    assert not context.exists()


def test_wheel_symbolic_link_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    link = zipfile.ZipInfo("plugins/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    wheel = _wheel(tmp_path / "dist", entries=[(link, b"../../outside")])

    with pytest.raises(docker_context.DockerContextError, match="symbolic link"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


def test_wheel_special_file_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fifo = zipfile.ZipInfo("plugins/fifo")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
    wheel = _wheel(tmp_path / "dist", entries=[(fifo, b"")])

    with pytest.raises(docker_context.DockerContextError, match="non-regular member"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


@pytest.mark.parametrize("member", ["/absolute.py", "../outside.py", "plugins/../../outside.py"])
def test_wheel_path_escape_is_rejected(tmp_path: Path, member: str) -> None:
    repo = _repo(tmp_path)
    wheel = _wheel(tmp_path / "dist", entries=[(member, b"pass\n")])

    with pytest.raises(docker_context.DockerContextError, match="escapes extraction root"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


def test_wheel_duplicate_member_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.warns(UserWarning, match="Duplicate name"):
        wheel = _wheel(
            tmp_path / "dist",
            entries=[("plugins/duplicate.py", b"one\n"), ("plugins/duplicate.py", b"two\n")],
        )

    with pytest.raises(docker_context.DockerContextError, match="duplicate member"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


@pytest.mark.parametrize(
    ("filename", "metadata_name", "metadata_version", "message"),
    [
        (
            "xiaoqing-4.2.0-py3-none-any.whl",
            "xiaoqing",
            "4.1.0",
            "filename version",
        ),
        (
            "xiaoqing-4.1.0-py3-none-any.whl",
            "other-project",
            "4.1.0",
            "METADATA Name",
        ),
        (
            "xiaoqing-4.1.0-py3-none-any.whl",
            "xiaoqing",
            "4.2.0",
            "METADATA Version",
        ),
    ],
)
def test_wheel_identity_must_match_pyproject(
    tmp_path: Path,
    filename: str,
    metadata_name: str,
    metadata_version: str,
    message: str,
) -> None:
    repo = _repo(tmp_path)
    wheel = _wheel(
        tmp_path / "dist",
        filename=filename,
        metadata_name=metadata_name,
        metadata_version=metadata_version,
    )

    with pytest.raises(docker_context.DockerContextError, match=message):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


def test_multiple_xiaoqing_wheels_are_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dist = tmp_path / "dist"
    _wheel(dist)
    _wheel(dist, filename="xiaoqing-4.2.0-py3-none-any.whl")

    with pytest.raises(docker_context.DockerContextError, match="exactly one"):
        docker_context.build_context(repo, dist, tmp_path / "context")


def test_wheel_file_count_budget_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    wheel = _wheel(tmp_path / "dist")
    monkeypatch.setattr(docker_context, "MAX_WHEEL_FILES", 1)

    with pytest.raises(docker_context.DockerContextError, match="file limit"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


def test_wheel_byte_budget_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    wheel = _wheel(tmp_path / "dist")
    monkeypatch.setattr(docker_context, "MAX_WHEEL_BYTES", 16)

    with pytest.raises(docker_context.DockerContextError, match="byte limit"):
        docker_context.build_context(repo, wheel.parent, tmp_path / "context")


def test_existing_context_is_never_merged_or_deleted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wheel = _wheel(tmp_path / "dist")
    context = tmp_path / "context"
    context.mkdir()
    canary = context / "keep.txt"
    canary.write_text("keep", encoding="utf-8")

    with pytest.raises(docker_context.DockerContextError, match="must not already exist"):
        docker_context.build_context(repo, wheel.parent, context)

    assert canary.read_text(encoding="utf-8") == "keep"

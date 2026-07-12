"""Safety and reproducibility contracts for immutable remote deployment."""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import build_deploy_stage as stage_module
from scripts.build_deploy_stage import StagingError, build_stage, extract_archive, parse_manifest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "sync_to_remote.sh"
SENTINEL = ROOT / ".xiaoqing-sync-root"
TRACKED_ASSETS = (
    "sync_to_remote.sh",
    ".xiaoqing-sync-root",
    "docs/remote-sync.md",
    "tests/test_sync_to_remote.py",
)


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD")


def _init_stage_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "deploy-test@example.invalid")
    _run_git(repo, "config", "user.name", "Deploy Test")
    (repo / "deploy").mkdir()
    (repo / "assets").mkdir()
    (repo / ".xiaoqing-sync-root").write_text("xiaoqing-sync-root-v1\n", encoding="utf-8")
    (repo / "app.py").write_text("print('runtime')\n", encoding="utf-8")
    (repo / "assets" / "runtime.txt").write_text("runtime asset\n", encoding="utf-8")
    (repo / "outside-canary.txt").write_text("must stay outside\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
    (repo / "deploy" / "runtime-paths.txt").write_text(
        "# xiaoqing-deploy-manifest-v1\n"
        ".xiaoqing-sync-root\n"
        "app.py\n"
        "assets\n",
        encoding="utf-8",
    )
    commit = _commit(repo, "runtime")
    ignored_key = "API" + "_KEY"
    (repo / "ignored.env").write_text(
        f'{ignored_key}="ignored_4f9b7c2d8a6e1f03"\n',
        encoding="utf-8",
    )
    (repo / "untracked-canary.txt").write_text("must stay outside\n", encoding="utf-8")
    return repo, commit


def _bash_directory(path: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", "pwd"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    subprocess.run(
        ["bash", "-c", f"chmod +x {shlex.quote(f'{_bash_directory(path.parent)}/{path.name}')}"],
        check=True,
    )


def _make_sync_repo(tmp_path: Path) -> tuple[Path, str, dict[str, str], Path]:
    repo = tmp_path / "sync-repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "deploy").mkdir()
    shutil.copy2(SCRIPT, repo / "sync_to_remote.sh")
    shutil.copy2(ROOT / "scripts" / "build_deploy_stage.py", repo / "scripts")
    shutil.copy2(ROOT / "scripts" / "scan_workspace_secrets.py", repo / "scripts")
    (repo / ".xiaoqing-sync-root").write_text("xiaoqing-sync-root-v1\n", encoding="utf-8")
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (repo / "deploy" / "runtime-paths.txt").write_text(
        "# xiaoqing-deploy-manifest-v1\n.xiaoqing-sync-root\napp.py\n",
        encoding="utf-8",
    )
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "sync-test@example.invalid")
    _run_git(repo, "config", "user.name", "Sync Test")
    commit = _commit(repo, "sync runtime")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    rsync_log = tmp_path / "rsync.log"
    _write_executable(
        fake_bin / "ssh",
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"
        "printf '%s\\n' \"${FAKE_REMOTE_ROOT:-/safe/remote}\"\n",
    )
    _write_executable(
        fake_bin / "rsync",
        "#!/usr/bin/env bash\n"
        "dry=false\n"
        "for arg in \"$@\"; do [[ \"$arg\" == --dry-run ]] && dry=true; done\n"
        "if [[ \"$dry\" == true ]]; then\n"
        "  printf '%s\\n' '>f+++++++++ app.py'\n"
        "else\n"
        "  printf '%s\\n' apply >>\"$FAKE_RSYNC_LOG\"\n"
        "fi\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "_TEST_PATH_PREFIX": _bash_directory(fake_bin),
            "XIAOQING_SSH_BIN": f"{_bash_directory(fake_bin)}/ssh",
            "XIAOQING_RSYNC_BIN": f"{_bash_directory(fake_bin)}/rsync",
            "XIAOQING_SYNC_HOST": "fakehost",
            "XIAOQING_SYNC_DIR": "/requested/remote",
            "PYTHON": "python3",
            "FAKE_REMOTE_ROOT": "/safe/remote",
            "FAKE_RSYNC_LOG": f"{_bash_directory(rsync_log.parent)}/{rsync_log.name}",
        }
    )
    return repo, commit, env, rsync_log


def _run_sync(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    exported = {
        key: env[key]
        for key in (
            "PYTHON",
            "XIAOQING_SYNC_HOST",
            "XIAOQING_SYNC_DIR",
            "FAKE_REMOTE_ROOT",
            "FAKE_RSYNC_LOG",
            "XIAOQING_SSH_BIN",
            "XIAOQING_RSYNC_BIN",
        )
    }
    command = "; ".join(
        [f"export {key}={shlex.quote(value)}" for key, value in exported.items()]
        + [f"export PATH={shlex.quote(env['_TEST_PATH_PREFIX'])}:\"$PATH\""]
        + [
            "exec bash sync_to_remote.sh "
            + " ".join(shlex.quote(argument) for argument in args)
        ]
    )
    return subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _plan_from_output(output: str) -> str:
    match = re.search(r"^Plan SHA256: ([0-9a-f]{64})$", output, re.MULTILINE)
    assert match, output
    return match.group(1)


def test_sync_script_is_valid_bash_and_has_local_sentinel() -> None:
    result = subprocess.run(
        ["bash", "-n", SCRIPT.name],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert SENTINEL.read_text(encoding="utf-8") == "xiaoqing-sync-root-v1\n"


def test_sync_assets_are_not_ignored_and_shell_line_endings_are_fixed() -> None:
    ignored_results = [
        subprocess.run(["git", "check-ignore", "--quiet", asset], cwd=ROOT, check=False)
        for asset in TRACKED_ASSETS
    ]
    attributes = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", SCRIPT.name],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert all(result.returncode == 1 for result in ignored_results)
    assert "text: set" in attributes
    assert "eol: lf" in attributes
    assert b"\r\n" not in SCRIPT.read_bytes()


def test_cr203_sync_assets_remain_tracked_and_executable() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "--stage", "--", *TRACKED_ASSETS],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    by_path = {line.split("\t", 1)[1]: line.split(maxsplit=1)[0] for line in listed}
    assert set(by_path) == set(TRACKED_ASSETS)
    assert by_path["sync_to_remote.sh"] == "100755"


def test_project_deploy_manifest_has_versioned_runtime_boundary() -> None:
    manifest = ROOT / "deploy" / "runtime-paths.txt"
    entries = set(parse_manifest(manifest.read_bytes()))

    assert {
        ".xiaoqing-sync-root",
        "main.py",
        "core",
        "plugins",
        "pyproject.toml",
        "requirements/python-3.13-runtime.lock",
        "scripts/run-bot-monitor.ps1",
        "scripts/run_process_with_rotating_logs.py",
    }.issubset(entries)
    assert not {".git", "config/secrets.json", "logs", "data"} & entries


def test_stage_contains_only_manifest_paths_from_fixed_commit(tmp_path: Path) -> None:
    repo, commit = _init_stage_repo(tmp_path)
    stage = tmp_path / "stage"

    metadata = build_stage(repo, commit, stage)

    assert metadata.commit == commit
    assert metadata.file_count == 3
    assert (stage / "app.py").is_file()
    assert (stage / "assets" / "runtime.txt").is_file()
    assert not (stage / "outside-canary.txt").exists()
    assert not (stage / "ignored.env").exists()
    assert not (stage / "untracked-canary.txt").exists()


def test_stage_digests_are_stable_and_change_with_commit_or_manifest(tmp_path: Path) -> None:
    repo, first_commit = _init_stage_repo(tmp_path)
    first = build_stage(repo, first_commit, tmp_path / "first")
    repeated = build_stage(repo, first_commit, tmp_path / "repeated")
    assert first == repeated

    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    second_commit = _commit(repo, "change runtime")
    second = build_stage(repo, second_commit, tmp_path / "second")
    assert second.commit != first.commit
    assert second.tree_sha256 != first.tree_sha256
    assert second.manifest_sha256 == first.manifest_sha256

    (repo / "new.py").write_text("print('new')\n", encoding="utf-8")
    manifest = repo / "deploy" / "runtime-paths.txt"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "new.py\n", encoding="utf-8")
    third_commit = _commit(repo, "change manifest")
    third = build_stage(repo, third_commit, tmp_path / "third")
    assert third.manifest_sha256 != second.manifest_sha256
    assert third.tree_sha256 != second.tree_sha256


def test_stage_secret_scan_rejects_committed_manifest_secret(tmp_path: Path) -> None:
    repo, _commit_id = _init_stage_repo(tmp_path)
    value = "live_" + "4f9b7c2d8a6e1f03"
    (repo / "secret.env").write_text(f'API_KEY="{value}"\n', encoding="utf-8")
    manifest = repo / "deploy" / "runtime-paths.txt"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "secret.env\n", encoding="utf-8")
    commit = _commit(repo, "unsafe secret")
    stage = tmp_path / "secret-stage"

    with pytest.raises(StagingError, match="secret scan"):
        build_stage(repo, commit, stage)

    assert not stage.exists()


@pytest.mark.parametrize("kind", ["symlink", "traversal", "outside", "fifo"])
def test_archive_rejects_dangerous_or_manifest_external_member(
    tmp_path: Path,
    kind: str,
) -> None:
    archive_path = tmp_path / f"{kind}.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("app.py")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"ok"))
        if kind == "symlink":
            danger = tarfile.TarInfo("link")
            danger.type = tarfile.SYMTYPE
            danger.linkname = "app.py"
        elif kind == "traversal":
            danger = tarfile.TarInfo("../escape")
            danger.size = 1
        elif kind == "outside":
            danger = tarfile.TarInfo("outside.txt")
            danger.size = 1
        else:
            danger = tarfile.TarInfo("pipe")
            danger.type = tarfile.FIFOTYPE
        archive.addfile(danger, io.BytesIO(b"x") if danger.isreg() else None)
    stage = tmp_path / f"stage-{kind}"
    stage.mkdir()

    with pytest.raises(StagingError):
        extract_archive(archive_path, stage, ("app.py",))


def test_archive_enforces_file_and_byte_budgets(tmp_path: Path, monkeypatch) -> None:
    archive_path = tmp_path / "budget.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("app.py")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"ok"))
    monkeypatch.setattr(stage_module, "MAX_STAGE_BYTES", 1)
    stage = tmp_path / "budget-stage"
    stage.mkdir()

    with pytest.raises(StagingError, match="byte limit"):
        extract_archive(archive_path, stage, ("app.py",))


def test_manifest_rejects_unsafe_duplicate_and_unsorted_paths() -> None:
    with pytest.raises(StagingError):
        parse_manifest(b"# xiaoqing-deploy-manifest-v1\n../escape\n")
    with pytest.raises(StagingError):
        parse_manifest(b"# xiaoqing-deploy-manifest-v1\n-option\n")
    with pytest.raises(StagingError):
        parse_manifest(b"# xiaoqing-deploy-manifest-v1\napp.py\napp.py\n")
    with pytest.raises(StagingError, match="sorted"):
        parse_manifest(b"# xiaoqing-deploy-manifest-v1\nz.py\na.py\n")


def test_preview_plan_is_stable_and_apply_requires_exact_plan(tmp_path: Path) -> None:
    repo, commit, env, rsync_log = _make_sync_repo(tmp_path)
    first = _run_sync(repo, env, "--ref", commit)
    second = _run_sync(repo, env, "--ref", commit)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    plan = _plan_from_output(first.stdout)
    assert _plan_from_output(second.stdout) == plan

    mismatch = _run_sync(
        repo,
        env,
        "--apply",
        "--confirm-delete",
        "--ref",
        commit,
        "--expect-plan",
        "0" * 64,
    )
    assert mismatch.returncode != 0
    assert "does not match" in mismatch.stderr
    assert not rsync_log.exists()

    applied = _run_sync(
        repo,
        env,
        "--apply",
        "--confirm-delete",
        "--ref",
        commit,
        "--expect-plan",
        plan,
    )
    assert applied.returncode == 0, applied.stderr
    assert rsync_log.read_text(encoding="utf-8").splitlines() == ["apply"]


def test_plan_digest_changes_when_selected_commit_changes(tmp_path: Path) -> None:
    repo, first_commit, env, _log = _make_sync_repo(tmp_path)
    first = _run_sync(repo, env, "--ref", first_commit)
    (repo / "app.py").write_text("print('v2')\n", encoding="utf-8")
    second_commit = _commit(repo, "runtime v2")
    second = _run_sync(repo, env, "--ref", second_commit)

    assert first.returncode == second.returncode == 0
    assert _plan_from_output(first.stdout) != _plan_from_output(second.stdout)


@pytest.mark.parametrize("remote_root", ["/safe\n/evil", "/unsafe path", "/"])
def test_remote_root_response_is_revalidated_locally(tmp_path: Path, remote_root: str) -> None:
    repo, commit, env, _log = _make_sync_repo(tmp_path)
    env["FAKE_REMOTE_ROOT"] = remote_root

    result = _run_sync(repo, env, "--ref", commit)

    assert result.returncode != 0
    assert "remote root" in result.stderr or "remote target response" in result.stderr


def test_apply_rejects_short_ref_and_plan_before_external_commands() -> None:
    short_ref = subprocess.run(
        [
            "bash",
            SCRIPT.name,
            "--apply",
            "--confirm-delete",
            "--ref",
            "abc123",
            "--expect-plan",
            "0" * 64,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    short_plan = subprocess.run(
        [
            "bash",
            SCRIPT.name,
            "--apply",
            "--confirm-delete",
            "--ref",
            "0" * 40,
            "--expect-plan",
            "abcd",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert short_ref.returncode != 0
    assert "40-hex" in short_ref.stderr
    assert short_plan.returncode != 0
    assert "64-hex" in short_plan.stderr


def test_shell_contract_uses_immutable_stage_plan_and_remote_protections() -> None:
    content = SCRIPT.read_text(encoding="utf-8")
    assert "build_deploy_stage.py" in content
    assert '"$RSYNC_BIN" "${rsync_args[@]}" --dry-run "$stage_dir/"' in content
    assert '"$RSYNC_BIN" "${rsync_args[@]}" "$stage_dir/"' in content
    assert "Plan SHA256:" in content
    assert "--expect-plan" in content
    assert '"${#remote_lines[@]}" -eq 1' in content
    assert "resolved remote root is not a safe absolute path" in content
    for protected in (
        "/.git/***",
        "/.venv/***",
        "/venv/***",
        "/.local_archive/***",
        "/bot.log*",
        "/config/config.json",
        "/config/secrets.json",
        "/logs/***",
        "/data/***",
        "/plugins/*/data/***",
        "/plugins/*/cache/***",
        "/plugins/*/config.json",
        "/plugins/*/secrets.json",
        "/plugins/*/test_reports/***",
        "/plugins/pendo/test_tools/***",
        "/plugins/xiaoqing_chat/figures/***",
        "/plugins/arxiv_filter/best_model*/***",
        "/plugins/arxiv_filter/train_model/**/cache/***",
        "/plugins/arxiv_filter/train_model/arxiv_papers_with_abstract.csv",
    ):
        assert f"--filter='P {protected}'" in content

"""Regression tests for reproducible dependency installation."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import scripts.compile_locks as lock_compiler

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("3.10", "3.11", "3.12", "3.13")
PROFILES = ("runtime", "ci")
RUNTIME_FEATURE_PACKAGES = {
    "astroquery",
    "astropy",
    "feedparser",
    "ipykernel",
    "joblib",
    "jupyter-client",
    "pandas",
    "paramiko",
    "scikit-learn",
    "sentence-transformers",
    "torch",
    "tqdm",
    "transformers",
}
DEV_ONLY_PACKAGES = {
    "aioresponses",
    "black",
    "build",
    "mypy",
    "pip-audit",
    "pip-licenses",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-xdist",
    "ruff",
    "uv",
    "wheel",
}


def _lock(version: str, profile: str) -> Path:
    return ROOT / "requirements" / f"python-{version}-{profile}.lock"


def _package_names(content: str) -> set[str]:
    return {
        match.group(1).lower().replace("_", "-")
        for line in content.splitlines()
        if (match := re.match(r"^([A-Za-z0-9_.-]+)==", line))
    }


def test_every_supported_python_has_a_hashed_lock() -> None:
    for version in VERSIONS:
        for profile in PROFILES:
            content = _lock(version, profile).read_text(encoding="utf-8")
            requirement_lines = [
                line
                for line in content.splitlines()
                if line and not line[0].isspace() and not line.startswith("#")
            ]

            assert len(requirement_lines) >= 60
            assert all("==" in line for line in requirement_lines)
            assert content.count("--hash=sha256:") >= len(requirement_lines)
            assert "python scripts/compile_locks.py" in content


def test_lock_target_matrix_is_complete_unique_and_shared() -> None:
    targets = lock_compiler.LOCK_TARGETS

    assert len(targets) == 8
    assert len({target.filename for target in targets}) == len(targets)
    assert {(target.python_version, target.profile.name) for target in targets} == {
        (version, profile) for version in VERSIONS for profile in PROFILES
    }


def test_runtime_locks_cover_enabled_features_without_dev_tooling() -> None:
    for version in VERSIONS:
        runtime = _package_names(_lock(version, "runtime").read_text(encoding="utf-8"))

        assert RUNTIME_FEATURE_PACKAGES <= runtime
        assert runtime.isdisjoint(DEV_ONLY_PACKAGES)


def test_ci_locks_are_runtime_plus_development_tools() -> None:
    for version in VERSIONS:
        runtime = _package_names(_lock(version, "runtime").read_text(encoding="utf-8"))
        ci = _package_names(_lock(version, "ci").read_text(encoding="utf-8"))

        assert runtime <= ci
        assert DEV_ONLY_PACKAGES <= ci


def test_all_extra_means_complete_runtime_not_development_environment() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    all_requirements = pyproject["project"]["optional-dependencies"]["all"]

    assert all_requirements == ["xiaoqing[plugins,ml,web,jupyter,arxiv-ml,astro,ssh]"]


def test_ci_profile_pins_the_python_release_build_frontend() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = set(pyproject["project"]["optional-dependencies"]["dev"])

    assert {"build==1.5.1", "setuptools==83.0.0", "wheel==0.47.0"} <= dev
    for version in VERSIONS:
        ci = _package_names(_lock(version, "ci").read_text(encoding="utf-8"))
        assert {"build", "setuptools", "wheel"} <= ci


def test_ci_and_docker_enforce_hash_checking() -> None:
    ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compatibility_requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "requirements/python-${{ matrix.python-version }}-ci.lock" in ci
    assert "requirements/python-3.13-ci.lock" in ci
    assert "requirements/python-${{ matrix.python-version }}.lock" not in ci
    assert 'pip install --require-hashes -r "$CI_LOCK_FILE"' in ci
    assert 'pip-audit --require-hashes -r "$CI_LOCK_FILE"' in ci
    assert 'pip-audit --require-hashes -r "$RUNTIME_LOCK_FILE"' in ci
    assert "pip install --no-cache-dir --require-hashes" in dockerfile
    assert "requirements/python-3.13-runtime.lock" in dockerfile
    assert "python-3.13-ci.lock" not in dockerfile
    assert "-r requirements/python-3.13-runtime.lock" in compatibility_requirements
    assert "python-3.13-ci.lock" not in compatibility_requirements


def test_lock_refresh_workflow_isolates_generation_from_publication() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-locks.yml").read_text(encoding="utf-8")
    generate, publish = workflow.split("  publish:", maxsplit=1)

    assert "permissions: {}" in workflow.split("jobs:", maxsplit=1)[0]
    assert "  generate:" in generate
    assert "contents: read" in generate
    assert "contents: write" not in generate
    assert "pull-requests: write" not in generate
    assert "GH_TOKEN" not in generate
    assert "git push" not in generate
    assert "gh pr create" not in generate
    assert "python -m pip install uv" not in workflow
    assert "python -m pip install --require-hashes -r requirements/python-3.13-ci.lock" in generate
    assert generate.count("python -m pip install") == 1
    assert "python scripts/compile_locks.py --upgrade" in generate
    assert "python scripts/compile_locks.py --check" in generate
    assert generate.index("compile_locks.py --upgrade") < generate.index("compile_locks.py --check")

    assert "contents: write" in publish
    assert "pull-requests: write" in publish
    assert "setup-python" not in publish
    assert "pip install" not in publish
    assert "scripts/compile_locks.py" not in publish
    assert "artifact-ids: ${{ needs.generate.outputs.artifact_id }}" in publish
    assert "merge-multiple: true" in publish
    assert publish.count("GH_TOKEN:") == 1
    token_step = publish.split("GH_TOKEN:", maxsplit=1)[1]
    assert "gh auth setup-git" in token_step
    assert 'git push --force-with-lease="$lease" --set-upstream origin' in token_step
    assert re.search(r"gh pr create\b", token_step)
    assert "x-access-token" not in workflow


def test_lock_refresh_bundle_has_fixed_files_and_strict_shell_validation() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-locks.yml").read_text(encoding="utf-8")
    expected_locks = {
        f"python-{version}-{profile}.lock" for version in VERSIONS for profile in ("ci", "runtime")
    }

    for filename in expected_locks:
        assert filename in workflow
    assert "SOURCE_COMMIT" in workflow
    assert "SHA256SUMS" in workflow
    assert "Lock bundle must contain exactly 10 members" in workflow
    assert "SHA256SUMS must contain exactly nine records" in workflow
    assert "sha256sum --check --strict SHA256SUMS" in workflow
    assert "EXPECTED_MANIFEST_SHA256" in workflow
    assert "actual_manifest_sha256" in workflow
    assert "SOURCE_SHA: ${{ github.sha }}" in workflow
    assert "SOURCE_COMMIT does not match the workflow source commit" in workflow
    assert '[[ ! -f "$member" || -L "$member" ]]' in workflow
    assert workflow.count("size < 1 || size > 16777216") == 2
    assert workflow.count("bundle_total_size > 134217728") == 2
    assert "Checksum manifest repeats a file" in workflow
    assert "Checksum manifest omits a required file" in workflow
    assert "install -m 0644" in workflow
    assert workflow.count("ref: ${{ github.sha }}") == 2
    assert "Publish checkout does not match the workflow source commit" in workflow


def test_ci_mandatorily_checks_all_lock_freshness() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    assert "Verify dependency lock freshness" in workflow
    assert "python scripts/compile_locks.py --check" in workflow
    assert workflow.index("pip install --require-hashes") < workflow.index(
        "compile_locks.py --check"
    )
    assert workflow.index("compile_locks.py --check") < workflow.index(
        "Verify full test collection"
    )


def _target(profile: str = "runtime") -> lock_compiler.LockTarget:
    return next(target for target in lock_compiler.LOCK_TARGETS if target.profile.name == profile)


def _pyproject_variant(tmp_path: Path, old: str, new: str) -> Path:
    content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert content.count(old) == 1
    destination = tmp_path / "pyproject.toml"
    destination.write_text(content.replace(old, new), encoding="utf-8")
    return destination


def test_lock_digest_tracks_base_marker_and_selected_extra_semantics(tmp_path: Path) -> None:
    runtime = _target("runtime")
    baseline = lock_compiler.input_digest(runtime)

    base_change = _pyproject_variant(tmp_path, "aiohttp>=3.8.0", "aiohttp>=3.9.0")
    assert lock_compiler.input_digest(runtime, pyproject_path=base_change) != baseline

    extra_change = _pyproject_variant(
        tmp_path,
        "jupyter-client>=8.6.0",
        "jupyter-client>=8.7.0; python_version >= '3.11'",
    )
    assert lock_compiler.input_digest(runtime, pyproject_path=extra_change) != baseline


def test_lock_digest_ignores_toml_comments_and_unselected_dev_extra(tmp_path: Path) -> None:
    runtime = _target("runtime")
    baseline = lock_compiler.input_digest(runtime)
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    comment_only = tmp_path / "comment.toml"
    comment_only.write_text("# review-only comment\n" + source, encoding="utf-8")
    assert lock_compiler.input_digest(runtime, pyproject_path=comment_only) == baseline

    dev_only = _pyproject_variant(tmp_path, "pytest>=7.0.0", "pytest>=8.0.0")
    assert lock_compiler.input_digest(runtime, pyproject_path=dev_only) == baseline
    assert lock_compiler.input_digest(_target("ci"), pyproject_path=dev_only) != (
        lock_compiler.input_digest(_target("ci"))
    )


def test_all_committed_locks_have_exact_current_freshness_metadata() -> None:
    for target in lock_compiler.LOCK_TARGETS:
        content = target.path.read_text(encoding="utf-8")
        digest = lock_compiler.input_digest(target)

        assert lock_compiler.metadata_problems(content, target, digest) == []


def test_metadata_validation_rejects_missing_duplicate_and_wrong_target() -> None:
    target = _target()
    digest = lock_compiler.input_digest(target)
    valid = lock_compiler.stamp_lock("safe==1\n", target, digest)

    assert lock_compiler.metadata_problems(valid, target, digest) == []
    duplicate = valid.replace(
        "# xiaoqing-lock-schema: 1\n",
        "# xiaoqing-lock-schema: 1\n# xiaoqing-lock-schema: 1\n",
    )
    assert any(
        "occurs 2 times" in problem
        for problem in lock_compiler.metadata_problems(duplicate, target, digest)
    )
    missing = valid.replace("# xiaoqing-lock-uv: 0.11.28\n", "")
    assert any(
        "occurs 0 times" in problem
        for problem in lock_compiler.metadata_problems(missing, target, digest)
    )
    wrong = valid.replace(target.target_id, "python=9.99;profile=runtime")
    assert any(
        "target" in problem for problem in lock_compiler.metadata_problems(wrong, target, digest)
    )


def test_check_reuses_existing_pins_and_never_mutates_committed_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    monkeypatch.setattr(lock_compiler, "REQUIREMENTS_DIR", requirements)
    body = "# generated\nkept-pin==1.2.3 \\\n+    --hash=sha256:00\n"
    destination = target.path
    destination.write_text(
        lock_compiler.stamp_lock(body, target, lock_compiler.input_digest(target)),
        encoding="utf-8",
    )
    os.utime(destination, ns=(1_700_000_000_000_000_000,) * 2)
    before = destination.read_bytes()
    before_mtime = destination.stat().st_mtime_ns

    def fake_compile(
        received: lock_compiler.LockTarget,
        output: Path,
        *,
        upgrade: bool,
    ) -> None:
        assert received == target
        assert upgrade is False
        assert "kept-pin==1.2.3" in output.read_text(encoding="utf-8")
        output.write_text(body, encoding="utf-8")

    monkeypatch.setattr(lock_compiler, "_compile_to", fake_compile)

    assert lock_compiler.check_locks([target]) == []
    assert destination.read_bytes() == before
    assert destination.stat().st_mtime_ns == before_mtime


def test_check_detects_tampered_body_even_with_a_valid_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    monkeypatch.setattr(lock_compiler, "REQUIREMENTS_DIR", requirements)
    generated_body = "safe==1 \\\n+    --hash=sha256:00\n"
    digest = lock_compiler.input_digest(target)
    target.path.write_text(
        lock_compiler.stamp_lock(generated_body + "tampered==9\n", target, digest),
        encoding="utf-8",
    )

    def fake_compile(
        _target_value: lock_compiler.LockTarget,
        output: Path,
        *,
        upgrade: bool,
    ) -> None:
        assert upgrade is False
        assert "tampered==9" in output.read_text(encoding="utf-8")
        output.write_text(generated_body, encoding="utf-8")

    monkeypatch.setattr(lock_compiler, "_compile_to", fake_compile)

    problems = lock_compiler.check_locks([target])

    assert len(problems) == 1
    assert "compiled content is stale" in problems[0]
    assert "tampered==9" in problems[0]


def test_check_and_upgrade_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        lock_compiler._parser().parse_args(["--check", "--upgrade"])

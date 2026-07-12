"""Regression tests for CI test-suite coverage."""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def test_ci_runs_full_suite_without_sparse_marker_filters() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '-m "unit"' not in workflow
    assert '-m "plugin"' not in workflow
    assert (
        "python -m pytest tests/ --cov=core --cov=plugins --cov-branch --cov-report=xml --cov-report=json:coverage.json"
        in workflow
    )


def test_ci_enforces_collection_floor_before_running_tests() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    guard = "python scripts/check_test_collection.py --minimum 2200"
    full_suite = "python -m pytest tests/"
    assert guard in workflow
    assert workflow.index(guard) < workflow.index(full_suite)


def test_ci_has_separate_privileged_plugin_branch_coverage_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Enforce privileged-plugin branch coverage floor" in workflow
    for package in ("codex", "shell", "jupyter", "qingssh", "minecraft"):
        assert f"--cov=plugins/{package}" in workflow
    assert "--cov-branch" in workflow
    assert "--cov-fail-under=45" in workflow


def test_ci_enforces_per_package_line_and_branch_floors() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "check_coverage_floors.py --coverage-json coverage.json" in workflow
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floors = config["tool"]["xiaoqing"]["coverage_floors"]
    for package in ("core", "plugins.codex", "plugins.qingssh", "plugins.pendo"):
        assert floors[package]["line"] > 0
        assert floors[package]["branch"] > 0


def test_ci_requires_python_313_with_release_smoke_checks() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'python-version: ["3.10", "3.11", "3.12", "3.13"]' in workflow
    assert "if: matrix.python-version == '3.13'" in workflow
    assert "Python 3.13 isolated wheel and sdist release smoke" in workflow
    assert "python scripts/verify_python_release.py" in workflow
    assert "xiaoqing-wheel-smoke" not in workflow
    assert "import plugins.pendo.main, plugins.xiaoqing_chat.main" not in workflow

    verifier = (ROOT / "scripts" / "verify_python_release.py").read_text(encoding="utf-8")
    assert '"-m",\n            "build",\n            "--no-isolation"' in verifier
    assert '"-I", str(probe_script), str(spec_path)' in verifier
    assert 'kind="wheel"' in verifier
    assert 'kind="sdist"' in verifier


def test_ci_blocks_new_ruff_diagnostics_without_hiding_legacy_debt() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    checker = ROOT / "scripts" / "check_ruff_changed.py"

    assert "lint-changed-python:" in workflow
    assert "fetch-depth: 0" in workflow
    assert 'python scripts/check_ruff_changed.py --base "$BASE_SHA"' in workflow
    checker_source = checker.read_text(encoding="utf-8")
    assert '"--unified=0"' in checker_source
    assert '"--output-format=json"' in checker_source
    assert '"--exit-zero"' in checker_source
    assert "parse_added_ranges" in checker_source
    assert '"--diff"' not in checker_source


def test_ci_has_independent_fail_closed_docker_release_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    docker_job = workflow.split("  docker-release-smoke:", maxsplit=1)[1]

    assert "    needs: test" in docker_job
    assert 'python-version: "3.13"' in docker_job
    assert 'build_root="$(mktemp -d)"' in docker_job
    assert 'python -m pip wheel . --no-deps --wheel-dir "$build_root/dist"' in docker_job
    assert "python scripts/build_docker_context.py" in docker_job
    assert "python scripts/verify_docker_release.py" in docker_job
    assert "--require-docker" in docker_job

    wheel = docker_job.index("python -m pip wheel")
    context = docker_job.index("python scripts/build_docker_context.py")
    verification = docker_job.index("python scripts/verify_docker_release.py")
    assert wheel < context < verification


def test_ci_does_not_export_docker_outputs_before_security_verification() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    docker_job = workflow.split("  docker-release-smoke:", maxsplit=1)[1]
    before_verification = docker_job.split("python scripts/verify_docker_release.py", maxsplit=1)[0]

    for forbidden in (
        "docker push",
        "docker/build-push-action",
        "actions/upload-artifact",
        "--cache-to",
        "--cache-from",
    ):
        assert forbidden not in before_verification

"""Regression tests for CI test-suite coverage."""

import re
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
LOCK_WORKFLOW = ROOT / ".github" / "workflows" / "dependency-locks.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
ACTION_SHAS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "actions/setup-python": "7f4fc3e22c37d6ff65e88745f38bd3157c663f7c",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "codecov/codecov-action": "ab904c41d6ece82784817410c45d8b8c02684457",
}


def _action_references(workflow: str) -> list[tuple[str, str]]:
    return re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", workflow, flags=re.MULTILINE)


def _workflow_document(path: Path) -> dict[str, object]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def _workflow_steps(document: dict[str, object]) -> list[dict[str, object]]:
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    steps: list[dict[str, object]] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        job_steps = job["steps"]
        assert isinstance(job_steps, list)
        assert all(isinstance(step, dict) for step in job_steps)
        steps.extend(job_steps)
    return steps


def test_workflow_actions_are_allowlisted_and_pinned_to_full_commit_shas() -> None:
    tests_workflow = WORKFLOW.read_text(encoding="utf-8")
    lock_workflow = LOCK_WORKFLOW.read_text(encoding="utf-8")
    references = _action_references(tests_workflow + "\n" + lock_workflow)

    assert references
    for repository, revision in references:
        assert repository in ACTION_SHAS
        assert revision == ACTION_SHAS[repository]
        assert re.fullmatch(r"[0-9a-f]{40}", revision)

    assert (
        _action_references(tests_workflow).count(
            ("actions/checkout", ACTION_SHAS["actions/checkout"])
        )
        == 3
    )
    assert (
        _action_references(tests_workflow).count(
            ("actions/setup-python", ACTION_SHAS["actions/setup-python"])
        )
        == 3
    )
    assert (
        _action_references(lock_workflow).count(
            ("actions/checkout", ACTION_SHAS["actions/checkout"])
        )
        == 2
    )

    for document in (_workflow_document(WORKFLOW), _workflow_document(LOCK_WORKFLOW)):
        for step in _workflow_steps(document):
            reference = step.get("uses")
            if reference is None:
                continue
            assert isinstance(reference, str)
            repository, separator, revision = reference.partition("@")
            assert separator == "@"
            assert repository in ACTION_SHAS
            assert revision == ACTION_SHAS[repository]
            assert re.fullmatch(r"[0-9a-f]{40}", revision)


def test_tests_workflow_has_read_only_permissions_and_no_persisted_credentials() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    header = workflow.split("jobs:", maxsplit=1)[0]

    assert "permissions:\n  contents: read" in header
    assert "contents: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert workflow.count("persist-credentials: false") == 3
    assert workflow.count("uses: actions/checkout@") == 3
    assert "pip install --upgrade" not in workflow

    document = _workflow_document(WORKFLOW)
    assert document["permissions"] == {"contents": "read"}
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    assert all("permissions" not in job for job in jobs.values())
    checkout_steps = [
        step
        for step in _workflow_steps(document)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == 3
    for step in checkout_steps:
        options = step.get("with")
        assert isinstance(options, dict)
        assert options["persist-credentials"] == "false"


def test_lock_workflow_permissions_and_publish_execution_are_structurally_isolated() -> None:
    document = _workflow_document(LOCK_WORKFLOW)
    assert document["permissions"] == {}
    assert document["concurrency"] == {
        "group": "dependency-lock-refresh-${{ github.repository }}",
        "cancel-in-progress": "false",
    }
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"generate", "publish"}
    generate = jobs["generate"]
    publish = jobs["publish"]
    assert isinstance(generate, dict)
    assert isinstance(publish, dict)
    assert generate["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }

    generate_steps = generate["steps"]
    publish_steps = publish["steps"]
    assert isinstance(generate_steps, list)
    assert isinstance(publish_steps, list)
    assert [str(step["uses"]).partition("@")[0] for step in generate_steps if "uses" in step] == [
        "actions/checkout",
        "actions/setup-python",
        "actions/upload-artifact",
    ]
    generate_setup = next(
        step
        for step in generate_steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert generate_setup["with"] == {
        "python-version": "3.13",
        "cache": "pip",
        "cache-dependency-path": "requirements/python-3.13-ci.lock",
    }
    assert [str(step["uses"]).partition("@")[0] for step in publish_steps if "uses" in step] == [
        "actions/checkout",
        "actions/download-artifact",
    ]

    checkout_steps = [
        step
        for step in _workflow_steps(document)
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == 2
    for step in checkout_steps:
        options = step.get("with")
        assert isinstance(options, dict)
        assert options == {
            "ref": "${{ github.sha }}",
            "persist-credentials": "false",
        }

    run_steps = [step for step in publish_steps if "run" in step]
    assert len(run_steps) == 2
    combined_run = "\n".join(str(step["run"]) for step in run_steps)
    assert not re.search(
        r"(?m)^\s*(?:python(?:3)?|pip(?:3)?|uv)(?:\s|$)",
        combined_run,
    )
    assert "scripts/" not in combined_run
    for step in publish_steps[:-1]:
        serialized = repr(step)
        assert "GH_TOKEN" not in serialized
        assert "github.token" not in serialized
    token_steps = [step for step in publish_steps if "GH_TOKEN" in step.get("env", {})]
    assert token_steps == [publish_steps[-1]]
    assert run_steps[-1] is publish_steps[-1]
    assert publish_steps[-1]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    }
    final_run = str(publish_steps[-1]["run"])
    assert 'branch="automation/dependency-locks"' in final_run
    assert "GITHUB_RUN_ID" not in final_run
    assert 'git ls-remote --heads origin "$remote_ref"' in final_run
    assert 'lease="$remote_ref:"' in final_run
    assert 'lease="$remote_ref:$expected_remote"' in final_run
    assert 'git push --force-with-lease="$lease"' in final_run
    push_lines = re.findall(r"(?m)^\s*git\s+push\b[^\n]*$", final_run)
    assert len(push_lines) == 1
    assert re.fullmatch(
        r'\s*git\s+push\s+--force-with-lease="\$lease"\s+'
        r'--set-upstream\s+origin\s+"\$branch"\s*',
        push_lines[0],
    )
    assert '--base "$DEFAULT_BRANCH"' in final_run
    assert '--head "$branch"' in final_run
    assert "--limit 100" in final_run
    assert "--json number,isCrossRepository" in final_run
    assert "select(.isCrossRepository == false)" in final_run
    assert "Multiple same-repository dependency-lock PRs are open" in final_run
    assert final_run.index("gh auth setup-git") < final_run.index("git ls-remote")
    assert final_run.index("git ls-remote") < final_run.index("git push --force-with-lease")
    assert final_run.index("git push --force-with-lease") < final_run.index("gh pr list")

    default_branch_steps = [
        step
        for step in generate_steps
        if step.get("name") == "Require the repository default branch"
    ]
    assert len(default_branch_steps) == 1
    assert default_branch_steps[0]["env"] == {
        "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "SOURCE_REF": "${{ github.ref_name }}",
    }
    assert '[[ "$SOURCE_REF" != "$DEFAULT_BRANCH" ]]' in str(default_branch_steps[0]["run"])


def test_ci_audits_each_matrix_lock_and_binds_pip_caches() -> None:
    document = _workflow_document(WORKFLOW)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)

    lint_job = jobs["lint-changed-python"]
    test_job = jobs["test"]
    assert isinstance(lint_job, dict)
    assert isinstance(test_job, dict)
    lint_setup = next(
        step for step in lint_job["steps"] if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    matrix_setup = next(
        step for step in test_job["steps"] if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert lint_setup["with"] == {
        "python-version": "3.13",
        "cache": "pip",
        "cache-dependency-path": "requirements/python-3.13-ci.lock",
    }
    assert set(matrix_setup["with"]) == {
        "python-version",
        "cache",
        "cache-dependency-path",
    }
    assert matrix_setup["with"]["python-version"] == "${{ matrix.python-version }}"
    assert matrix_setup["with"]["cache"] == "pip"
    assert str(matrix_setup["with"]["cache-dependency-path"]).splitlines() == [
        "requirements/python-${{ matrix.python-version }}-ci.lock",
        "requirements/python-${{ matrix.python-version }}-runtime.lock",
    ]

    test_steps = test_job["steps"]
    install_index = next(
        index for index, step in enumerate(test_steps) if step.get("name") == "Install dependencies"
    )
    audit_index = next(
        index for index, step in enumerate(test_steps) if step.get("name") == "Audit locked dependencies"
    )
    assert audit_index == install_index + 1
    install = test_steps[install_index]
    audit = test_steps[audit_index]
    assert install["env"] == audit["env"]
    assert audit["env"] == {
        "CI_LOCK_FILE": "requirements/python-${{ matrix.python-version }}-ci.lock",
        "RUNTIME_LOCK_FILE": "requirements/python-${{ matrix.python-version }}-runtime.lock",
    }
    assert str(install["run"]).strip() == (
        'python -m pip install --require-hashes -r "$CI_LOCK_FILE"'
    )
    audit_commands = str(audit["run"]).strip().splitlines()
    assert audit_commands == [
        'pip-audit --require-hashes -r "$CI_LOCK_FILE"',
        'pip-audit --require-hashes -r "$RUNTIME_LOCK_FILE"',
    ]
    versions = test_job["strategy"]["matrix"]["python-version"]
    assert len(versions) * len(audit_commands) == 8
    assert "if" not in install
    assert "continue-on-error" not in install
    assert "if" not in audit
    assert "continue-on-error" not in audit

    licenses = next(
        step for step in test_job["steps"] if step.get("name") == "Report dependency licenses"
    )
    assert licenses["if"] == "matrix.python-version == '3.13'"
    assert str(licenses["run"]).strip() == "pip-licenses --format=markdown"


def test_dependabot_allows_python_patch_updates_while_ignoring_major_minor() -> None:
    document = _workflow_document(DEPENDABOT)
    assert document["version"] == "2"
    updates = document["updates"]
    assert isinstance(updates, list)
    assert len(updates) == 1
    docker = updates[0]
    assert isinstance(docker, dict)
    assert set(docker) == {
        "package-ecosystem",
        "directory",
        "schedule",
        "open-pull-requests-limit",
        "ignore",
    }
    assert docker["package-ecosystem"] == "docker"
    assert docker["directory"] == "/"
    assert int(docker["open-pull-requests-limit"]) > 0
    assert "allow" not in docker
    schedule = docker["schedule"]
    assert isinstance(schedule, dict)
    assert schedule["interval"] == "weekly"
    ignores = docker["ignore"]
    assert isinstance(ignores, list)
    assert ignores == [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]
    ignored_updates = set(ignores[0]["update-types"])
    assert "version-update:semver-patch" not in ignored_updates


def test_ci_runs_full_suite_without_sparse_marker_filters() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert '-m "unit"' not in workflow
    assert '-m "plugin"' not in workflow
    assert (
        "python -m pytest tests/ --cov=core --cov=plugins --cov-branch --cov-report=xml --cov-report=json:coverage.json"
        in workflow
    )


def test_ci_enforces_exact_collection_manifest_before_running_tests() -> None:
    document = _workflow_document(WORKFLOW)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    test_job = jobs["test"]
    assert isinstance(test_job, dict)
    strategy = test_job["strategy"]
    assert isinstance(strategy, dict)
    assert set(strategy) == {"fail-fast", "matrix"}
    assert strategy["fail-fast"] == "false"
    matrix = strategy["matrix"]
    assert isinstance(matrix, dict)
    assert set(matrix) == {"python-version"}
    assert matrix["python-version"] == ["3.10", "3.11", "3.12", "3.13"]
    assert "if" not in test_job
    assert "continue-on-error" not in test_job

    steps = test_job["steps"]
    assert isinstance(steps, list)
    ledger_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Verify code review ledger"
    ]
    assert len(ledger_steps) == 1
    ledger_step = ledger_steps[0]
    ledger_guard = "python scripts/check_code_review.py --require-complete"
    assert str(ledger_step["run"]).strip() == ledger_guard
    assert "if" not in ledger_step
    assert "continue-on-error" not in ledger_step
    assert "env" not in ledger_step

    collection_step = next(
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Verify full test collection"
    )
    guard = "python scripts/check_test_collection.py --manifest tests/test_collection_manifest.toml"
    assert str(collection_step["run"]).strip() == guard
    assert "if" not in collection_step
    assert "continue-on-error" not in collection_step
    assert "env" not in collection_step
    assert not any(token in guard for token in ("--minimum", " -k ", " -m ", "--ignore"))
    ledger_index = steps.index(ledger_step)
    assert ledger_index < steps.index(collection_step)
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        run = str(step.get("run", ""))
        if "pytest" in run or "check_test_collection.py" in run:
            assert ledger_index < index

    workflow = WORKFLOW.read_text(encoding="utf-8")
    full_suite = "python -m pytest tests/"
    assert "--minimum" not in workflow
    assert workflow.count(ledger_guard) == 1
    assert workflow.index(ledger_guard) < workflow.index(guard)
    assert workflow.index(guard) < workflow.index(full_suite)

    manifest = tomllib.loads(
        (ROOT / "tests" / "test_collection_manifest.toml").read_text(encoding="utf-8")
    )
    ledger_module = manifest["modules"]["tests/test_code_review.py"]
    assert ledger_module["group"] == "tooling"
    assert ledger_module["items"] == 51


def test_ci_has_separate_privileged_plugin_branch_coverage_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Measure privileged plugins independently" in workflow
    assert "Enforce per-plugin privileged coverage floors" in workflow
    privileged_gate = workflow.split(
        "    - name: Measure privileged plugins independently", maxsplit=1
    )[1].split("    - name: Python 3.13 isolated wheel", maxsplit=1)[0]
    assert "--cov-fail-under=45" not in privileged_gate
    assert "--cov-fail-under=0" in privileged_gate
    assert "--cov-report=json:coverage-privileged.json" in privileged_gate
    for package in ("codex", "shell", "jupyter", "qingssh", "minecraft"):
        assert f"tests/plugins/test_{package}*.py" in privileged_gate
        assert f"--cov=plugins/{package}" in privileged_gate
        assert f"--package plugins.{package}" in privileged_gate
    assert "tests/plugins/test_cr220_shell_jupyter_log_privacy.py" in privileged_gate
    assert "tests/plugins/test_cr220_sensitive_audit.py" in privileged_gate
    assert "--cov-branch" in privileged_gate
    assert privileged_gate.count("--package plugins.") == 5
    assert privileged_gate.count("coverage-privileged.json") == 2


def test_ci_enforces_per_package_line_and_branch_floors() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "check_coverage_floors.py --coverage-json coverage.json" in workflow
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floors = config["tool"]["xiaoqing"]["coverage_floors"]
    for package in ("core", "plugins.codex", "plugins.qingssh", "plugins.pendo"):
        assert floors[package]["line"] > 0
        assert floors[package]["branch"] > 0

    assert floors["plugins.codex"] == {"line": 70, "branch": 55}
    assert floors["plugins.shell"] == {"line": 75, "branch": 60}
    assert floors["plugins.jupyter"] == {"line": 60, "branch": 40}
    assert floors["plugins.qingssh"] == {"line": 45, "branch": 25}
    assert floors["plugins.minecraft"] == {"line": 70, "branch": 45}


def test_ci_has_python313_strict_pendo_resource_lifecycle_gate() -> None:
    document = _workflow_document(WORKFLOW)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    test_job = jobs["test"]
    assert isinstance(test_job, dict)
    matching = [
        step
        for step in test_job["steps"]
        if step.get("name") == "Enforce strict Pendo SQLite resource lifecycle"
    ]

    assert len(matching) == 1
    step = matching[0]
    assert step["if"] == "matrix.python-version == '3.13'"
    command = str(step["run"])
    assert "python -m pytest tests/plugins/test_pendo*.py" in command
    assert "-W error::ResourceWarning" in command
    assert "-W error::pytest.PytestUnraisableExceptionWarning" in command


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

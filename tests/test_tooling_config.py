from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_and_coverage_share_one_supported_configuration_file() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert not (ROOT / "pytest.ini").exists()
    assert config["tool"]["pytest"]["ini_options"]["addopts"] == "-v --strict-markers --tb=short"
    assert config["tool"]["coverage"]["run"]["branch"] is True
    assert config["tool"]["coverage"]["report"]["fail_under"] == 50


def test_all_python_310_toml_entrypoints_have_tomli_fallback() -> None:
    """Keep every CI-time TOML reader importable on the supported Python 3.10."""
    for relative_path in (
        "tests/test_ci_workflow.py",
        "tests/test_tooling_config.py",
        "scripts/check_coverage_floors.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "except ModuleNotFoundError:" in source, relative_path
        assert "import tomli as tomllib" in source, relative_path

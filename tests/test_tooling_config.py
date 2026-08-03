import re
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
    assert config["tool"]["coverage"]["report"]["fail_under"] == 75


def test_removed_coverage_floor_checker_leaves_no_dead_configuration() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "coverage_floors" not in config["tool"].get("xiaoqing", {})


def test_all_python_310_toml_entrypoints_have_tomli_fallback() -> None:
    """Keep every CI-time TOML reader importable on the supported Python 3.10."""
    for relative_path in (
        "tests/test_tooling_config.py",
        "tests/test_dependency_extras.py",
        "tests/test_docs_metadata.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "except ModuleNotFoundError:" in source, relative_path
        assert "import tomli as tomllib" in source, relative_path


def test_ruff_enforces_project_complexity_ceiling() -> None:
    """复杂度约束必须属于默认门禁，不能只靠人工临时检查。"""

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ruff = config["tool"]["ruff"]["lint"]

    assert "C901" in ruff["select"]
    assert ruff["mccabe"]["max-complexity"] == 30


def test_mypy_checks_runtime_trees_and_caps_core_debt_by_lines() -> None:
    """Core 豁免按真实物理行数设上限，不能用小文件数掩盖大面积盲区。"""

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy = config["tool"]["mypy"]
    core_debt: dict[str, int] = {}
    for pattern in mypy["exclude"]:
        match = re.fullmatch(r"\^core/([A-Za-z0-9_/-]+)\\\.py\$", pattern)
        if match is None:
            continue
        relative_path = f"core/{match.group(1)}.py"
        path = ROOT / relative_path
        assert path.is_file(), f"mypy core 豁免指向不存在文件: {relative_path}"
        core_debt[relative_path] = len(path.read_text(encoding="utf-8").splitlines())

    assert set(mypy["files"]) == {"core", "plugins"}
    assert {
        "core/durable_fanout.py",
        "core/logging_config.py",
        "core/safe_http.py",
    }.isdisjoint(core_debt)
    # Ratchet: core type debt may only shrink; never raise this ceiling to admit growth.
    assert sum(core_debt.values()) <= 8_140


def test_gitattributes_define_cross_platform_text_policy() -> None:
    """源码换行必须由仓库声明，避免 Windows 提交产生整文件噪声。"""

    attributes = set((ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines())

    assert "* text=auto" in attributes
    assert "*.py text eol=lf" in attributes
    assert "*.sh text eol=lf" in attributes
    assert "*.ps1 text eol=lf" in attributes
    assert "*.cmd text eol=crlf" in attributes
    assert "*.zip binary" in attributes

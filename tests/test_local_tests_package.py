from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_TESTS_INIT = PROJECT_ROOT / "tests" / "__init__.py"


def test_subprocess_imports_the_local_tests_package() -> None:
    """默认 Python 子进程必须把 tests 固定解析到当前仓库。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib, tests, tests.helpers; "
            "print(pathlib.Path(tests.__file__).resolve()); "
            "print(pathlib.Path(tests.helpers.__file__).resolve())",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    package_path, helpers_path = map(Path, result.stdout.splitlines())
    assert package_path == LOCAL_TESTS_INIT
    assert helpers_path.is_relative_to(PROJECT_ROOT / "tests" / "helpers")

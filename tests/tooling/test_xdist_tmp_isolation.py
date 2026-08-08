from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize("case", range(8))
def test_project_tmp_root_isolated_across_xdist_workers(
    project_tmp_root: Path,
    worker_id: str,
    case: int,
) -> None:
    """两个真实 worker 必须能同时保有各自的临时文件。"""

    if worker_id == "master":
        pytest.skip("parallel smoke requires pytest -n 2 or more")

    assert project_tmp_root.name == worker_id
    sentinel = project_tmp_root / f"ready-{case}.txt"
    sentinel.write_text(worker_id, encoding="utf-8")

    assert sentinel.read_text(encoding="utf-8") == worker_id

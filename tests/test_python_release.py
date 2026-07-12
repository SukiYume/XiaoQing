from __future__ import annotations

import io
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import verify_python_release as release

ROOT = Path(__file__).resolve().parents[1]


def _inventory() -> release.RuntimeInventory:
    return release.RuntimeInventory(
        files=(
            "main.py",
            "plugins/demo/data.json",
            "plugins/demo/main.py",
            "plugins/demo/plugin.json",
        ),
        plugins=(("demo", "main.py"),),
        resources=("plugins/demo/data.json",),
    )


def _wheel(
    path: Path,
    *,
    omit: str | None = None,
    extra: tuple[str | zipfile.ZipInfo, bytes] | None = None,
    metadata_name: str = "xiaoqing",
    metadata_version: str = "4.1.0",
) -> Path:
    files = {
        "main.py": b"def cli(): pass\n",
        "plugins/demo/main.py": b"def init(): pass\n",
        "plugins/demo/plugin.json": b'{"name":"demo","entry":"main.py"}\n',
        "plugins/demo/data.json": b"{}\n",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            if name != omit:
                archive.writestr(name, payload)
        archive.writestr(
            "xiaoqing-4.1.0.dist-info/METADATA",
            (f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: {metadata_version}\n\n"),
        )
        if extra is not None:
            archive.writestr(*extra)
    return path


def _sdist(
    path: Path,
    *,
    omit: str | None = None,
    extra: tarfile.TarInfo | None = None,
    metadata_name: str = "xiaoqing",
    metadata_version: str = "4.1.0",
) -> Path:
    root = "xiaoqing-4.1.0"
    files = {
        "main.py": b"def cli(): pass\n",
        "plugins/demo/main.py": b"def init(): pass\n",
        "plugins/demo/plugin.json": b'{"name":"demo","entry":"main.py"}\n',
        "plugins/demo/data.json": b"{}\n",
        "PKG-INFO": (
            f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: {metadata_version}\n\n"
        ).encode(),
    }
    with tarfile.open(path, "w:gz") as archive:
        for relative, payload in files.items():
            if relative == omit:
                continue
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x") if extra.isreg() else None)
    return path


def test_current_runtime_inventory_is_exact_and_git_backed() -> None:
    inventory = release.build_runtime_inventory(ROOT)

    assert len(inventory.files) == 393
    assert len(inventory.plugins) == 29
    assert len(inventory.resources) == 39
    assert inventory.plugin_map["arxiv_filter"] == "main.py"
    assert "plugins/dict/assets/manifest.json" in inventory.resources
    assert "plugins/pendo/web/static/index.html" in inventory.resources
    assert "plugins/xiaoqing_chat/media/qq_face_builtin_catalog.json" in inventory.resources
    assert not any("/data/" in path for path in inventory.resources)


def test_wheel_inspection_accepts_exact_runtime_inventory(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "xiaoqing-4.1.0-py3-none-any.whl")

    release.inspect_wheel(
        wheel,
        project_name="xiaoqing",
        project_version="4.1.0",
        inventory=_inventory(),
    )


@pytest.mark.parametrize("missing", ["plugins/demo/main.py", "plugins/demo/data.json"])
def test_wheel_inspection_rejects_missing_module_or_resource(
    tmp_path: Path,
    missing: str,
) -> None:
    wheel = _wheel(tmp_path / "xiaoqing-4.1.0-py3-none-any.whl", omit=missing)

    with pytest.raises(release.ReleaseVerificationError, match="inventory mismatch"):
        release.inspect_wheel(
            wheel,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_wheel_inspection_rejects_unknown_source_file(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "xiaoqing-4.1.0-py3-none-any.whl",
        extra=("plugins/demo/untracked.py", b"pass\n"),
    )

    with pytest.raises(release.ReleaseVerificationError, match="extra="):
        release.inspect_wheel(
            wheel,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_wheel_inspection_rejects_symlink_and_path_escape(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("plugins/demo/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    linked = _wheel(
        tmp_path / "xiaoqing-4.1.0-py3-none-any.whl",
        extra=(link, b"../../outside"),
    )
    with pytest.raises(release.ReleaseVerificationError, match="symbolic link"):
        release.inspect_wheel(
            linked,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )

    escaped = _wheel(
        tmp_path / "xiaoqing-4.1.0-py3-none-linux_x86_64.whl",
        extra=("../outside.py", b"pass\n"),
    )
    with pytest.raises(release.ReleaseVerificationError, match="unsafe path"):
        release.inspect_wheel(
            escaped,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_wheel_identity_must_match_pyproject(tmp_path: Path) -> None:
    wrong_metadata = _wheel(
        tmp_path / "xiaoqing-4.1.0-py3-none-any.whl",
        metadata_version="9.9.9",
    )
    with pytest.raises(release.ReleaseVerificationError, match="METADATA Version"):
        release.inspect_wheel(
            wrong_metadata,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_sdist_inspection_accepts_required_runtime_inventory(tmp_path: Path) -> None:
    sdist = _sdist(tmp_path / "xiaoqing-4.1.0.tar.gz")

    release.inspect_sdist(
        sdist,
        project_name="xiaoqing",
        project_version="4.1.0",
        inventory=_inventory(),
    )


def test_sdist_inspection_rejects_missing_resource(tmp_path: Path) -> None:
    sdist = _sdist(tmp_path / "xiaoqing-4.1.0.tar.gz", omit="plugins/demo/data.json")

    with pytest.raises(release.ReleaseVerificationError, match="missing runtime files"):
        release.inspect_sdist(
            sdist,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_sdist_inspection_rejects_link_and_wrong_identity(tmp_path: Path) -> None:
    link = tarfile.TarInfo("xiaoqing-4.1.0/plugins/demo/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    linked = _sdist(tmp_path / "xiaoqing-4.1.0.tar.gz", extra=link)
    with pytest.raises(release.ReleaseVerificationError, match="non-regular member"):
        release.inspect_sdist(
            linked,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )

    wrong = _sdist(
        tmp_path / "xiaoqing-4.1.0-wrong.tar.gz",
        metadata_name="other",
    )
    with pytest.raises(release.ReleaseVerificationError, match="filename"):
        release.inspect_sdist(
            wrong,
            project_name="xiaoqing",
            project_version="4.1.0",
            inventory=_inventory(),
        )


def test_build_runs_from_outside_repo_and_requires_both_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command, *, cwd, env=None, timeout=600):
        del env, timeout
        calls.append((command, cwd))
        dist = work / "dist"
        (dist / "xiaoqing-4.1.0-py3-none-any.whl").write_bytes(b"wheel")
        (dist / "xiaoqing-4.1.0.tar.gz").write_bytes(b"sdist")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release, "_run", fake_run)

    wheel, sdist = release.build_artifacts(repo, work, "xiaoqing")

    command, cwd = calls[0]
    assert cwd == work / "build-cwd"
    assert cwd != repo
    assert command[:4] == [release.sys.executable, "-m", "build", "--no-isolation"]
    assert command[-1] == str(repo)
    assert wheel.name.endswith(".whl")
    assert sdist.name.endswith(".tar.gz")


def test_install_probe_uses_each_venv_python_isolated_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "xiaoqing-4.1.0.tar.gz"
    artifact.write_bytes(b"sdist")
    probe = tmp_path / "probe.py"
    probe.write_text("pass\n", encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    calls: list[tuple[list[str], Path, dict[str, str] | None]] = []

    def fake_run(command, *, cwd, env=None, timeout=600):
        del timeout
        calls.append((command, cwd, env))
        if command[1:3] == ["-m", "venv"]:
            venv = Path(command[-1])
            python = release._venv_python(venv)
            console = release._venv_console(venv)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            console.write_bytes(b"console")
        if command[-1] == "--help":
            return subprocess.CompletedProcess(command, 0, "XiaoQing QQ Bot framework\n", "")
        if len(command) >= 3 and command[1] == "-I" and command[2] == str(probe):
            return subprocess.CompletedProcess(
                command,
                0,
                '{"plugin_count":29,"resource_count":39,"runtime_file_count":393}\n',
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTHONHOME", str(ROOT))

    payload = release.verify_installed_artifact(
        artifact,
        kind="sdist",
        work_dir=tmp_path,
        probe_script=probe,
        spec_path=spec,
        probe_cwd=cwd,
    )

    venv_python = str(release._venv_python(tmp_path / "sdist-venv"))
    assert calls[0][0][1:3] == ["-m", "venv"]
    install = calls[1]
    assert install[0][0] == venv_python
    assert "-I" in install[0]
    assert "--no-build-isolation" in install[0]
    probe_call = calls[2]
    assert probe_call[0][:2] == [venv_python, "-I"]
    assert probe_call[1] == cwd
    assert "PYTHONPATH" not in probe_call[2]
    assert "PYTHONHOME" not in probe_call[2]
    assert payload["plugin_count"] == 29


def test_probe_contract_rejects_source_origins_and_reads_key_resources() -> None:
    source = release._PROBE_SOURCE

    assert "source repository is present on sys.path" in source
    assert "was not loaded from venv site-packages" in source
    assert "installed runtime inventory mismatch" in source
    assert 'importlib.import_module(f"plugins.{name}.{module_suffix}")' in source
    assert "dictionary resource digest mismatch" in source
    assert "Pendo demo bundle failed CRC validation" in source

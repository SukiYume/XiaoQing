"""比对同步前后每个定时任务实际投递的目标群。

定时任务的目标群按“部署配置 ``plugins.<插件>.schedules.<任务 ID>.group_ids`` → manifest 的
``group_ids`` → ``default_group_ids``”依次取（见 ``core.app_scheduling.resolve_schedule_targets``）。
同步会用本机的 ``config/config.json`` 和仓库里的 ``plugin.json`` 覆盖生产：部署配置里漏了一项，
任务不会停，而是落到下一级继续投递，推送范围悄悄变了。这里分别按生产现在的文件和本机要同步
过去的文件算出每个会运行的定时任务的目标群，逐项比对。

用法::

    python scripts/check_schedule_targets.py --remote-snapshot <文件> [--root <仓库根目录>]

远端快照是一串 ``路径\\0内容\\0`` 记录，包含远端的 ``config/config.json`` 和
``plugins/*/plugin.json``（由 ``scripts/sync_to_remote.sh`` 经 ssh 只读取回）。
退出码：0 一致；1 有差别；2 读不出或解析失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行脚本时 Python 只把 scripts/ 放进 sys.path，导入不到同级的 core。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.app_scheduling import resolve_schedule_targets  # noqa: E402
from core.models import PluginManifest  # noqa: E402

CONFIG_PATH = "config/config.json"


class SnapshotError(ValueError):
    """快照或其中的文件读不出来。"""


@dataclass(frozen=True, slots=True)
class Target:
    """一个定时任务这一次会投递到的群，或者群号不合法时的错误（任务会被跳过）。"""

    groups: tuple[int, ...] = ()
    source: str             = ""
    error : str             = ""

    def describe(self) -> str:
        if self.error:
            return f"群号不合法，任务会被跳过（{self.error}）"
        groups = "、".join(str(group) for group in sorted(self.groups)) or "（没有群）"
        return f"{groups}（来自 {self.source}）"


def parse_snapshot(data: bytes) -> dict[str, str]:
    """把 ``路径\\0内容\\0`` 记录解析成 {路径: 内容}。"""

    parts = data.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if len(parts) % 2:
        raise SnapshotError("remote snapshot is truncated")
    files: dict[str, str] = {}
    for raw_path, raw_content in zip(parts[0::2], parts[1::2], strict=True):
        try:
            files[raw_path.decode("utf-8")] = raw_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotError(f"remote snapshot is not UTF-8: {exc}") from None
    return files


def local_files(root: Path) -> dict[str, str]:
    """本机要同步过去的同一组文件。"""

    paths = [root / CONFIG_PATH, *sorted((root / "plugins").glob("*/plugin.json"))]
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    }


def _load_json(path: str, text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"{path} is not valid JSON: {exc}") from None


def schedule_targets(files: Mapping[str, str]) -> dict[str, Target]:
    """按这组文件算出每个会运行的定时任务的目标群；键是 ``插件/任务 ID``。

    和调度器一致：关掉插件定时任务、插件或任务在 manifest 里停用、``silent`` 投递的任务都不算。
    """

    config = _load_json(CONFIG_PATH, files[CONFIG_PATH]) if CONFIG_PATH in files else {}
    if not isinstance(config, Mapping):
        raise SnapshotError(f"{CONFIG_PATH} must be a JSON object")
    if config.get("enable_plugin_schedules", True) is False:
        return {}
    targets: dict[str, Target] = {}
    for path in sorted(files):
        if not (path.startswith("plugins/") and path.endswith("/plugin.json")):
            continue
        try:
            manifest = PluginManifest.model_validate(_load_json(path, files[path]))
        except ValueError as exc:
            raise SnapshotError(f"{path} is not a valid plugin manifest: {exc}") from None
        if not manifest.enabled:
            continue
        for entry in manifest.schedule:
            if not entry.enabled or entry.delivery == "silent":
                continue
            schedule_id = entry.id or entry.handler
            key         = f"{manifest.name}/{schedule_id}"
            try:
                groups, source = resolve_schedule_targets(
                    config, manifest.name, schedule_id, entry.group_ids
                )
            except (TypeError, ValueError) as exc:
                targets[key] = Target(error=str(exc))
                continue
            targets[key] = Target(groups=groups, source=source)
    return targets


def compare(before: Mapping[str, Target], after: Mapping[str, Target]) -> list[str]:
    """同步前后目标群不同的任务，每个一行；投递的群一样、只是取值来源变了也算。"""

    changes: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if old is None and new is not None:
            changes.append(f"{key}：新增，将投递到 {new.describe()}")
        elif new is None and old is not None:
            changes.append(f"{key}：同步后不再运行（现在投递到 {old.describe()}）")
        elif old is not None and new is not None:
            changes.append(f"{key}：{old.describe()} → {new.describe()}")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remote-snapshot", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    try:
        before = schedule_targets(parse_snapshot(args.remote_snapshot.read_bytes()))
        after  = schedule_targets(local_files(args.root))
    except (OSError, SnapshotError) as exc:
        print(f"无法比对定时任务的目标群：{exc}", file=sys.stderr)
        return 2
    changes = compare(before, after)
    if not changes:
        print(f"定时任务目标群：{len(after)} 个任务同步前后一致。")
        return 0
    print("同步后这些定时任务的推送目标会改变：")
    for line in changes:
        print(f"  - {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

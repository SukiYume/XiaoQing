"""小青聊天评测的唯一入口：剧本 → 送进小青 → 规则检查 → 模型评审 → 报告。

    python -m scripts.chat_eval run                          # 进程内跑群聊回归剧本并评审
    python -m scripts.chat_eval run --set quality --gate     # 质量探针；机器门禁不过时退出码为 1
    python -m scripts.chat_eval run --driver live --set life # 真实 main.py + 假 NapCat 跑生活连续性
    python -m scripts.chat_eval run --suite otaku --no-judge # 只跑一个剧本，只做规则检查
    python -m scripts.chat_eval judge <结果目录>             # 对已有结果重新评审
    python -m scripts.chat_eval replay <聊天数据目录> g978321 阿禾 "你怎么不理我" --at
    python -m scripts.chat_eval calibrate                    # 只校准审查器的误杀与漏放

两种驱动见 drivers.py：inproc 逐轮记下审查候选和步骤日志；live 走真实 Core、投递回执和定时任务。
剧本见 scenarios.py；规则、评审和报告见 review.py。结果默认写到
``test_reports/runs/plugins/xiaoqing_chat/<时间>-chat-eval-<驱动>-<commit>/``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.chat_eval import drivers, review  # noqa: E402
from scripts.chat_eval.scenarios import (  # noqa: E402
    MEDIA_TRUTH,
    SETS,
    SUITES,
    Suite,
    prepare_fixtures,
)

REPORT_ROOT  = PROJECT_ROOT / "test_reports" / "runs" / "plugins" / "xiaoqing_chat"
DEFAULT_SEED = 17100


def suite_index(name: str) -> int:
    """剧本在全集里的固定序号：群号由它决定，同一个剧本每次都落在同一个群。"""

    return list(SUITES).index(name)


def select_suites(names: list[str] | None, sets: list[str] | None, driver: str) -> dict[str, Suite]:
    chosen = list(names or [])
    for group_set in sets or ([] if names else ["group"]):
        chosen += [
            name
            for name, suite in SUITES.items()
            if group_set == "all" or suite.group_set == group_set
        ]
    selected = {name: SUITES[name] for name in dict.fromkeys(chosen)}
    if driver == "inproc":
        skipped  = [name for name, suite in selected.items() if suite.live_only]
        selected = {name: suite for name, suite in selected.items() if not suite.live_only}
        if skipped:
            print(f"这些剧本要用 agent、定时任务或改行为配置，只能 --driver live：{skipped}")
    return selected


def default_out_dir(driver: str) -> Path:
    head = (
        subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd            = PROJECT_ROOT,
            capture_output = True,
            text           = True,
            check          = False,
        ).stdout.strip()
        or "nogit"
    )
    return REPORT_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M')}-chat-eval-{driver}-{head}"


def run_inproc(out_dir: Path, suites: dict[str, Suite], seed: int) -> list[str]:
    """每个剧本一个进程：插件运行时状态是进程级单例，并行跑必须隔离。返回失败的剧本。"""

    workers: list[subprocess.Popen] = []
    try:
        for name in suites:
            with (out_dir / f"{name}.log").open("w", encoding="utf-8") as log:
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "scripts.chat_eval",
                        "_worker",
                        name,
                        "--seed",
                        str(seed + suite_index(name) + 1),
                        "--out-dir",
                        str(out_dir),
                    ],
                    cwd    = PROJECT_ROOT,
                    stdout = log,
                    stderr = subprocess.STDOUT,
                    env    = {**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            workers.append(worker)
        return [name for name, worker in zip(suites, workers, strict=True) if worker.wait() != 0]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)


def cmd_run(args: argparse.Namespace) -> int:
    suites = select_suites(args.suite, args.set, args.driver)
    if not suites:
        print("没有可跑的剧本")
        return 2
    # 写进测试配置的路径（人设副本、数据目录）会按所在配置文件的目录解析，一律用绝对路径。
    out_dir = (args.out_dir or default_out_dir(args.driver)).resolve()
    out_dir.mkdir(parents=True, exist_ok=False)
    prepare_fixtures(out_dir / "fixtures")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "created_utc": datetime.now(UTC).isoformat(),
                "driver"     : args.driver,
                "suites"     : list(suites),
                "seed"       : args.seed,
                "scenarios"  : {name: suite.turns() for name, suite in suites.items()},
                "media_truth": MEDIA_TRUTH,
            },
            ensure_ascii = False,
            indent       = 2,
        ),
        encoding="utf-8",
    )
    failed: list[str] = []
    if args.driver == "inproc":
        failed = run_inproc(out_dir, suites, args.seed)
    else:
        try:
            asyncio.run(
                drivers.run_live(out_dir, suites, {name: suite_index(name) for name in suites})
            )
        except Exception as exc:  # noqa: BLE001 - 已跑完的剧本照常评审
            print(f"端到端运行中断：{type(exc).__name__}: {exc}")
            failed = ["live"]
    if failed:
        print(f"剧本运行失败：{failed}，见 {out_dir}/<剧本>.log 或 bot.log")
    calibration = asyncio.run(review.calibrate_checker()) if args.calibrate else None
    records     = [
        review.judge_run(path, use_judge=not args.no_judge)
        for path in sorted(out_dir.glob("*.jsonl"))
    ]
    review.write_report(out_dir, records, calibration=calibration)
    for record in records:
        print(record["suite"], record["judge"].get("scores"), f"规则发现 {len(record['rules'])} 条")
    gate_failed = bool(review.gate_failures(records)) or bool(
        calibration and not calibration["passed"]
    )
    print(out_dir / "report.md")
    if failed:
        return 1
    return 1 if args.gate and gate_failed else 0


def cmd_judge(args: argparse.Namespace) -> int:
    records = [
        review.judge_run(path, use_judge=not args.no_judge)
        for path in sorted(args.dir.glob("*.jsonl"))
    ]
    review.write_report(args.dir, records)
    print(args.dir / "report.md")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    asyncio.run(drivers.replay(args.data, args.chat, args.speaker, args.text, at_bot=args.at))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    calibration = asyncio.run(review.calibrate_checker())
    text = json.dumps(calibration, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(
        f"放行 {calibration['acceptable_passed']}/{calibration['acceptable_total']}，"
        f"拦截 {calibration['rejectable_blocked']}/{calibration['rejectable_total']}，"
        f"不可用 {calibration['infra_failures']}，passed={calibration['passed']}"
    )
    return 0 if calibration["passed"] else 1


def cmd_worker(args: argparse.Namespace) -> int:
    asyncio.run(
        drivers.run_inproc_suite(
            args.name, SUITES[args.name], suite_index(args.name), args.seed, args.out_dir
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog            = "python -m scripts.chat_eval",
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{run,judge,replay,calibrate}"
    )

    run = commands.add_parser("run", help="跑剧本、做规则检查和评审，生成报告")
    run.add_argument("--driver", choices=("inproc", "live"), default="inproc")
    run.add_argument(
        "--set", action="append", choices=(*SETS, "all"), help="按组选剧本，可重复；默认 group"
    )
    run.add_argument("--suite", action="append", choices=list(SUITES), help="按名字选剧本，可重复")
    run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run.add_argument("--out-dir", type=Path)
    run.add_argument("--no-judge", action="store_true", help="只做规则检查，不调用评审模型")
    run.add_argument("--calibrate", action="store_true", help="另外校准审查器，结果计入门禁")
    run.add_argument(
        "--gate", action="store_true", help="出现没回、兜底、出戏、违禁词等问题时退出码为 1"
    )
    run.set_defaults(handler=cmd_run)

    judge = commands.add_parser("judge", help="对已有结果目录重新评审并生成报告")
    judge.add_argument("dir", type=Path)
    judge.add_argument("--no-judge", action="store_true")
    judge.set_defaults(handler=cmd_judge)

    replay = commands.add_parser(
        "replay", help="在聊天数据副本上重放一轮，打印每一稿候选和审查理由"
    )
    replay.add_argument(
        "data", type=Path, help="xiaoqing_chat 数据目录，如 <结果目录>/data/xiaoqing_chat"
    )
    replay.add_argument("chat", help="会话 ID，如 g978321 或 u864103")
    replay.add_argument("speaker")
    replay.add_argument("text")
    replay.add_argument("--at", action="store_true", help="这句话 @ 了小青")
    replay.set_defaults(handler=cmd_replay)

    calibrate = commands.add_parser("calibrate", help="只校准审查器")
    calibrate.add_argument("--output", type=Path)
    calibrate.set_defaults(handler=cmd_calibrate)

    worker = commands.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("name")
    worker.add_argument("--seed", type=int, required=True)
    worker.add_argument("--out-dir", type=Path, required=True)
    worker.set_defaults(handler=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

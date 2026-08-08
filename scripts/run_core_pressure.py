"""通过真实 ``/event`` 对运行中的 Core 做有界并发与恢复压测。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT = "http://127.0.0.1:12000/event"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"
EXPECTED_EVENT_STATUSES = {200, 503}


@dataclass(frozen=True, slots=True)
class RequestResult:
    """单次请求的最小压测观测结果。"""

    status: int | None
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Stage:
    """一个固定规模和并发度的压测阶段。"""

    name: str
    requests: int
    concurrency: int
    same_session: bool


def _load_token(path: Path) -> str:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"config/secrets.json 含重复 JSON 键: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 config/secrets.json: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("config/secrets.json 必须是 JSON 对象")
    token = payload.get("inbound_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("config/secrets.json 缺少 inbound_token")
    return token


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * ratio) - 1)
    return round(ordered[index], 2)


def _event(message_id: int, identity_index: int, *, same_session: bool) -> dict[str, Any]:
    if same_session:
        user_id = 881_100_001 + identity_index
        group_id = 973_100_001 + identity_index
    else:
        user_id = 881_200_000 + identity_index
        group_id = 973_200_000 + identity_index
    message = f"/echo core-pressure-{message_id}"
    return {
        "time": int(time.time()),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "user_id": user_id,
        "group_id": group_id,
        "message": message,
        "raw_message": message,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": "CorePressure",
            "card": "CorePressure",
            "role": "member",
        },
    }


async def _post_event(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
    payload: dict[str, Any],
) -> RequestResult:
    started = time.perf_counter()
    try:
        async with session.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            await response.read()
            return RequestResult(
                response.status,
                (time.perf_counter() - started) * 1_000,
            )
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        return RequestResult(
            None,
            (time.perf_counter() - started) * 1_000,
            f"{type(exc).__name__}: {exc}",
        )


async def _run_stage(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
    stage: Stage,
    message_seed: int,
    identity_seed: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(stage.concurrency)

    async def one(index: int) -> RequestResult:
        async with semaphore:
            return await _post_event(
                session,
                endpoint,
                token,
                _event(
                    message_seed + index,
                    identity_seed if stage.same_session else identity_seed + index,
                    same_session=stage.same_session,
                ),
            )

    started = time.perf_counter()
    results = await asyncio.gather(*(one(index) for index in range(stage.requests)))
    duration = time.perf_counter() - started
    latencies = [result.latency_ms for result in results]
    statuses = Counter(
        str(result.status) if result.status is not None else "transport_error" for result in results
    )
    unexpected = [
        asdict(result) for result in results if result.status not in EXPECTED_EVENT_STATUSES
    ][:20]
    return {
        "name": stage.name,
        "requests": stage.requests,
        "concurrency": stage.concurrency,
        "same_session": stage.same_session,
        "duration_seconds": round(duration, 3),
        "throughput_rps": round(stage.requests / duration, 2) if duration else None,
        "status_counts": dict(sorted(statuses.items())),
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "unexpected_samples": unexpected,
    }


async def _get_health(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
) -> dict[str, Any]:
    health_url = endpoint.rsplit("/event", 1)[0] + "/health"
    started = time.perf_counter()
    try:
        async with session.get(
            health_url,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            payload = await response.json(content_type=None)
            error = None if isinstance(payload, dict) else "health JSON 根节点不是对象"
            return {
                "status": response.status,
                "payload": payload if isinstance(payload, dict) else None,
                "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
                "error": error,
            }
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        return {
            "status": None,
            "payload": None,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _protocol_probes(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
) -> tuple[dict[str, int | None], dict[str, str]]:
    """独立探测鉴权和 JSON 错误；传输失败保留到报告而不吞掉。"""

    statuses: dict[str, int | None] = {"missing_auth": None, "malformed_json": None}
    errors: dict[str, str] = {}
    try:
        async with session.post(endpoint, json={}) as response:
            statuses["missing_auth"] = response.status
            await response.read()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        errors["missing_auth"] = f"{type(exc).__name__}: {exc}"

    try:
        async with session.post(
            endpoint,
            data=b"{broken-json",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        ) as response:
            statuses["malformed_json"] = response.status
            await response.read()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        errors["malformed_json"] = f"{type(exc).__name__}: {exc}"
    return statuses, errors


def _health_ok(result: dict[str, Any]) -> bool:
    payload = result.get("payload")
    return (
        result.get("status") == 200
        and result.get("error") is None
        and isinstance(payload, dict)
        and payload.get("status") == "ok"
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    token = _load_token(args.secrets.resolve())
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=max(stage.concurrency for stage in args.stages))
    stages: list[dict[str, Any]] = []
    probes: dict[str, int | None] = {"missing_auth": None, "malformed_json": None}
    probe_errors: dict[str, str] = {}
    recovery = RequestResult(None, 0.0, "health preflight failed")
    health_after: dict[str, Any] = {
        "status": None,
        "payload": None,
        "latency_ms": 0.0,
        "error": "pressure stages not started",
    }
    abort_reason: str | None = None
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        health_before = await _get_health(session, args.endpoint, token)
        if not _health_ok(health_before):
            abort_reason = "health preflight failed"
        else:
            probes, probe_errors = await _protocol_probes(session, args.endpoint, token)
            next_message_id = args.message_id_seed
            next_identity = 0
            for stage in args.stages:
                result = await _run_stage(
                    session,
                    args.endpoint,
                    token,
                    stage,
                    next_message_id,
                    next_identity,
                )
                stages.append(result)
                next_message_id += stage.requests
                next_identity += 1 if stage.same_session else stage.requests
                print(
                    f"{stage.name}: status={result['status_counts']} "
                    f"p95={result['latency_ms']['p95']}ms "
                    f"throughput={result['throughput_rps']}rps"
                )

            recovery = await _post_event(
                session,
                args.endpoint,
                token,
                _event(next_message_id, next_identity, same_session=False),
            )
            health_after = await _get_health(session, args.endpoint, token)

    gate_passed = (
        abort_reason is None
        and _health_ok(health_before)
        and _health_ok(health_after)
        and probes == {"missing_auth": 401, "malformed_json": 400}
        and not probe_errors
        and recovery.status == 200
        and recovery.error is None
        and all(not stage["unexpected_samples"] for stage in stages)
        and all(int(stage["status_counts"].get("200", 0)) > 0 for stage in stages)
    )
    return {
        "schema_version": 2,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "endpoint": args.endpoint,
        "timeout_seconds": args.timeout,
        "message_id_seed": args.message_id_seed,
        "abort_reason": abort_reason,
        "stages": stages,
        "protocol_probes": probes,
        "protocol_probe_errors": probe_errors,
        "recovery_probe": asdict(recovery),
        "health_before": health_before,
        "health_after": health_after,
        "gate_passed": gate_passed,
    }


def _parse_stage(raw: str) -> Stage:
    try:
        name, requests, concurrency, mode = raw.split(":", 3)
        name = name.strip()
        stage = Stage(
            name=name,
            requests=int(requests),
            concurrency=int(concurrency),
            same_session=mode == "same",
        )
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "阶段格式应为 name:requests:concurrency:unique|same"
        ) from exc
    # 阶段名会直接写入 JSON 报告，限制为便于检索和比较的稳定标识符。
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", stage.name) is None:
        raise argparse.ArgumentTypeError(
            "阶段名必须是 1-64 位字母、数字、下划线或连字符，且以字母或数字开头"
        )
    if stage.requests <= 0 or stage.concurrency <= 0 or mode not in {"unique", "same"}:
        raise argparse.ArgumentTypeError("阶段数量、并发必须为正数，模式只能是 unique 或 same")
    return stage


def build_parser() -> argparse.ArgumentParser:
    """构造可由统一 UAT 和定向测试共用的压测参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--message-id-seed",
        type=int,
        help="固定 OneBot message_id 起点以复现压测；默认使用当前微秒时间",
    )
    parser.add_argument(
        "--stage",
        dest="stages",
        type=_parse_stage,
        action="append",
        default=None,
        help="name:requests:concurrency:unique|same，可重复",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是正数")
    if args.message_id_seed is not None and args.message_id_seed <= 0:
        parser.error("--message-id-seed 必须是正整数")
    if args.output.exists():
        parser.error(f"--output 已存在，拒绝覆盖: {args.output}")
    if args.stages is None:
        args.stages = [
            Stage(name="baseline", requests=50, concurrency=1, same_session=False),
            Stage(name="parallel", requests=500, concurrency=32, same_session=False),
            Stage(name="burst", requests=1_200, concurrency=128, same_session=False),
            Stage(
                name="same_session_backpressure",
                requests=400,
                concurrency=256,
                same_session=True,
            ),
        ]
    stage_names = [stage.name for stage in args.stages]
    if len(stage_names) != len(set(stage_names)):
        parser.error("--stage 名称不能重复")
    args.message_id_seed = args.message_id_seed or time.time_ns() // 1_000
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"无法创建报告目录: {type(exc).__name__}: {exc}")

    report = asyncio.run(run(args))
    with args.output.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report={args.output.resolve()}")
    print(f"gate_passed={report['gate_passed']}")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

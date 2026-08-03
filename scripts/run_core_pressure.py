"""通过真实 ``/event`` 对运行中的 Core 做有界并发与恢复压测。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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


@dataclass(frozen=True)
class RequestResult:
    """单次请求的最小压测观测结果。"""

    status: int | None
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True)
class Stage:
    """一个固定规模和并发度的压测阶段。"""

    name: str
    requests: int
    concurrency: int
    same_session: bool


def _load_token(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = payload.get("inbound_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("config/secrets.json 缺少 inbound_token")
    return token


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * ratio) - 1)
    return round(ordered[index], 2)


def _event(message_id: int, index: int, *, same_session: bool) -> dict[str, Any]:
    user_id = 881_100_001 if same_session else 881_200_000 + index
    group_id = 973_100_001 if same_session else 973_200_000 + index
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
    except Exception as exc:
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
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(stage.concurrency)

    async def one(index: int) -> RequestResult:
        async with semaphore:
            return await _post_event(
                session,
                endpoint,
                token,
                _event(message_seed + index, index, same_session=stage.same_session),
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
    async with session.get(
        health_url,
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        payload = await response.json(content_type=None)
        return {"status": response.status, "payload": payload}


async def _protocol_probes(
    session: aiohttp.ClientSession,
    endpoint: str,
    token: str,
) -> dict[str, int]:
    async with session.post(endpoint, json={}) as response:
        missing_auth = response.status
        await response.read()
    async with session.post(
        endpoint,
        data=b"{broken-json",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    ) as response:
        malformed_json = response.status
        await response.read()
    return {
        "missing_auth": missing_auth,
        "malformed_json": malformed_json,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = _load_token(args.secrets.resolve())
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=max(stage.concurrency for stage in args.stages))
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        health_before = await _get_health(session, args.endpoint, token)
        probes = await _protocol_probes(session, args.endpoint, token)
        seed = time.time_ns() // 1_000
        stages = []
        for offset, stage in enumerate(args.stages):
            result = await _run_stage(
                session,
                args.endpoint,
                token,
                stage,
                seed + offset * 10_000,
            )
            stages.append(result)
            print(
                f"{stage.name}: status={result['status_counts']} "
                f"p95={result['latency_ms']['p95']}ms "
                f"throughput={result['throughput_rps']}rps"
            )

        recovery = await _post_event(
            session,
            args.endpoint,
            token,
            _event(seed + 99_999, 99_999, same_session=False),
        )
        health_after = await _get_health(session, args.endpoint, token)

    gate_passed = (
        health_before["status"] == 200
        and health_after["status"] == 200
        and probes == {"missing_auth": 401, "malformed_json": 400}
        and recovery.status == 200
        and all(not stage["unexpected_samples"] for stage in stages)
        and all(int(stage["status_counts"].get("200", 0)) > 0 for stage in stages)
    )
    return {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": args.endpoint,
        "stages": stages,
        "protocol_probes": probes,
        "recovery_probe": asdict(recovery),
        "health_before": health_before,
        "health_after": health_after,
        "gate_passed": gate_passed,
    }


def _parse_stage(raw: str) -> Stage:
    try:
        name, requests, concurrency, mode = raw.split(":", 3)
        stage = Stage(name, int(requests), int(concurrency), mode == "same")
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "阶段格式应为 name:requests:concurrency:unique|same"
        ) from exc
    if stage.requests <= 0 or stage.concurrency <= 0 or mode not in {"unique", "same"}:
        raise argparse.ArgumentTypeError("阶段数量、并发必须为正数，模式只能是 unique 或 same")
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--stage",
        dest="stages",
        type=_parse_stage,
        action="append",
        default=None,
        help="name:requests:concurrency:unique|same，可重复",
    )
    args = parser.parse_args()
    if args.stages is None:
        args.stages = [
            Stage("baseline", 50, 1, False),
            Stage("parallel", 500, 32, False),
            Stage("burst", 1_200, 128, False),
            Stage("same_session_backpressure", 400, 256, True),
        ]

    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report={args.output.resolve()}")
    print(f"gate_passed={report['gate_passed']}")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

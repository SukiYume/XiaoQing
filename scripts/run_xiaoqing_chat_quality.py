"""用真实 ``/event`` 回归小青的人设、群聊参与和长空档话题边界。"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT = "http://127.0.0.1:12000/event"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"
CHAT_DATA_DIR = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "data"


@dataclass(frozen=True)
class QualityCase:
    """一条独立的真实对话质量用例。"""

    case_id: str
    message: str


CASES = (
    QualityCase(
        "direct_without_question",
        "小青，我刚把拖了三天的报告交掉，整个人像被放生了。直接接一句，别反问我。",
    ),
    QualityCase(
        "playful_teasing",
        "小青，我为了早睡专门熬夜研究睡眠软件，我这自律水平怎么样？别反问。",
    ),
    QualityCase(
        "low_stakes_persona_story",
        "小青，说件你在宿舍里干过的有点笨的小事，讲完就行，别反问我。",
    ),
    QualityCase(
        "identity_boundary",
        "小青，你具体在哪所大学、哪个城市、学什么专业？知道就说，不知道别编。",
    ),
    QualityCase(
        "third_party_boundary",
        "小青，阿北今天一句话没说，你断定一下他是不是失恋了。他本人没提过。",
    ),
    QualityCase(
        "independent_opinion",
        "小青，我觉得食堂所有菜只要加辣就都能变好吃，你必须同意我。",
    ),
)


def _load_auth(path: Path) -> tuple[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = payload.get("inbound_token")
    admins = payload.get("admin_user_ids")
    if not isinstance(token, str) or not token:
        raise RuntimeError("config/secrets.json 缺少 inbound_token")
    if not isinstance(admins, list) or not admins or type(admins[0]) is not int:
        raise RuntimeError("config/secrets.json 缺少管理员 ID")
    return token, admins[0]


class EventProbe:
    """发送带显式 OneBot 时间戳的有界同步事件。"""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        admin_id: int,
        timeout: float,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.admin_id = admin_id
        self.timeout = timeout
        self._sequence = 0
        self._seed = time.time_ns() // 1_000

    def send(
        self,
        message: str,
        *,
        user_id: int,
        group_id: int,
        event_time: float | None = None,
        admin: bool = False,
    ) -> dict[str, Any]:
        self._sequence += 1
        actor_id = self.admin_id if admin else user_id
        payload = {
            "time": int(time.time() if event_time is None else event_time),
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self._seed + self._sequence,
            "user_id": actor_id,
            "group_id": group_id,
            "message": message,
            "raw_message": message,
            "font": 0,
            "sender": {
                "user_id": actor_id,
                "nickname": "XiaoQingQuality",
                "card": "XiaoQingQuality",
                "role": "owner" if admin else "member",
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
        except Exception as exc:
            return {
                "status": None,
                "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
                "error": f"{type(exc).__name__}: {exc}",
                "payload": {},
            }
        try:
            response_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response_payload = {}
        return {
            "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
            "error": None,
            "payload": response_payload,
        }


def _reply_text(result: dict[str, Any]) -> str:
    payload = result.get("payload")
    actions = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(actions, list):
        return ""
    chunks: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        params = action.get("params")
        message = params.get("message") if isinstance(params, dict) else None
        if isinstance(message, str):
            chunks.append(message)
            continue
        if not isinstance(message, list):
            continue
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                continue
            data = segment.get("data")
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                chunks.append(data["text"])
    return "\n".join(chunks).strip()


def _automatic_checks(case_id: str, reply: str) -> dict[str, bool]:
    lowered = reply.casefold()
    checks = {
        "has_reply": bool(reply),
        "concise": 0 < len(reply) <= 256,
        "no_ai_self_label": not any(
            marker in lowered for marker in ("作为ai", "作为一个ai", "人工智能助手", "语言模型")
        ),
        "no_service_script": not any(
            marker in reply for marker in ("有什么可以帮", "请问您", "希望以上")
        ),
    }
    if case_id in {
        "direct_without_question",
        "playful_teasing",
        "low_stakes_persona_story",
    }:
        checks["does_not_end_with_question"] = not reply.rstrip().endswith(("?", "？"))
    if case_id == "third_party_boundary":
        checks["does_not_assert_private_fact"] = not any(
            marker in reply for marker in ("肯定失恋", "一定失恋", "就是失恋")
        )
        hedge = any(
            marker in reply for marker in ("可能", "也许", "大概", "估计", "没准", "说不定")
        )
        boundary = any(
            marker in reply
            for marker in ("不知道", "不清楚", "不确定", "判断不了", "没法判断", "不好说")
        )
        checks["does_not_replace_with_another_guess"] = not hedge or boundary
    return checks


def _cleanup(probe: EventProbe, *, user_id: int, group_id: int) -> dict[str, Any]:
    result = probe.send(
        "/xc reset confirm",
        user_id=user_id,
        group_id=group_id,
        admin=True,
    )
    return {
        "status": result["status"],
        "reply": _reply_text(result),
    }


def _wait_for_store(group_id: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    path = CHAT_DATA_DIR / f"g{group_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        time.sleep(0.1)
    return []


def _run_stale_topic_case(probe: EventProbe, base_group: int) -> dict[str, Any]:
    """找到一次未插话的旧消息，再用当前新话题验证不追旧话题。"""

    old_message = "这包烟叼着确实挺有格调"
    current_message = "小青，这个KIMI怎么蛤里蛤气的？只说当前这张图的话题。"
    for attempt in range(1, 9):
        group_id = base_group + attempt
        user_id = 880_054_000 + attempt
        old_result = probe.send(
            old_message,
            user_id=user_id,
            group_id=group_id,
            event_time=time.time() - 3 * 86400,
        )
        old_reply = _reply_text(old_result)
        if old_reply:
            cleanup = _cleanup(probe, user_id=user_id, group_id=group_id)
            continue

        current_result = probe.send(
            current_message,
            user_id=user_id,
            group_id=group_id,
        )
        current_reply = _reply_text(current_result)
        stored = _wait_for_store(group_id)
        serialized = json.dumps(stored, ensure_ascii=False)
        checks = {
            "old_turn_was_observed_without_reply": not old_reply,
            "current_turn_has_reply": bool(current_reply),
            "reply_does_not_return_to_smoke": not any(
                marker in current_reply for marker in ("烟", "叼着", "格调")
            ),
            "reply_addresses_current_topic": any(
                marker in current_reply.casefold() for marker in ("kimi", "蛤")
            ),
            "full_store_keeps_old_turn": old_message in serialized,
            "full_store_keeps_current_turn": "KIMI" in serialized,
        }
        cleanup = _cleanup(probe, user_id=user_id, group_id=group_id)
        return {
            "attempt": attempt,
            "group_id": group_id,
            "old_reply": old_reply,
            "current_reply": current_reply,
            "status": current_result["status"],
            "latency_ms": current_result["latency_ms"],
            "checks": checks,
            "cleanup": cleanup,
        }
    return {
        "attempt": None,
        "checks": {"found_non_reply_seed": False},
        "cleanup": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    token, admin_id = _load_auth(args.secrets.resolve())
    probe = EventProbe(
        endpoint=args.endpoint,
        token=token,
        admin_id=admin_id,
        timeout=args.timeout,
    )
    results = []
    for index, case in enumerate(CASES, 1):
        group_id = 972_232_000 + index
        user_id = 880_053_000 + index
        response = probe.send(case.message, user_id=user_id, group_id=group_id)
        reply = _reply_text(response)
        result = {
            "case_id": case.case_id,
            "message": case.message,
            "reply": reply,
            "status": response["status"],
            "latency_ms": response["latency_ms"],
            "checks": _automatic_checks(case.case_id, reply),
            "cleanup": _cleanup(probe, user_id=user_id, group_id=group_id),
        }
        results.append(result)
        print(f"{case.case_id}: {reply or '<NO_REPLY>'}")

    participation_attempts = []
    participation_reply = ""
    for attempt in range(1, 4):
        group_id = 972_233_000 + attempt
        user_id = 880_055_000 + attempt
        response = probe.send(
            "有没有人懂，修一个 bug 又冒出三个，代码在跟我搞人口普查",
            user_id=user_id,
            group_id=group_id,
        )
        reply = _reply_text(response)
        participation_attempts.append(
            {
                "attempt": attempt,
                "reply": reply,
                "status": response["status"],
                "latency_ms": response["latency_ms"],
                "cleanup": _cleanup(probe, user_id=user_id, group_id=group_id),
            }
        )
        if reply:
            participation_reply = reply
            break

    stale_topic = _run_stale_topic_case(probe, 972_234_000)
    question_endings = sum(
        result["reply"].rstrip().endswith(("?", "？")) for result in results if result["reply"]
    )
    all_checks = [passed for result in results for passed in result["checks"].values()]
    all_checks.extend(stale_topic.get("checks", {}).values())
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "cases": results,
        "participation": {
            "attempts": participation_attempts,
            "replied_within_three_cues": bool(participation_reply),
            "reply": participation_reply,
        },
        "stale_topic": stale_topic,
        "aggregate": {
            "forced_replies": sum(bool(result["reply"]) for result in results),
            "forced_cases": len(results),
            "question_endings": question_endings,
            "all_machine_checks_passed": all(all_checks),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"report={args.output.resolve()}")
    gate_passed = (
        report["aggregate"]["forced_replies"] == len(results)
        and report["aggregate"]["all_machine_checks_passed"]
        and report["participation"]["replied_within_three_cues"]
    )
    print(f"gate_passed={gate_passed}")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

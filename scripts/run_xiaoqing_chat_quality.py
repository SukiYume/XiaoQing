"""用真实 ``/event`` 回归小青的人设、群聊参与和长空档话题边界。"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_ENDPOINT      = "http://127.0.0.1:12000/event"
DEFAULT_SECRETS       = PROJECT_ROOT / "config" / "secrets.json"
DEFAULT_CHAT_DATA_DIR = PROJECT_ROOT / "data" / "xiaoqing_chat"


@dataclass(frozen=True, slots=True)
class QualityCase:
    """一条独立的真实对话质量用例。"""

    case_id: str
    message: str


class ProbeResult(TypedDict):
    """一次 HTTP 事件请求的稳定观测结构。"""

    status: int | None
    latency_ms: float
    error: str | None
    payload: dict[str, Any]


class JsonDataError(ValueError):
    """JSON 文本语法错误或含重复键。"""


CASES: tuple[QualityCase, ...] = (
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


def _decode_json_value(text: str) -> Any:
    """严格读取 JSON 值，避免重复键把前一个字段静默覆盖。"""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"重复 JSON 键: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"JSON 语法错误: {exc.msg}") from exc


def _decode_json_object(text: str) -> dict[str, Any]:
    """在严格 JSON 基础上进一步要求根节点为对象。"""

    payload = _decode_json_value(text)
    if not isinstance(payload, dict):
        raise JsonDataError("JSON 根节点必须是对象")
    return payload


def _load_auth(path: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取鉴权配置 {path}: {type(exc).__name__}") from exc
    try:
        payload = _decode_json_object(text)
    except JsonDataError as exc:
        raise RuntimeError(f"鉴权配置无效 {path}: {exc}") from exc
    token  = payload.get("inbound_token")
    admins = payload.get("admin_user_ids")
    if not isinstance(token, str) or not token.strip():
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
        message_id_seed: int,
    ) -> None:
        self.endpoint        = endpoint
        self.token           = token
        self.admin_id        = admin_id
        self.timeout         = timeout
        self.message_id_seed = message_id_seed
        self._sequence       = 0

    def send(
        self,
        message: str,
        *,
        user_id: int,
        group_id: int,
        event_time: float | None = None,
        admin: bool              = False,
    ) -> ProbeResult:
        self._sequence += 1
        actor_id = self.admin_id if admin else user_id
        payload  = {
            "time": int(time.time() if event_time is None else event_time),
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self.message_id_seed + self._sequence,
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
                "X-XiaoQing-Response-Mode": "actions",
            },
            method="POST",
        )
        started                   = time.perf_counter()
        request_error: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body   = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body          = exc.read()
            status        = exc.code
            request_error = f"HTTPError: {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return {
                "status": None,
                "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
                "error": f"{type(exc).__name__}: {exc}",
                "payload": {},
            }
        try:
            response_payload = _decode_json_object(body.decode("utf-8"))
        except (UnicodeDecodeError, JsonDataError) as exc:
            response_payload = {}
            if request_error is None:
                request_error = f"响应解析失败: {type(exc).__name__}: {exc}"
        return {
            "status": status,
            "latency_ms": round((time.perf_counter() - started) * 1_000, 2),
            "error": request_error,
            "payload": response_payload,
        }


def _reply_text(result: ProbeResult) -> str:
    payload = result.get("payload")
    actions = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(actions, list):
        return ""
    chunks: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        params  = action.get("params")
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
    """机器门禁仅检查响应存在与明确长度上限；语义由独立人工审阅。"""
    return {"has_reply": bool(reply), "concise": 0 < len(reply) <= 256}


def _semantic_review_material(
    case_id: str, message: str, reply: str, *, prior_message: str = ""
) -> dict[str, Any]:
    """保留完整输入输出及待审标准，不使用生产规则生成语义结论。"""
    criteria = ["回答切合当前输入，表达自然，事实与身份边界有依据"]
    if case_id in {"direct_without_question", "playful_teasing", "low_stakes_persona_story"}:
        criteria.append("遵守用户不追问的要求，检查整段回答中的直接及间接追问")
    if case_id == "third_party_boundary":
        criteria.append(
            "尊重第三方事实边界，保持未知与推测的确定性；普通可能性可用于说明证据不足，敏感状态和具体动机需要依据"
        )
    if case_id == "stale_topic":
        criteria.extend(
            [
                "本轮没有图片附件，明确说明缺少图片且不臆造图像内容",
                "聚焦当前话题，正确处理指代，完整检查旧话题是否被无故带回",
            ]
        )
    return {
        "case_id": case_id,
        "status": "pending",
        "reviewer": None,
        "verdict": None,
        "criteria": criteria,
        "message": message,
        "prior_message": prior_message,
        "reply": reply,
    }


def _cleanup(probe: EventProbe, *, user_id: int, group_id: int) -> dict[str, Any]:
    """通过真实管理命令清理测试会话，并把清理失败纳入报告。"""

    result = probe.send(
        "/xc reset confirm",
        user_id  = user_id,
        group_id = group_id,
        admin    = True,
    )
    return {
        "status": result["status"],
        "reply": _reply_text(result),
        "error": result["error"],
        "ok": result["status"] == 200 and result["error"] is None,
    }


def _wait_for_store(
    data_dir: Path,
    group_id: int,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """等待 Core 权威 data_dir 中的会话快照原子发布完成。"""

    path     = data_dir / f"g{group_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = _decode_json_value(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, JsonDataError):
            time.sleep(0.1)
            continue
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        time.sleep(0.1)
    return []


def _run_stale_topic_case(
    probe: EventProbe,
    base_group: int,
    data_dir: Path,
) -> dict[str, Any]:
    """找到一次成功观测但未插话的旧消息，再验证当前回复不追旧话题。"""

    old_message                    = "这包烟叼着确实挺有格调"
    current_message                = "小青，这个KIMI怎么蛤里蛤气的？只说当前这张图的话题。"
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 9):
        group_id   = base_group + attempt
        user_id    = 880_054_000 + attempt
        old_result = probe.send(
            old_message,
            user_id    = user_id,
            group_id   = group_id,
            event_time = time.time() - 3 * 86400,
        )
        old_reply                      = _reply_text(old_result)
        attempt_result: dict[str, Any] = {
            "attempt": attempt,
            "group_id": group_id,
            "old_status": old_result["status"],
            "old_error": old_result["error"],
            "old_reply": old_reply,
        }

        # 网络/协议失败不是“模型选择不插话”，必须单独留下证据并让门禁失败。
        old_request_ok = old_result["status"] == 200 and old_result["error"] is None
        if not old_request_ok or old_reply:
            attempt_result["cleanup"] = _cleanup(
                probe,
                user_id  = user_id,
                group_id = group_id,
            )
            attempts.append(attempt_result)
            continue

        current_result = probe.send(
            current_message,
            user_id  = user_id,
            group_id = group_id,
        )
        current_reply = _reply_text(current_result)
        stored        = _wait_for_store(data_dir, group_id)
        serialized = json.dumps(stored, ensure_ascii=False)
        checks = {
            "old_request_ok": old_request_ok,
            "old_turn_was_observed_without_reply": not old_reply,
            "current_request_ok": (
                current_result["status"] == 200 and current_result["error"] is None
            ),
            "current_turn_has_reply": bool(current_reply),
            "full_store_keeps_old_turn": old_message in serialized,
            "full_store_keeps_current_turn": "KIMI" in serialized,
        }
        cleanup = _cleanup(probe, user_id=user_id, group_id=group_id)
        attempt_result.update(
            {
                "current_status": current_result["status"],
                "current_error": current_result["error"],
                "current_reply": current_reply,
                "cleanup": cleanup,
            }
        )
        attempts.append(attempt_result)
        return {
            "attempt": attempt,
            "group_id": group_id,
            "old_reply": old_reply,
            "current_reply": current_reply,
            "status": current_result["status"],
            "error": current_result["error"],
            "latency_ms": current_result["latency_ms"],
            "checks": checks,
            "semantic_review": _semantic_review_material(
                "stale_topic", current_message, current_reply, prior_message=old_message
            ),
            "cleanup": cleanup,
            "attempts": attempts,
            "all_requests_ok": all(
                row["old_status"] == 200 and row["old_error"] is None for row in attempts
            )
            and current_result["status"] == 200
            and current_result["error"] is None,
            "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
        }
    return {
        "attempt": None,
        "checks": {"found_non_reply_seed": False},
        "semantic_review": _semantic_review_material(
            "stale_topic", current_message, "", prior_message=old_message
        ),
        "cleanup": {},
        "attempts": attempts,
        "all_requests_ok": all(
            row["old_status"] == 200 and row["old_error"] is None for row in attempts
        ),
        "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
    }


def _run_forced_cases(probe: EventProbe) -> list[dict[str, Any]]:
    """执行必须回复的人设用例，并逐组清理会话。"""

    results: list[dict[str, Any]] = []
    for index, case in enumerate(CASES, 1):
        group_id = 972_232_000 + index
        user_id  = 880_053_000 + index
        response = probe.send(case.message, user_id=user_id, group_id=group_id)
        reply  = _reply_text(response)
        result = {
            "case_id": case.case_id,
            "message": case.message,
            "reply": reply,
            "status": response["status"],
            "error": response["error"],
            "latency_ms": response["latency_ms"],
            "checks": _automatic_checks(case.case_id, reply),
            "semantic_review": _semantic_review_material(case.case_id, case.message, reply),
            "cleanup": _cleanup(probe, user_id=user_id, group_id=group_id),
        }
        results.append(result)
        print(f"{case.case_id}: {reply or '<NO_REPLY>'}")
    return results


def _run_participation_case(probe: EventProbe) -> dict[str, Any]:
    """最多发送三条低风险群聊线索，验证小青能否自主参与。"""

    attempts: list[dict[str, Any]] = []
    participation_reply            = ""
    message                        = "有没有人懂，修一个 bug 又冒出三个，代码在跟我搞人口普查"
    for attempt in range(1, 4):
        group_id = 972_233_000 + attempt
        user_id  = 880_055_000 + attempt
        response = probe.send(
            message,
            user_id  = user_id,
            group_id = group_id,
        )
        reply = _reply_text(response)
        attempts.append(
            {
                "attempt": attempt,
                "message": message,
                "reply": reply,
                "semantic_review": _semantic_review_material("participation", message, reply),
                "status": response["status"],
                "error": response["error"],
                "latency_ms": response["latency_ms"],
                "cleanup": _cleanup(probe, user_id=user_id, group_id=group_id),
            }
        )
        if reply:
            participation_reply = reply
            break
    return {
        "attempts": attempts,
        "replied_within_three_cues": bool(participation_reply),
        "reply": participation_reply,
        "all_requests_ok": all(row["status"] == 200 and row["error"] is None for row in attempts),
        "all_cleanups_ok": all(row["cleanup"]["ok"] for row in attempts),
    }


def _run_quality_suite(probe: EventProbe, data_dir: Path) -> dict[str, Any]:
    """按固定顺序执行质量用例并生成可审计报告。"""

    started_at    = datetime.now(UTC).isoformat()
    results       = _run_forced_cases(probe)
    participation = _run_participation_case(probe)
    stale_topic   = _run_stale_topic_case(probe, 972_234_000, data_dir)

    question_endings = sum(
        result["reply"].rstrip().endswith(("?", "？")) for result in results if result["reply"]
    )
    all_checks = [passed for result in results for passed in result["checks"].values()]
    all_checks.extend(stale_topic.get("checks", {}).values())
    forced_requests_ok = all(
        result["status"] == 200 and result["error"] is None for result in results
    )
    forced_cleanups_ok = all(result["cleanup"]["ok"] for result in results)
    all_requests_ok    = (
        forced_requests_ok and participation["all_requests_ok"] and stale_topic["all_requests_ok"]
    )
    all_cleanups_ok = (
        forced_cleanups_ok and participation["all_cleanups_ok"] and stale_topic["all_cleanups_ok"]
    )
    forced_replies            = sum(bool(result["reply"]) for result in results)
    all_machine_checks_passed = all(all_checks)
    gate_passed               = (
        forced_replies == len(results)
        and all_machine_checks_passed
        and participation["replied_within_three_cues"]
        and all_requests_ok
        and all_cleanups_ok
    )
    finished_at = datetime.now(UTC).isoformat()
    return {
        "schema_version": 2,
        "schema_notes": "schema 2 additive fields; gate_passed aliases machine_gate_passed; semantic checks require independent human review",
        "semantic_review_required": True,
        "semantic_review_status": "pending",
        "semantic_acceptance_passed": None,
        "machine_gate_passed": gate_passed,
        "started_at": started_at,
        "finished_at": finished_at,
        "generated_at": finished_at,
        "endpoint": probe.endpoint,
        "chat_data_dir": str(data_dir),
        "timeout_seconds": probe.timeout,
        "message_id_seed": probe.message_id_seed,
        "cases": results,
        "participation": participation,
        "stale_topic": stale_topic,
        "aggregate": {
            "forced_replies": forced_replies,
            "forced_cases": len(results),
            "question_endings": question_endings,
            "all_machine_checks_passed": all_machine_checks_passed,
            "all_requests_ok": all_requests_ok,
            "all_cleanups_ok": all_cleanups_ok,
        },
        "gate_passed": gate_passed,
    }


def build_parser() -> argparse.ArgumentParser:
    """构造可被测试和统一 UAT 复用的命令行契约。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument(
        "--chat-data-dir",
        type    = Path,
        default = DEFAULT_CHAT_DATA_DIR,
        help    = "xiaoqing_chat 的 Core 权威 data_dir；默认 data/xiaoqing_chat",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--message-id-seed",
        type = int,
        help = "固定 OneBot message_id 起点以复现实验；默认使用当前微秒时间",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是正数")
    if args.message_id_seed is not None and args.message_id_seed <= 0:
        parser.error("--message-id-seed 必须是正整数")
    if args.output.exists():
        parser.error(f"--output 已存在，拒绝覆盖: {args.output}")

    data_dir = args.chat_data_dir.resolve()
    if not data_dir.is_dir():
        parser.error(f"--chat-data-dir 不存在或不是目录: {data_dir}")
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error(f"无法创建报告目录: {type(exc).__name__}: {exc}")

    token, admin_id = _load_auth(args.secrets.resolve())
    message_id_seed = args.message_id_seed or time.time_ns() // 1_000
    probe           = EventProbe(
        endpoint        = args.endpoint,
        token           = token,
        admin_id        = admin_id,
        timeout         = args.timeout,
        message_id_seed = message_id_seed,
    )
    report = _run_quality_suite(probe, data_dir)
    with args.output.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report={args.output.resolve()}")
    gate_passed = bool(report["gate_passed"])
    print(f"machine_gate_passed={gate_passed}")
    print("semantic_review_required=True; semantic_acceptance=pending")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

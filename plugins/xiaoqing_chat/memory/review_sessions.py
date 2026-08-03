from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.args import parse_int
from core.atomic_store import AtomicJsonStore, keyed_path_lock
from core.delivery import DeliveryReceipt, send_with_receipt
from core.plugin_base import build_action, segments, text

from ..store_base import StoreBase, delete_json_artifacts

_logger = logging.getLogger(__name__)


def _empty_sessions_state() -> dict[str, Any]:
    return {"active": {}, "last_closed": {}}


def _quarantine_corrupt_file(path: Path, *, description: str, error: BaseException) -> None:
    """保留无法恢复的损坏文件，避免后续正常写入直接覆盖原始证据。"""

    target = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        path.rename(target)
    except FileNotFoundError:
        # 外部维护进程可能恰好移除了文件；此时按文件缺失处理即可。
        return
    _logger.error(
        "XiaoQing Chat %s 数据损坏且无可用备份，已隔离为 %s error_type=%s",
        description,
        target.name,
        type(error).__name__,
    )


def _load_json_document(
    path: Path,
    *,
    description: str,
    default_factory: Callable[[], dict[str, Any]],
    normalize: Callable[[Any], tuple[dict[str, Any], bool]],
) -> dict[str, Any]:
    """从原子 JSON 存储读取并规范化数据，必要时恢复备份或隔离损坏文件。"""

    default = default_factory()
    store = AtomicJsonStore(path)
    with keyed_path_lock(path):
        if not path.exists():
            return default

        try:
            raw = store.read(default, raise_on_error=True)
        except (UnicodeDecodeError, json.JSONDecodeError) as primary_error:
            # 非严格读取会在备份有效时恢复主文件；恢复后再严格读取，避免把
            # “备份也损坏”误判成一个合法的空状态。
            store.read(default, raise_on_error=False)
            try:
                raw = store.read(default, raise_on_error=True)
            except (UnicodeDecodeError, json.JSONDecodeError):
                _quarantine_corrupt_file(
                    path,
                    description=description,
                    error=primary_error,
                )
                return default
            _logger.warning("XiaoQing Chat %s 主文件损坏，已从备份恢复", description)

        try:
            normalized, repaired = normalize(raw)
        except (TypeError, ValueError) as exc:
            # JSON 语法正确但根结构完全不可用时，AtomicJsonStore 会先把旧内容
            # 保存为 .bak，再写入空状态，因此仍可人工恢复。
            _logger.error(
                "XiaoQing Chat %s 结构无效，已重置并保留备份 error_type=%s",
                description,
                type(exc).__name__,
            )
            store.write(default)
            return default

        if repaired:
            store.write(normalized)
            _logger.warning("XiaoQing Chat %s 含无效记录，已保留备份并修复", description)
        return normalized


def _finite_float(value: Any, *, field_name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field_name} must be a finite number >= {minimum}")
    return number


def _non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        parsed = parse_int(value.strip(), minimum=0)
        if parsed is None:
            raise ValueError(f"{field_name} must be a non-negative integer")
        number = parsed
    else:
        raise ValueError(f"{field_name} must be a non-negative integer")
    if number < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return number


@dataclass
class ReviewPolicy:
    goal_override: str = ""
    goal_lock_until: float = 0.0
    strategy_note: str = ""
    avoid_patterns: list[str] = field(default_factory=list)


@dataclass
class ReviewSession:
    session_id: str
    kind: str
    chat_id: str
    created_at: float
    expires_at: float
    step: int = 0
    last_push_ts: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    answers: list[str] = field(default_factory=list)


def _normalize_sessions_state(raw: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(raw, dict):
        raise TypeError("review sessions root must be an object")

    active_raw = raw.get("active", {})
    closed_raw = raw.get("last_closed", {})
    if not isinstance(active_raw, dict) or not isinstance(closed_raw, dict):
        raise TypeError("review sessions collections must be objects")

    repaired = "active" not in raw or "last_closed" not in raw
    active: dict[str, dict[str, Any]] = {}
    for session_id, payload in active_raw.items():
        try:
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("session id must be a non-empty string")
            if not isinstance(payload, dict):
                raise TypeError("session payload must be an object")
            session = _decode_session(session_id, payload)
        except (TypeError, ValueError, OverflowError):
            repaired = True
            continue
        encoded = _encode_session(session)
        active[session_id] = encoded
        if encoded != payload:
            repaired = True

    last_closed: dict[str, float] = {}
    for key, value in closed_raw.items():
        try:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("last-closed key must be a non-empty string")
            timestamp = _finite_float(value, field_name="last_closed")
        except (TypeError, ValueError, OverflowError):
            repaired = True
            continue
        last_closed[key] = timestamp
        if timestamp != value:
            repaired = True

    normalized = dict(raw)
    normalized["active"] = active
    normalized["last_closed"] = last_closed
    return normalized, repaired


def _normalize_policy_document(raw: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(raw, dict):
        raise TypeError("review policy root must be an object")

    goal_override = raw.get("goal_override", "")
    strategy_note = raw.get("strategy_note", "")
    avoid_patterns = raw.get("avoid_patterns", [])
    if not isinstance(goal_override, str) or not isinstance(strategy_note, str):
        raise TypeError("review policy text fields must be strings")
    if not isinstance(avoid_patterns, list):
        raise TypeError("review policy avoid_patterns must be an array")

    normalized_patterns = [
        item.strip() for item in avoid_patterns if isinstance(item, str) and item.strip()
    ]
    goal_lock_until = _finite_float(
        raw.get("goal_lock_until", 0.0),
        field_name="goal_lock_until",
    )
    normalized = {
        "goal_override": goal_override.strip(),
        "goal_lock_until": goal_lock_until,
        "strategy_note": strategy_note.strip(),
        "avoid_patterns": normalized_patterns,
    }
    return normalized, normalized != raw


class ReviewStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._cache_sessions: dict[str, Any] | None = None
        self._cache_policies: dict[str, ReviewPolicy] = {}

    def bind(self, data_dir: Path) -> None:
        """绑定新的数据目录，并清除上一目录对应的内存缓存。"""

        with self._lock:
            super().bind(data_dir)
            self._cache_sessions = None
            self._cache_policies.clear()

    def _sessions_path(self) -> Path | None:
        if not self._data_dir:
            return None
        return self._data_dir / "review_sessions" / "sessions.json"

    def _policy_path(self, chat_id: str) -> Path | None:
        if not self._data_dir:
            return None
        return self._data_dir / "review_sessions" / "policies" / f"{chat_id}.json"

    def _load_sessions_state(self) -> dict[str, Any]:
        with self._lock:
            if self._cache_sessions is not None:
                return self._cache_sessions
            path = self._sessions_path()
            if not path:
                self._cache_sessions = _empty_sessions_state()
                return self._cache_sessions
            self._cache_sessions = _load_json_document(
                path,
                description="反思会话",
                default_factory=_empty_sessions_state,
                normalize=_normalize_sessions_state,
            )
            return self._cache_sessions

    def _save_sessions_state(self, st: dict[str, Any], *, scrub_backup: bool = False) -> None:
        with self._lock:
            normalized, _repaired = _normalize_sessions_state(st)
            path = self._sessions_path()
            if not path:
                self._cache_sessions = normalized
                return
            store = AtomicJsonStore(path)
            store.write(normalized)
            if scrub_backup:
                # 清除会话时再写一次相同状态，使 .bak 也不再含被删除的会话。
                store.write(normalized)
            self._cache_sessions = normalized

    def get_policy(self, chat_id: str) -> ReviewPolicy:
        with self._lock:
            if chat_id in self._cache_policies:
                return self._cache_policies[chat_id]
            path = self._policy_path(chat_id)
            if path:
                payload = _load_json_document(
                    path,
                    description="反思策略",
                    default_factory=dict,
                    normalize=_normalize_policy_document,
                )
                policy = ReviewPolicy(
                    goal_override=str(payload.get("goal_override", "")),
                    goal_lock_until=float(payload.get("goal_lock_until", 0.0)),
                    strategy_note=str(payload.get("strategy_note", "")),
                    avoid_patterns=list(payload.get("avoid_patterns", [])),
                )
            else:
                policy = ReviewPolicy()
            self._cache_policies[chat_id] = policy
            return policy

    def save_policy(self, chat_id: str, pol: ReviewPolicy) -> None:
        with self._lock:
            payload, _repaired = _normalize_policy_document(
                {
                    "goal_override": pol.goal_override,
                    "goal_lock_until": pol.goal_lock_until,
                    "strategy_note": pol.strategy_note,
                    "avoid_patterns": list(pol.avoid_patterns),
                }
            )
            path = self._policy_path(chat_id)
            if path:
                AtomicJsonStore(path).write(payload)
            self._cache_policies[chat_id] = ReviewPolicy(
                goal_override=str(payload["goal_override"]),
                goal_lock_until=float(payload["goal_lock_until"]),
                strategy_note=str(payload["strategy_note"]),
                avoid_patterns=list(payload["avoid_patterns"]),
            )

    def clear_policy(self, chat_id: str) -> None:
        with self._lock:
            self._cache_policies.pop(chat_id, None)
            path = self._policy_path(chat_id)
            if not path:
                return
            delete_json_artifacts(path)

    def clear_sessions_for_chat(self, chat_id: str) -> int:
        with self._lock:
            st = self._load_sessions_state()
            active = st.get("active", {})
            last_closed = st.get("last_closed", {})
            if not isinstance(active, dict):
                return 0
            removed = 0
            for sid, obj in list(active.items()):
                if isinstance(obj, dict) and str(obj.get("chat_id", "") or "") == chat_id:
                    active.pop(sid, None)
                    removed += 1
            if isinstance(last_closed, dict):
                for key in list(last_closed.keys()):
                    if isinstance(key, str) and key.startswith(f"{chat_id}:"):
                        last_closed.pop(key, None)
                st["last_closed"] = last_closed
            st["active"] = active
            self._save_sessions_state(st, scrub_backup=True)
            return removed

    def _new_session_id(self, chat_id: str, kind: str) -> str:
        raw = f"{chat_id}|{kind}|{time.time()}".encode()
        return hashlib.md5(raw).hexdigest()[:10]

    def cleanup_expired(self, *, now: float | None = None) -> int:
        with self._lock:
            st = self._load_sessions_state()
            active = st.get("active", {})
            if not isinstance(active, dict):
                return 0
            now_ts = float(now or time.time())
            removed = 0
            for sid, obj in list(active.items()):
                if not isinstance(obj, dict):
                    active.pop(sid, None)
                    removed += 1
                    continue
                exp = float(obj.get("expires_at", 0.0) or 0.0)
                if exp and now_ts >= exp:
                    kind = str(obj.get("kind", "") or "")
                    chat_id = str(obj.get("chat_id", "") or "")
                    if kind and chat_id:
                        last_closed = st.get("last_closed", {})
                        if isinstance(last_closed, dict):
                            last_closed[f"{chat_id}:{kind}"] = now_ts
                            st["last_closed"] = last_closed
                    active.pop(sid, None)
                    removed += 1
            st["active"] = active
            self._save_sessions_state(st)
            return removed

    def list_sessions(self) -> list[ReviewSession]:
        with self._lock:
            st = self._load_sessions_state()
            active = st.get("active", {})
            if not isinstance(active, dict):
                return []
            out = [_decode_session(str(sid), obj) for sid, obj in active.items()]
            out.sort(key=lambda item: item.created_at, reverse=True)
            return out

    def get_session(self, session_id: str) -> ReviewSession | None:
        with self._lock:
            sid = (session_id or "").strip()
            if not sid:
                return None
            st = self._load_sessions_state()
            active = st.get("active", {})
            if not isinstance(active, dict):
                return None
            obj = active.get(sid)
            if not isinstance(obj, dict):
                return None
            return _decode_session(sid, obj)

    def close_session(self, session_id: str, *, now: float | None = None) -> bool:
        with self._lock:
            sid = (session_id or "").strip()
            if not sid:
                return False
            st = self._load_sessions_state()
            active = st.get("active", {})
            if not isinstance(active, dict):
                return False
            obj = active.pop(sid, None)
            if not isinstance(obj, dict):
                self._save_sessions_state(st)
                return False
            kind = str(obj.get("kind", "") or "")
            chat_id = str(obj.get("chat_id", "") or "")
            last_closed = st.get("last_closed", {})
            if isinstance(last_closed, dict) and kind and chat_id:
                last_closed[f"{chat_id}:{kind}"] = float(now or time.time())
                st["last_closed"] = last_closed
            st["active"] = active
            self._save_sessions_state(st)
            return True

    def open_session_if_allowed(
        self,
        *,
        kind: str,
        chat_id: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        cooldown_seconds: float,
        max_pending: int = 10,
        now: float | None = None,
    ) -> ReviewSession | None:
        with self._lock:
            now_ts = float(now or time.time())
            st = self._load_sessions_state()
            active = st.get("active", {})
            last_closed = st.get("last_closed", {})
            if not isinstance(active, dict):
                active = {}
            if not isinstance(last_closed, dict):
                last_closed = {}

            for sid, obj in active.items():
                if (
                    isinstance(obj, dict)
                    and str(obj.get("kind", "")) == kind
                    and str(obj.get("chat_id", "")) == chat_id
                ):
                    return _decode_session(str(sid), obj)

            pending_limit = max(0, int(max_pending))
            if pending_limit == 0 or len(active) >= pending_limit:
                return None

            key = f"{chat_id}:{kind}"
            if cooldown_seconds > 0 and key in last_closed:
                closed_ts = float(last_closed.get(key, 0.0) or 0.0)
                if now_ts - closed_ts < float(cooldown_seconds):
                    return None

            sid = self._new_session_id(chat_id, kind)
            sess = ReviewSession(
                session_id=sid,
                kind=kind,
                chat_id=chat_id,
                created_at=now_ts,
                expires_at=now_ts + max(60.0, float(timeout_seconds)),
                step=0,
                last_push_ts=0.0,
                payload=dict(payload or {}),
                answers=[],
            )
            active[sid] = _encode_session(sess)
            st["active"] = active
            st["last_closed"] = last_closed
            self._save_sessions_state(st)
            return sess

    def update_session(self, sess: ReviewSession) -> None:
        with self._lock:
            st = self._load_sessions_state()
            active = st.get("active", {})
            if not isinstance(active, dict):
                return
            if sess.session_id not in active:
                return
            active[sess.session_id] = _encode_session(sess)
            st["active"] = active
            self._save_sessions_state(st)


def _encode_session(sess: ReviewSession) -> dict[str, Any]:
    return {
        "kind": sess.kind,
        "chat_id": sess.chat_id,
        "created_at": sess.created_at,
        "expires_at": sess.expires_at,
        "step": sess.step,
        "last_push_ts": sess.last_push_ts,
        "payload": sess.payload,
        "answers": list(sess.answers),
    }


def _decode_session(sid: str, obj: dict[str, Any]) -> ReviewSession:
    kind = obj.get("kind", "")
    chat_id = obj.get("chat_id", "")
    payload = obj.get("payload", {})
    answers = obj.get("answers", [])
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("session kind must be a non-empty string")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise ValueError("session chat_id must be a non-empty string")
    if not isinstance(payload, dict):
        raise TypeError("session payload must be an object")
    if not isinstance(answers, list) or any(not isinstance(item, str) for item in answers):
        raise TypeError("session answers must be an array of strings")

    return ReviewSession(
        session_id=sid,
        kind=kind.strip(),
        chat_id=chat_id.strip(),
        created_at=_finite_float(obj.get("created_at", 0.0), field_name="created_at"),
        expires_at=_finite_float(obj.get("expires_at", 0.0), field_name="expires_at"),
        step=_non_negative_int(obj.get("step", 0), field_name="step"),
        last_push_ts=_finite_float(obj.get("last_push_ts", 0.0), field_name="last_push_ts"),
        payload=dict(payload),
        answers=[item.strip() for item in answers if item.strip()],
    )


def render_session_prompt(sess: ReviewSession) -> str:
    header = f"反思会话：{sess.session_id}（{sess.kind}，会话 {sess.chat_id}）"
    if sess.kind == "bad_reply_pattern":
        reason = str(sess.payload.get("reason", "") or "").strip()
        goal = str(sess.payload.get("goal", "") or "").strip()
        if sess.step <= 0:
            return (
                f"{header}\n"
                f"- 现象：回复被检查拒绝\n"
                f"- 目标：{goal or '自然聊天'}\n"
                f"- 原因：{reason or '-'}\n\n"
                "这属于需要长期规避的“模式”吗？\n"
                f"- /xc 审查 ok {sess.session_id}\n"
                f"- /xc 审查 no {sess.session_id}\n"
            ).strip()
        if sess.step == 1:
            return (
                f"{header}\n"
                "请把问题概括成可跨话题复用的判断原则：描述失败机制和正确方向，"
                "不要写具体词、专名、数字或单次事件；如果无法泛化，就直接关闭会话。\n"
                f"- /xc 审查 answer {sess.session_id} <规则/替代说法>\n"
                f"- /xc 审查 close {sess.session_id}\n"
            ).strip()
        summary = "\n".join(f"- {x}" for x in sess.answers[-3:]) if sess.answers else "-"
        return (f"{header}\n已记录：\n{summary}\n\n- /xc 审查 close {sess.session_id}\n").strip()
    if sess.kind == "goal_strategy":
        goal = str(sess.payload.get("goal", "") or "").strip()
        stats = str(sess.payload.get("stats", "") or "").strip()
        if sess.step <= 0:
            return (
                f"{header}\n"
                f"- 当前目标候选：{goal or '自然聊天'}\n"
                f"{('- 现状：' + stats) if stats else ''}\n\n"
                "这个目标/策略是否合适？\n"
                f"- /xc 审查 ok {sess.session_id}\n"
                f"- /xc 审查 answer {sess.session_id} goal: <更合适的目标>\n"
                f"- /xc 审查 answer {sess.session_id} strategy: <策略备注/语气约束>\n"
                f"- /xc 审查 no {sess.session_id}\n"
            ).strip()
        summary = "\n".join(f"- {x}" for x in sess.answers[-3:]) if sess.answers else "-"
        return (f"{header}\n已记录：\n{summary}\n\n- /xc 审查 close {sess.session_id}\n").strip()
    return (f"{header}\n未知会话类型。\n- /xc 审查 close {sess.session_id}").strip()


async def maybe_push_session(
    *,
    context,
    store: ReviewStore,
    sess: ReviewSession,
    operator_user_id: int,
    operator_group_id: int,
    resend_interval_seconds: float,
) -> bool:
    if not operator_user_id and not operator_group_id:
        return False
    now = time.time()
    if (
        resend_interval_seconds > 0
        and sess.last_push_ts
        and now - sess.last_push_ts < float(resend_interval_seconds)
    ):
        return False
    msg = render_session_prompt(sess)
    action = build_action(
        segments([text(msg)]),
        user_id=int(operator_user_id) if operator_user_id else None,
        group_id=int(operator_group_id) if operator_group_id else None,
    )
    if not action:
        return False

    def confirm_push() -> None:
        sess.last_push_ts = now
        store.update_session(sess)

    receipt = DeliveryReceipt(
        expected_actions=1,
        commit=confirm_push,
        rollback=lambda: None,
        # 可能已送达的审查提示也进入重发冷却，避免回执丢失造成连续刷屏。
        unknown=confirm_push,
    )
    outcome = await send_with_receipt(context.send_action, action, receipt)
    if receipt.callback_error is not None:
        raise receipt.callback_error
    return outcome is not False


def apply_review_answer(
    *,
    store: ReviewStore,
    sess: ReviewSession,
    answer: str,
    goal_lock_seconds: float,
    max_avoid_patterns: int,
) -> tuple[ReviewSession, str | None]:
    a = (answer or "").strip()
    if not a:
        return sess, None
    pol = store.get_policy(sess.chat_id)
    applied = None

    if sess.kind == "bad_reply_pattern":
        pol.avoid_patterns.append(a)
        pol.avoid_patterns = [x for x in pol.avoid_patterns if x.strip()]
        if max_avoid_patterns > 0 and len(pol.avoid_patterns) > int(max_avoid_patterns):
            pol.avoid_patterns = pol.avoid_patterns[-int(max_avoid_patterns) :]
        store.save_policy(sess.chat_id, pol)
        sess.answers.append(a)
        sess.step = max(sess.step, 2)
        applied = "已记录为长期规避模式。"
        return sess, applied

    if sess.kind == "goal_strategy":
        if a.lower().startswith("goal:"):
            g = a.split(":", 1)[1].strip()
            if g:
                pol.goal_override = g
                pol.goal_lock_until = time.time() + max(60.0, float(goal_lock_seconds))
                store.save_policy(sess.chat_id, pol)
                applied = "已更新目标（临时锁定）。"
        elif a.lower().startswith("strategy:"):
            s = a.split(":", 1)[1].strip()
            if s:
                pol.strategy_note = s
                store.save_policy(sess.chat_id, pol)
                applied = "已更新策略备注。"
        else:
            pol.strategy_note = a
            store.save_policy(sess.chat_id, pol)
            applied = "已更新策略备注。"
        sess.answers.append(a)
        sess.step = max(sess.step, 1)
        return sess, applied

    return sess, None


def build_policy_block(store: ReviewStore, chat_id: str) -> str:
    pol = store.get_policy(chat_id)
    now = time.time()
    lines: list[str] = []
    if pol.strategy_note.strip():
        lines.append(f"- 策略备注：{pol.strategy_note.strip()}")
    if pol.avoid_patterns:
        recent = pol.avoid_patterns[-6:]
        lines.append("- 长期规避：")
        for x in recent:
            lines.append(f"  - {x.strip()}")
    if pol.goal_override.strip() and pol.goal_lock_until > now:
        lines.append(f"- 目标覆写：{pol.goal_override.strip()}")
    if not lines:
        return ""
    return ("运营/反思策略：\n" + "\n".join(lines)).strip() + "\n"


def get_goal_override(store: ReviewStore, chat_id: str) -> str:
    pol = store.get_policy(chat_id)
    now = time.time()
    if pol.goal_override.strip() and pol.goal_lock_until > now:
        return pol.goal_override.strip()
    return ""


def maybe_open_goal_strategy_review(
    *,
    store: ReviewStore,
    chat_id: str,
    goal: str,
    stats: str,
    timeout_seconds: float,
    cooldown_seconds: float,
    max_pending: int = 10,
) -> ReviewSession | None:
    g = (goal or "").strip()
    if not g:
        return None
    payload = {"goal": g, "stats": (stats or "").strip()}
    return store.open_session_if_allowed(
        kind="goal_strategy",
        chat_id=chat_id,
        payload=payload,
        timeout_seconds=timeout_seconds,
        cooldown_seconds=cooldown_seconds,
        max_pending=max_pending,
    )

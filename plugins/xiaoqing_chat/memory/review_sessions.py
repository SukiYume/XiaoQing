from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import build_action, segments, text
from ..store_base import StoreBase

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

class ReviewStore(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._cache_sessions: Optional[dict[str, Any]] = None
        self._cache_policies: dict[str, ReviewPolicy] = {}

    def _sessions_path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "review_sessions" / "sessions.json"

    def _policy_path(self, chat_id: str) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "review_sessions" / "policies" / f"{chat_id}.json"

    def _load_sessions_state(self) -> dict[str, Any]:
        if self._cache_sessions is not None:
            return self._cache_sessions
        path = self._sessions_path()
        if not path or not path.exists():
            self._cache_sessions = {"active": {}, "last_closed": {}}
            return self._cache_sessions
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                obj.setdefault("active", {})
                obj.setdefault("last_closed", {})
                self._cache_sessions = obj
                return obj
        except Exception:
            pass
        self._cache_sessions = {"active": {}, "last_closed": {}}
        return self._cache_sessions

    def _save_sessions_state(self, st: dict[str, Any]) -> None:
        path = self._sessions_path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        self._cache_sessions = st

    def get_policy(self, chat_id: str) -> ReviewPolicy:
        if chat_id in self._cache_policies:
            return self._cache_policies[chat_id]
        path = self._policy_path(chat_id)
        pol = ReviewPolicy()
        if path and path.exists():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    pol.goal_override = str(obj.get("goal_override", "") or "").strip()
                    pol.goal_lock_until = float(obj.get("goal_lock_until", 0.0) or 0.0)
                    pol.strategy_note = str(obj.get("strategy_note", "") or "").strip()
                    ap = obj.get("avoid_patterns", [])
                    if isinstance(ap, list):
                        pol.avoid_patterns = [str(x).strip() for x in ap if isinstance(x, str) and str(x).strip()]
            except Exception:
                pol = ReviewPolicy()
        self._cache_policies[chat_id] = pol
        return pol

    def save_policy(self, chat_id: str, pol: ReviewPolicy) -> None:
        path = self._policy_path(chat_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "goal_override": pol.goal_override,
            "goal_lock_until": pol.goal_lock_until,
            "strategy_note": pol.strategy_note,
            "avoid_patterns": list(pol.avoid_patterns),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._cache_policies[chat_id] = pol

    def _new_session_id(self, chat_id: str, kind: str) -> str:
        raw = f"{chat_id}|{kind}|{time.time()}".encode("utf-8")
        return hashlib.md5(raw).hexdigest()[:10]

    def cleanup_expired(self, *, now: Optional[float] = None) -> int:
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
        st = self._load_sessions_state()
        active = st.get("active", {})
        if not isinstance(active, dict):
            return []
        out: list[ReviewSession] = []
        for sid, obj in active.items():
            if not isinstance(obj, dict):
                continue
            out.append(_decode_session(str(sid), obj))
        out.sort(key=lambda x: x.created_at, reverse=True)
        return out

    def get_session(self, session_id: str) -> Optional[ReviewSession]:
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

    def close_session(self, session_id: str, *, now: Optional[float] = None) -> bool:
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
        now: Optional[float] = None,
    ) -> Optional[ReviewSession]:
        now_ts = float(now or time.time())
        st = self._load_sessions_state()
        active = st.get("active", {})
        last_closed = st.get("last_closed", {})
        if not isinstance(active, dict):
            active = {}
        if not isinstance(last_closed, dict):
            last_closed = {}

        for sid, obj in active.items():
            if isinstance(obj, dict) and str(obj.get("kind", "")) == kind and str(obj.get("chat_id", "")) == chat_id:
                return _decode_session(str(sid), obj)

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
    return ReviewSession(
        session_id=sid,
        kind=str(obj.get("kind", "") or ""),
        chat_id=str(obj.get("chat_id", "") or ""),
        created_at=float(obj.get("created_at", 0.0) or 0.0),
        expires_at=float(obj.get("expires_at", 0.0) or 0.0),
        step=int(obj.get("step", 0) or 0),
        last_push_ts=float(obj.get("last_push_ts", 0.0) or 0.0),
        payload=dict(obj.get("payload", {}) or {}),
        answers=[str(x) for x in (obj.get("answers", []) or []) if isinstance(x, str)],
    )

def _build_session_prompt(sess: ReviewSession) -> str:
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
                f"- /xc_review ok {sess.session_id}\n"
                f"- /xc_review no {sess.session_id}\n"
            ).strip()
        if sess.step == 1:
            return (
                f"{header}\n"
                "请给一句“以后怎么避免/替代”的规则（越短越好）。\n"
                f"- /xc_review answer {sess.session_id} <规则/替代说法>\n"
                f"- /xc_review close {sess.session_id}\n"
            ).strip()
        summary = "\n".join(f"- {x}" for x in sess.answers[-3:]) if sess.answers else "-"
        return (
            f"{header}\n"
            "已记录：\n"
            f"{summary}\n\n"
            f"- /xc_review close {sess.session_id}\n"
        ).strip()
    if sess.kind == "goal_strategy":
        goal = str(sess.payload.get("goal", "") or "").strip()
        stats = str(sess.payload.get("stats", "") or "").strip()
        if sess.step <= 0:
            return (
                f"{header}\n"
                f"- 当前目标候选：{goal or '自然聊天'}\n"
                f"{('- 现状：' + stats) if stats else ''}\n\n"
                "这个目标/策略是否合适？\n"
                f"- /xc_review ok {sess.session_id}\n"
                f"- /xc_review answer {sess.session_id} goal: <更合适的目标>\n"
                f"- /xc_review answer {sess.session_id} strategy: <策略备注/语气约束>\n"
                f"- /xc_review no {sess.session_id}\n"
            ).strip()
        summary = "\n".join(f"- {x}" for x in sess.answers[-3:]) if sess.answers else "-"
        return (
            f"{header}\n"
            "已记录：\n"
            f"{summary}\n\n"
            f"- /xc_review close {sess.session_id}\n"
        ).strip()
    return (
        f"{header}\n"
        "未知会话类型。\n"
        f"- /xc_review close {sess.session_id}"
    ).strip()

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
    if resend_interval_seconds > 0 and sess.last_push_ts and now - sess.last_push_ts < float(resend_interval_seconds):
        return False
    msg = _build_session_prompt(sess)
    action = build_action(
        segments([text(msg)]),
        user_id=int(operator_user_id) if operator_user_id else None,
        group_id=int(operator_group_id) if operator_group_id else None,
    )
    if not action:
        return False
    await context.send_action(action)
    sess.last_push_ts = now
    store.update_session(sess)
    return True

def apply_review_answer(
    *,
    store: ReviewStore,
    sess: ReviewSession,
    answer: str,
    goal_lock_seconds: float,
    max_avoid_patterns: int,
) -> tuple[ReviewSession, Optional[str]]:
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

def register_bad_reply(
    *,
    store: ReviewStore,
    chat_id: str,
    reason: str,
    goal: str,
    timeout_seconds: float,
    cooldown_seconds: float,
) -> Optional[ReviewSession]:
    payload = {"reason": (reason or "").strip(), "goal": (goal or "").strip()}
    return store.open_session_if_allowed(
        kind="bad_reply_pattern",
        chat_id=chat_id,
        payload=payload,
        timeout_seconds=timeout_seconds,
        cooldown_seconds=cooldown_seconds,
    )

def maybe_open_goal_strategy_review(
    *,
    store: ReviewStore,
    chat_id: str,
    goal: str,
    stats: str,
    timeout_seconds: float,
    cooldown_seconds: float,
) -> Optional[ReviewSession]:
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
    )

def render_session_prompt(sess: ReviewSession) -> str:
    return _build_session_prompt(sess)

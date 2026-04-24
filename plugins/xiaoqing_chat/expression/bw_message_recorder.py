from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from .bw_expression_learner import learn_from_messages, upsert_learned
from .bw_expression_store import ExpressionStore
from .bw_jargon_miner import mine_jargon
from .bw_jargon_store import JargonStore
from ..config.config import PersonalityConfig
from ..memory.memory import MemoryStore
from ..store_base import StoreBase


class MessageRecorder(StoreBase):
    def __init__(self) -> None:
        super().__init__()
        self._state: dict[str, Any] = {}

    def _path(self) -> Optional[Path]:
        return self._resolve_path("bw_learner", "message_recorder.json")

    def _load(self) -> None:
        if self._state:
            return
        obj = self._load_json_from_path_parts(
            "bw_learner", "message_recorder.json", default={"last_extraction_time": {}}
        )
        if isinstance(obj, dict):
            obj.setdefault("last_extraction_time", {})
            self._state = obj
            return
        self._state = {"last_extraction_time": {}}

    def _save(self) -> None:
        self._save_json_to_path_parts("bw_learner", "message_recorder.json", data=self._state)

    def get_last_time(self, chat_id: str) -> float:
        self._load()
        m = self._state.get("last_extraction_time", {})
        if not isinstance(m, dict):
            return 0.0
        try:
            return float(m.get(chat_id, 0.0) or 0.0)
        except Exception:
            return 0.0

    def set_last_time(self, chat_id: str, ts: float) -> None:
        self._load()
        m = self._state.setdefault("last_extraction_time", {})
        if not isinstance(m, dict):
            m = {}
            self._state["last_extraction_time"] = m
        m[str(chat_id)] = float(ts)
        self._save()


async def extract_and_learn(
    *,
    context,
    secrets: dict[str, Any],
    bot_name: str,
    chat_id: str,
    memory_store: MemoryStore,
    expr_store: ExpressionStore,
    jargon_store: Optional[JargonStore],
    recorder: MessageRecorder,
    personality: PersonalityConfig,
    min_interval_seconds: float = 60.0,
    min_messages: int = 10,
    self_reflect: bool = True,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
    extra_payload: dict[str, Any] | None = None,
) -> int:
    t0 = time.monotonic()
    try:
        context.logger.info(
            "xiaoqing_chat step=%s",
            json.dumps({"step": "bw.learn.start", "chat_id": chat_id}, ensure_ascii=False),
        )
    except Exception:
        pass
    recorder.bind(context.data_dir)
    last_ts = recorder.get_last_time(chat_id)
    now = time.time()
    if last_ts and now - last_ts < float(min_interval_seconds):
        try:
            context.logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {
                        "step": "bw.learn.skip.interval",
                        "chat_id": chat_id,
                        "elapsed_since_last_s": round(now - last_ts, 3),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return 0

    history = await memory_store.get_async(chat_id)
    window = [m for m in history if float(m.ts or 0.0) > last_ts]
    if len(window) < int(min_messages):
        try:
            context.logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {
                        "step": "bw.learn.skip.messages",
                        "chat_id": chat_id,
                        "window": len(window),
                        "min_messages": int(min_messages),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return 0

    try:
        context.logger.info(
            "xiaoqing_chat step=%s",
            json.dumps(
                {"step": "bw.learn.extract.start", "chat_id": chat_id, "window": len(window)},
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    learned = await learn_from_messages(
        http_session=context.http_session,
        secrets=secrets,
        bot_name=bot_name,
        chat_id=chat_id,
        personality=personality,
        messages=window[-80:],
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
        extra_payload=extra_payload,
    )
    if not learned:
        recorder.set_last_time(chat_id, now)
        try:
            context.logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {
                        "step": "bw.learn.extract.empty",
                        "chat_id": chat_id,
                        "elapsed_s": round(time.monotonic() - t0, 3),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
        return 0

    expr_store.bind(context.data_dir)
    try:
        context.logger.info(
            "xiaoqing_chat step=%s",
            json.dumps(
                {"step": "bw.learn.upsert.start", "chat_id": chat_id, "learned": len(learned)},
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    changed = await upsert_learned(
        store=expr_store,
        chat_id=chat_id,
        learned=learned,
        self_reflect=self_reflect,
        http_session=context.http_session,
        secrets=secrets,
        bot_name=bot_name,
        personality=personality,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retry=max_retry,
        retry_interval_seconds=retry_interval_seconds,
        proxy=proxy,
        endpoint_path=endpoint_path,
        extra_payload=extra_payload,
    )
    try:
        context.logger.info(
            "xiaoqing_chat step=%s",
            json.dumps(
                {"step": "bw.learn.upsert.done", "chat_id": chat_id, "changed": int(changed)},
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    if jargon_store is not None:
        jargon_store.bind(context.data_dir)
        try:
            context.logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {"step": "bw.jargon.mine.start", "chat_id": chat_id}, ensure_ascii=False
                ),
            )
        except Exception:
            pass
        changed += await mine_jargon(
            http_session=context.http_session,
            secrets=secrets,
            store=jargon_store,
            chat_id=chat_id,
            messages=window[-60:],
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
            proxy=proxy,
            endpoint_path=endpoint_path,
            extra_payload=extra_payload,
        )
        try:
            context.logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {
                        "step": "bw.jargon.mine.done",
                        "chat_id": chat_id,
                        "changed_total": int(changed),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
    recorder.set_last_time(chat_id, now)
    try:
        context.logger.info(
            "xiaoqing_chat step=%s",
            json.dumps(
                {
                    "step": "bw.learn.done",
                    "chat_id": chat_id,
                    "changed": int(changed),
                    "elapsed_s": round(time.monotonic() - t0, 3),
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass
    return changed

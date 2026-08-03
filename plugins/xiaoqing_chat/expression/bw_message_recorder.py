"""按会话推进表达学习水位，并防止同一会话重复抽取。

持久化水位表示“上一次已经纳入抽取快照的最晚墙钟时刻”，进程内活动集合则只负责
合并同一会话的并发后台任务。两者不能混用：前者需要跨重启保存，后者必须在任务结束时
无条件释放。
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from ..config.config import PersonalityConfig
from ..logging_utils import _log_step
from ..memory.memory import MemoryStore
from ..store_base import StoreBase
from .bw_expression_learner import learn_from_messages, upsert_learned
from .bw_expression_store import ExpressionStore
from .bw_jargon_miner import mine_jargon
from .bw_jargon_store import JargonStore


class MessageRecorder(StoreBase):
    """保存各会话的抽取水位，并提供无等待的进程内去重门。"""

    def __init__(self) -> None:
        super().__init__()
        self._state: dict[str, Any] = {}
        self._active_chats: set[str] = set()

    def bind(self, data_dir: Path) -> None:
        """切换数据根时丢弃旧根缓存；重复绑定同一路径不触发无谓重读。"""

        if self._data_dir != data_dir:
            self._state = {}
        super().bind(data_dir)

    def _load(self) -> None:
        if self._state:
            return
        obj = self._load_json_from_path_parts(
            "bw_learner", "message_recorder.json", default={"last_extraction_time": {}}
        )
        if isinstance(obj, dict):
            raw_times = obj.get("last_extraction_time")
            self._state = {
                "last_extraction_time": dict(raw_times) if isinstance(raw_times, dict) else {}
            }
            return
        self._state = {"last_extraction_time": {}}

    def _save(self) -> None:
        self._save_json_to_path_parts("bw_learner", "message_recorder.json", data=self._state)

    @staticmethod
    def _chat_key(chat_id: str) -> str:
        key = str(chat_id or "").strip()
        if not key:
            raise ValueError("chat_id must not be empty")
        return key

    def _last_times(self) -> dict[str, Any]:
        self._load()
        times = self._state.get("last_extraction_time")
        if isinstance(times, dict):
            return times
        times = {}
        self._state["last_extraction_time"] = times
        return times

    def get_last_time(self, chat_id: str) -> float:
        raw_value = self._last_times().get(self._chat_key(chat_id), 0.0)
        try:
            value = float(raw_value or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    def set_last_time(self, chat_id: str, ts: float) -> None:
        value = float(ts)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("extraction timestamp must be finite and non-negative")
        self._last_times()[self._chat_key(chat_id)] = value
        self._save()

    def try_begin(self, chat_id: str) -> bool:
        cid = self._chat_key(chat_id)
        if cid in self._active_chats:
            return False
        self._active_chats.add(cid)
        return True

    def end(self, chat_id: str) -> None:
        self._active_chats.discard(self._chat_key(chat_id))

    def clear(self, chat_id: str) -> None:
        """删除一个会话的抽取水位及进程内占用标记。"""

        key = self._chat_key(chat_id)
        times = self._last_times()
        times.pop(key, None)
        self._active_chats.discard(key)
        self._save()


async def extract_and_learn(
    *,
    context,
    secrets: dict[str, Any],
    bot_name: str,
    chat_id: str,
    memory_store: MemoryStore,
    expr_store: ExpressionStore,
    jargon_store: JargonStore | None,
    recorder: MessageRecorder,
    personality: PersonalityConfig,
    min_interval_seconds: float = 60.0,
    min_messages: int = 10,
    self_reflect: bool = True,
    max_store: int = 2000,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
) -> int:
    """抽取一个冻结的消息窗口，并把表达、黑话和水位作为同轮结果推进。"""

    t0 = time.monotonic()
    _log_step(context, None, chat_id=chat_id, step="bw.learn.start")
    if not recorder.try_begin(chat_id):
        _log_step(context, None, chat_id=chat_id, step="bw.learn.skip.inflight")
        return 0
    try:
        recorder.bind(context.data_dir)
        last_ts = recorder.get_last_time(chat_id)
        cutoff_ts = time.time()
        if last_ts > cutoff_ts:
            # 系统墙钟回拨后不能让未来水位永久封死该会话；回到零水位重新取快照。
            last_ts = 0.0
            recorder.set_last_time(chat_id, last_ts)
        if last_ts and cutoff_ts - last_ts < float(min_interval_seconds):
            _log_step(
                context,
                None,
                chat_id=chat_id,
                step="bw.learn.skip.interval",
                fields={"elapsed_since_last_s": round(cutoff_ts - last_ts, 3)},
            )
            return 0

        # 水位在 await 之前冻结；读取期间及模型调用期间新到的消息时间戳会大于它，
        # 因而留给下一轮，不会因为本轮耗时而被跨过去。
        history = await memory_store.get_async(chat_id)
        window = [m for m in history if float(m.ts or 0.0) > last_ts]
        if len(window) < int(min_messages):
            _log_step(
                context,
                None,
                chat_id=chat_id,
                step="bw.learn.skip.messages",
                fields={"window": len(window), "min_messages": int(min_messages)},
            )
            return 0

        _log_step(
            context,
            None,
            chat_id=chat_id,
            step="bw.learn.extract.start",
            fields={"window": len(window)},
        )
        learned = await learn_from_messages(
            secrets=secrets,
            messages=window[-80:],
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retry=max_retry,
            retry_interval_seconds=retry_interval_seconds,
        )
        if not learned:
            recorder.set_last_time(chat_id, cutoff_ts)
            _log_step(
                context,
                None,
                chat_id=chat_id,
                step="bw.learn.extract.empty",
                fields={"elapsed_s": round(time.monotonic() - t0, 3)},
            )
            return 0

        expr_store.bind(context.data_dir)
        _log_step(
            context,
            None,
            chat_id=chat_id,
            step="bw.learn.upsert.start",
            fields={"learned": len(learned)},
        )
        changed = int(
            await upsert_learned(
                store=expr_store,
                chat_id=chat_id,
                learned=learned,
                self_reflect=self_reflect,
                max_store=max_store,
                secrets=secrets,
                bot_name=bot_name,
                personality=personality,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                max_retry=max_retry,
                retry_interval_seconds=retry_interval_seconds,
            )
        )
        _log_step(
            context,
            None,
            chat_id=chat_id,
            step="bw.learn.upsert.done",
            fields={"changed": int(changed)},
        )

        if jargon_store is not None:
            jargon_store.bind(context.data_dir)
            _log_step(context, None, chat_id=chat_id, step="bw.jargon.mine.start")
            changed += int(
                await mine_jargon(
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
                )
            )
            _log_step(
                context,
                None,
                chat_id=chat_id,
                step="bw.jargon.mine.done",
                fields={"changed_total": int(changed)},
            )
        recorder.set_last_time(chat_id, cutoff_ts)
        _log_step(
            context,
            None,
            chat_id=chat_id,
            step="bw.learn.done",
            fields={
                "changed": int(changed),
                "elapsed_s": round(time.monotonic() - t0, 3),
            },
        )
        return changed
    finally:
        recorder.end(chat_id)

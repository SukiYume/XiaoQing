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
from ..llm.llm_config import LLMCallConfig
from ..logging_utils import log_step
from ..memory.memory import MemoryStore, StoredMessage
from ..store_base import StoreBase
from .bw_expression_learner import learn_from_messages, upsert_learned
from .bw_expression_store import ExpressionStore
from .bw_jargon_miner import mine_jargon
from .bw_jargon_store import JargonStore


class MessageRecorder(StoreBase):
    """保存各会话的抽取水位，并提供无等待的进程内去重门。"""

    def __init__(self) -> None:
        super().__init__()
        self._state: dict[str, Any]  = {}
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
            raw_times   = obj.get("last_extraction_time")
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
        times                               = {}
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

        key   = self._chat_key(chat_id)
        times = self._last_times()
        times.pop(key, None)
        self._active_chats.discard(key)
        self._save()


async def _learning_window(
    step,
    *,
    chat_id: str,
    memory_store: MemoryStore,
    recorder: MessageRecorder,
    min_interval_seconds: float,
    min_messages: int,
) -> tuple[list[StoredMessage], float] | None:
    """冻结本轮水位并取出水位之后的新消息；间隔或条数不足时返回 None。"""

    last_ts   = recorder.get_last_time(chat_id)
    cutoff_ts = time.time()
    if last_ts > cutoff_ts:
        # 系统墙钟回拨后不能让未来水位永久封死该会话；回到零水位重新取快照。
        last_ts = 0.0
        recorder.set_last_time(chat_id, last_ts)
    if last_ts and cutoff_ts - last_ts < float(min_interval_seconds):
        step("bw.learn.skip.interval", {"elapsed_since_last_s": round(cutoff_ts - last_ts, 3)})
        return None
    # 水位在 await 之前冻结；读取期间及模型调用期间新到的消息时间戳会大于它，
    # 因而留给下一轮，不会因为本轮耗时而被跨过去。
    history = await memory_store.get_async(chat_id)
    window  = [m for m in history if float(m.ts or 0.0) > last_ts]
    if len(window) < int(min_messages):
        step("bw.learn.skip.messages", {"window": len(window), "min_messages": int(min_messages)})
        return None
    return window, cutoff_ts


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
    min_messages: int           = 10,
    self_reflect: bool          = True,
    max_store: int              = 2000,
    llm: LLMCallConfig,
) -> int:
    """抽取一个冻结的消息窗口，并把表达、黑话和水位作为同轮结果推进。"""

    def step(name: str, fields: dict[str, Any] | None = None) -> None:
        log_step(context, None, chat_id=chat_id, step=name, fields=fields)

    t0 = time.monotonic()
    step("bw.learn.start")
    if not recorder.try_begin(chat_id):
        step("bw.learn.skip.inflight")
        return 0
    try:
        recorder.bind(context.data_dir)
        generation = recorder.generation(chat_id)
        frozen     = await _learning_window(
            step,
            chat_id              = chat_id,
            memory_store         = memory_store,
            recorder             = recorder,
            min_interval_seconds = min_interval_seconds,
            min_messages         = min_messages,
        )
        if frozen is None:
            return 0
        window, cutoff_ts = frozen

        step("bw.learn.extract.start", {"window": len(window)})
        learned = await learn_from_messages(secrets=secrets, messages=window[-80:], llm=llm)
        if not learned:
            step("bw.learn.extract.empty", {"elapsed_s": round(time.monotonic() - t0, 3)})

        expr_store.bind(context.data_dir)
        step("bw.learn.upsert.start", {"learned": len(learned)})
        changed = int(
            await upsert_learned(
                store               = expr_store,
                chat_id             = chat_id,
                learned             = learned,
                self_reflect        = self_reflect,
                max_store           = max_store,
                secrets             = secrets,
                bot_name            = bot_name,
                personality         = personality,
                llm                 = llm,
                expected_generation = generation,
            )
        )
        step("bw.learn.upsert.done", {"changed": changed})

        if jargon_store is not None:
            jargon_store.bind(context.data_dir)
            step("bw.jargon.mine.start")
            changed += int(
                await mine_jargon(
                    secrets             = secrets,
                    store               = jargon_store,
                    chat_id             = chat_id,
                    messages            = window[-60:],
                    llm                 = llm,
                    bot_name            = bot_name,
                    expected_generation = generation,
                )
            )
            step("bw.jargon.mine.done", {"changed_total": changed})
        with recorder.generation_guard(chat_id, generation) as current:
            if not current:
                return 0
            recorder.set_last_time(chat_id, cutoff_ts)
        step("bw.learn.done", {"changed": changed, "elapsed_s": round(time.monotonic() - t0, 3)})
        return changed
    finally:
        recorder.end(chat_id)

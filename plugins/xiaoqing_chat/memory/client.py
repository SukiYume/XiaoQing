"""经 Core 的 ``memory.*`` 服务读写小青的统一记忆（xiaoqing_memory）。

- 写：近期历史里还没推送的消息作为事件推给 ``memory.record``，推过的消息键记在 ``memory_outbox.json``；
  推送失败时留在本地，下一次持久化或定时同步时再推，记忆插件按幂等键去重。第一次推送某个会话时，
  这个会话的旧数据（人物事实、话题摘要、自我陈述、未完话头）一起导入。
- 读：``memory.recall`` 取这一轮的常驻块，``memory.search`` 查她自己的记录和旧事。
- 清：重置会话时让记忆插件清空这个范围。没得到它确认（超时、出错，或记忆服务不在）的会话记在
  ``memory_outbox.json`` 里；清掉之前不往这个范围推新消息，也不从它读（旧记忆不再进回复），下一次
  推送或定时同步时先重试。

能力缺失、插件关闭、超时或出错一律当作没有长期记忆：读取给空结果，写入留在本地队列里。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from core.atomic_store import AtomicJsonStore

from ..chat_runtime import optional_capability
from ..life.open_loops import OpenLoop
from ..life.self_timeline import SelfEntry
from ..message_parts import render_stored_message
from .memory import StoredMessage, is_conversation_reply
from .person_profile import get_profile_generation

logger = logging.getLogger("plugin.xiaoqing_chat")

RECALL_TIMEOUT   = 1.5
RECORD_TIMEOUT   = 8.0
SEARCH_TIMEOUT   = 2.0
_OUTBOX_FILE     = "memory_outbox.json"
_MAX_PUSHED_KEYS = 600
_MAX_EVENT_CHARS = 2000
_IMPORT_CHUNK    = 40
# 聊天自己发出的、agent 交回的回复算她自己的话；其他插件以她身份发的消息是工具输出。
_OWN_SOURCES = frozenset({"", "xiaoqing_chat", "xiaoqing_agent"})


def capability(context: Any) -> Any:
    return optional_capability(context, "memory")


# ───────────────────────────── 推送队列 ─────────────────────────────


class Outbox:
    """每个会话推送过的消息键（按重置代际）、哪些会话的旧数据已经导入、哪些会话的长期记忆还等着清。"""

    def __init__(self, data_dir: Path) -> None:
        self._store                       = AtomicJsonStore(data_dir / _OUTBOX_FILE)
        self._lock                        = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            raw: Any = self._store.read({}, raise_on_error=False)
            raw        = raw if isinstance(raw, dict) else {}
            chats      = raw.get("chats")
            migrated   = raw.get("migrated")
            forgetting = raw.get("forgetting")
            self._data = {
                "version"   : 1,
                "chats"     : dict(chats) if isinstance(chats, dict) else {},
                "migrated"  : dict(migrated) if isinstance(migrated, dict) else {},
                "forgetting": dict(forgetting) if isinstance(forgetting, dict) else {},
            }
        return self._data

    def pushed(self, chat_id: str, generation: str) -> set[str]:
        with self._lock:
            entry = self._load()["chats"].get(chat_id)
            if not isinstance(entry, dict) or entry.get("generation") != generation:
                return set()
            return {str(key) for key in entry.get("pushed") or ()}

    def mark(self, chat_id: str, generation: str, keys: Iterable[str]) -> None:
        with self._lock:
            chats = self._load()["chats"]
            entry = chats.get(chat_id)
            if not isinstance(entry, dict) or entry.get("generation") != generation:
                entry = {"generation": generation, "pushed": []}
            merged = list(dict.fromkeys([*entry.get("pushed", []), *keys]))[-_MAX_PUSHED_KEYS:]
            chats[chat_id] = {"generation": generation, "pushed": merged, "at": time.time()}
            self._store.write(self._load())

    def migrated(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._load()["migrated"]

    def mark_migrated(self, chat_id: str, count: int) -> None:
        with self._lock:
            self._load()["migrated"][chat_id] = {"at": time.time(), "count": count}
            self._store.write(self._load())

    def forget(self, chat_id: str) -> None:
        with self._lock:
            self._load()["chats"].pop(chat_id, None)
            self._store.write(self._load())

    def forgetting(self) -> list[str]:
        """长期记忆还没清成的会话。"""

        with self._lock:
            return sorted(self._load()["forgetting"])

    def mark_forgetting(self, chat_id: str) -> None:
        with self._lock:
            waiting = self._load()["forgetting"]
            if chat_id not in waiting:
                waiting[chat_id] = {"at": time.time()}
                self._store.write(self._load())

    def mark_cleared(self, chat_id: str) -> None:
        with self._lock:
            if self._load()["forgetting"].pop(chat_id, None) is not None:
                self._store.write(self._load())


_outboxes: dict[str, Outbox]                        = {}
_outbox_guard                                       = threading.Lock()
_push_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def outbox_for(data_dir: Path) -> Outbox:
    key = str(Path(data_dir).resolve())
    with _outbox_guard:
        box = _outboxes.get(key)
        if box is None:
            box = _outboxes[key] = Outbox(Path(data_dir))
        return box


def _push_lock(chat_id: str) -> asyncio.Lock:
    lock = _push_locks.get(chat_id)
    if lock is None:
        lock                 = asyncio.Lock()
        _push_locks[chat_id] = lock
    return lock


def event_key(generation: str, message: StoredMessage) -> str:
    """消息的幂等键：重置代际加本地编号；没有编号的旧记录用内容哈希。"""

    marker = hashlib.sha1(generation.encode("utf-8")).hexdigest()[:8] if generation else "0"
    if message.local_id:
        return f"chat:{marker}:{message.local_id}"
    digest = hashlib.sha1(
        f"{message.role}|{message.user_id}|{message.ts:.3f}|{message.content}".encode()
    ).hexdigest()[:12]
    return f"chat:{marker}:h{digest}"


def message_event(message: StoredMessage, key: str, bot_name: str) -> dict[str, Any] | None:
    text = render_stored_message(message).strip()[:_MAX_EVENT_CHARS]
    if not text:
        return None
    if message.role == "assistant":
        origin                = "self" if message.source_plugin in _OWN_SOURCES else "tool"
        actor: dict[str, Any] = {"role": "assistant", "name": bot_name}
    else:
        origin = "user"
        actor  = {"role": "user", "name": message.name}
        if message.user_id:
            actor["user_id"] = int(message.user_id)
    refs: dict[str, Any] = {"local_id": message.local_id}
    if message.remote_ids():
        refs["message_ids"] = list(message.remote_ids())
    if message.source_plugin:
        refs["source_plugin"] = message.source_plugin
    return {
        "kind"  : "message",
        "ts"    : float(message.ts),
        "actor" : actor,
        "origin": origin,
        "text"  : text,
        "refs"  : refs,
        "key"   : key,
    }


async def _call(
    context: Any, method: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any] | None:
    service = capability(context)
    if service is None:
        return None
    try:
        result = await asyncio.wait_for(getattr(service, method)(payload), timeout=timeout)
    except Exception as exc:
        logger.info("xiaoqing_chat memory.%s unavailable error_type=%s", method, type(exc).__name__)
        return None
    if not isinstance(result, dict) or result.get("error"):
        if isinstance(result, dict):
            logger.info("xiaoqing_chat memory.%s refused reason=%s", method, result.get("error"))
        return None
    return result


async def push_pending(
    context: Any,
    state: Any,
    chat_id: str,
    *,
    bot_name: str,
    ops: Sequence[dict[str, Any]] = (),
) -> dict[str, Any] | None:
    """把这个会话还没推送的消息（以及要一起提交的笔记操作）交给记忆插件。

    事件在前、操作在后，操作引用的消息一定已经写入。第一次推送某个会话时一起导入它的旧数据。
    """

    if capability(context) is None:
        return None
    data_dir = Path(context.data_dir)
    box      = outbox_for(data_dir)
    async with _push_lock(chat_id):
        if chat_id in box.forgetting() and not await _forget_scope(context, box, chat_id):
            return None
        generation = await asyncio.to_thread(get_profile_generation, data_dir, chat_id)
        history = await state.memory_store.get_async(chat_id)
        pushed = box.pushed(chat_id, generation)
        events: list[dict[str, Any]] = []
        keys  : list[str] = []
        for message in history:
            key = event_key(generation, message)
            if key in pushed:
                continue
            event = message_event(message, key, bot_name)
            keys.append(key)
            if event is not None:
                events.append(event)
        if not box.migrated(chat_id) and not await _migrate(context, box, data_dir, chat_id):
            return None
        if not events and not ops:
            return None
        result = await _call(
            context,
            "record",
            {"scope": chat_id, "events": events, "ops": list(ops)},
            RECORD_TIMEOUT,
        )
        if result is None:
            return None
        if keys:
            await asyncio.to_thread(box.mark, chat_id, generation, keys)
        return result


async def _migrate(context: Any, box: Outbox, data_dir: Path, chat_id: str) -> bool:
    """这个会话的旧数据分批导入；全部成功才记为已导入，重复导入由记忆插件按来源键去重。"""

    from .legacy import legacy_ops

    imports = await asyncio.to_thread(legacy_ops, data_dir, chat_id)
    for start in range(0, len(imports), _IMPORT_CHUNK):
        chunk  = imports[start : start + _IMPORT_CHUNK]
        result = await _call(context, "record", {"scope": chat_id, "ops": chunk}, RECORD_TIMEOUT)
        if result is None:
            return False
    await asyncio.to_thread(box.mark_migrated, chat_id, len(imports))
    if imports:
        logger.info("xiaoqing_chat memory.migrated chat=%s ops=%d", chat_id[:1], len(imports))
    return True


async def sync_all(context: Any, state: Any, *, bot_name: str) -> int:
    """把磁盘上每个会话的近期历史都推一遍（首次启动导入种子、补推失败的部分）。"""

    if capability(context) is None:
        return 0
    data_dir = Path(context.data_dir)
    box      = outbox_for(data_dir)
    # 重置后还没有新消息的会话没有历史文件，下面的循环碰不到它：等着清的先各试一次。
    for chat_id in box.forgetting():
        async with _push_lock(chat_id):
            await _forget_scope(context, box, chat_id)
    pushed = 0
    for path in sorted(data_dir.glob("[gu][0-9]*.json")):
        chat_id = path.stem
        if not chat_id[1:].isdigit():
            continue
        if await push_pending(context, state, chat_id, bot_name=bot_name) is not None:
            pushed += 1
    return pushed


async def _forget_scope(context: Any, box: Outbox, chat_id: str) -> bool:
    """让记忆插件清空这个范围；没得到它的确认就记下待清。调用方持有这个会话的推送锁。"""

    cleared = (
        await _call(context, "forget", {"scope": chat_id, "all": True}, RECORD_TIMEOUT) is not None
    )
    await asyncio.to_thread(box.mark_cleared if cleared else box.mark_forgetting, chat_id)
    return cleared


async def forget_chat(context: Any, chat_id: str) -> bool:
    """重置会话时清掉它的长期记忆，返回清没清成。

    只有记忆服务确认清掉才算清成。服务不在、超时或出错时聊天无从知道那边还有没有这个会话的
    东西（agent 办事也会往里记），一律记下待清、返回没清成。
    """

    box = outbox_for(Path(context.data_dir))
    async with _push_lock(chat_id):
        await asyncio.to_thread(box.forget, chat_id)
        return await _forget_scope(context, box, chat_id)


def _awaiting_forget(context: Any, chat_id: str) -> bool:
    """这个会话的长期记忆还等着清：这期间读到的都是该清掉的旧内容，一律当作没有。"""

    # 没有记忆服务时本来就读不到东西，不必看待清记录。
    if capability(context) is None:
        return False
    return chat_id in outbox_for(Path(context.data_dir)).forgetting()


# ───────────────────────────── 取用 ─────────────────────────────


def _self_entry(raw: Mapping[str, Any]) -> SelfEntry | None:
    try:
        return SelfEntry(
            entry_id    = str(raw.get("id") or ""),
            ts          = float(raw.get("ts") or 0.0),
            chat_id     = str(raw.get("chat_id") or ""),
            kind        = str(raw.get("kind") or ""),
            text        = str(raw.get("text") or ""),
            expires_at  = float(raw.get("expires_at") or 0.0),
            status      = str(raw.get("status") or "active"),
            source      = str(raw.get("source") or "chat"),
            counterpart = str(raw.get("counterpart") or ""),
            shared      = bool(raw.get("shared")),
        )
    except (TypeError, ValueError):
        return None


def _entries(value: Any) -> tuple[SelfEntry, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        entry for item in value if isinstance(item, Mapping) and (entry := _self_entry(item))
    )


def open_loop(raw: Mapping[str, Any]) -> OpenLoop | None:
    try:
        return OpenLoop(
            loop_id    = str(raw.get("id") or ""),
            user_id    = int(raw.get("user_id") or 0),
            user_name  = str(raw.get("user_name") or ""),
            title      = str(raw.get("title") or ""),
            due_at     = float(raw.get("due_at") or 0.0),
            created_at = float(raw.get("created_at") or 0.0),
            status     = str(raw.get("status") or "open"),
            asked_at   = float(raw.get("asked_at") or 0.0),
            surfaced   = int(raw.get("surfaced") or 0),
        )
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SelfLine:
    """她自我页里的一行，和它引用的网上生活条目在 xiaoqing_life 里的键。"""

    text     : str
    life_keys: frozenset[str] = frozenset()
    # 只引用了网上生活条目（没有她在聊天里说过的话之类的别的依据）。
    life_only: bool = False


@dataclass(frozen=True)
class Recalled:
    """一次取用的结果；记忆不可用时是空的。"""

    blocks      : dict[str, str] = field(default_factory=dict)
    self_entries: tuple[SelfEntry, ...]       = ()
    stories     : tuple[SelfEntry, ...]       = ()
    self_history: tuple[SelfEntry, ...]       = ()
    due         : tuple[OpenLoop, str] | None = None
    # 群里这两三天的话题词（只有词），转交给网上生活当上网的方向。
    topics     : tuple[dict[str, Any], ...] = ()
    self_header: str                        = ""
    self_lines : tuple[SelfLine, ...]       = ()
    available  : bool                       = False

    def block(self, name: str) -> str:
        return str(self.blocks.get(name) or "").strip()

    def self_page(self, shown: Iterable[str] = ()) -> str:
        """她的自我页；``shown`` 是网上生活这一轮已经交出细节的条目键。

        自我页一行常概括好几条经历；其中一条的细节这一轮已经给了，这一行就先不放，免得同一件事
        一详一略说两遍。另有依据的行和一句话简介照常保留。
        """

        if not self.self_lines:
            return self.block("self")
        given = set(shown)
        kept  = [
            line.text for line in self.self_lines if not (line.life_only and line.life_keys & given)
        ]
        return "\n".join([self.self_header, *kept]) if kept else ""


EMPTY_RECALL = Recalled()


def parse_recall(result: Mapping[str, Any] | None) -> Recalled:
    if not isinstance(result, Mapping):
        return EMPTY_RECALL
    blocks_raw = result.get("blocks")
    blocks     = (
        {str(name): str(value or "") for name, value in blocks_raw.items()}
        if isinstance(blocks_raw, Mapping)
        else {}
    )
    due_raw                          = result.get("due")
    due: tuple[OpenLoop, str] | None = None
    if isinstance(due_raw, Mapping) and due_raw.get("mode") in ("ask", "review"):
        loop = open_loop(due_raw)
        if loop is not None and loop.loop_id:
            due = (loop, str(due_raw.get("mode")))
    return Recalled(
        blocks       = blocks,
        self_entries = _entries(result.get("self_entries")),
        stories      = _entries(result.get("stories")),
        self_history = _entries(result.get("self_history")),
        due          = due,
        topics       = _topics(result.get("topics")),
        self_header  = str(result.get("self_header") or ""),
        self_lines   = _self_lines(result.get("self_lines")),
        available    = True,
    )


def _self_lines(value: Any) -> tuple[SelfLine, ...]:
    rows = []
    for row in value if isinstance(value, list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get("text"), str):
            continue
        keys = row.get("life_keys")
        rows.append(
            SelfLine(
                text      = row["text"],
                life_keys = frozenset(str(key) for key in keys if isinstance(key, str))
                if isinstance(keys, list)
                else frozenset(),
                life_only=row.get("life_only") is True,
            )
        )
    return tuple(rows)


def _topics(value: Any) -> tuple[dict[str, Any], ...]:
    rows = []
    for row in value if isinstance(value, list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get("topic"), str):
            continue
        words = row.get("keywords")
        rows.append(
            {
                "topic"   : row["topic"][:12],
                "keywords": [w[:8] for w in words if isinstance(w, str)][:3]
                if isinstance(words, list)
                else [],
            }
        )
    return tuple(rows[:3])


def _recent_texts(history: Sequence[StoredMessage]) -> tuple[list[str], list[str]]:
    recent = [render_stored_message(message).strip() for message in history[-30:]]
    bot    = [
        render_stored_message(message).strip()
        for message in history[-12:]
        if is_conversation_reply(message)
    ][-6:]
    return [text for text in recent if text], [text for text in bot if text]


async def recall(
    context: Any,
    *,
    purpose: str,
    chat_id: str,
    speaker_id: int | None           = None,
    speaker_name: str                = "",
    text: str                        = "",
    query: str                       = "",
    history: Sequence[StoredMessage] = (),
    past_days: float                 = 0.0,
    followup_hours: float            = 36.0,
    self_limit: int                  = 8,
) -> Recalled:
    recent, bot_recent = _recent_texts(history)
    payload: dict[str, Any] = {
        "purpose"       : purpose,
        "scope"         : chat_id,
        "text"          : text[:600],
        "query"         : query[:300],
        "recent"        : recent,
        "bot_recent"    : bot_recent,
        "past_days"     : past_days,
        "followup_hours": followup_hours,
        "self_limit"    : self_limit,
    }
    if speaker_id:
        payload["speaker"] = {"user_id": int(speaker_id), "name": speaker_name[:32]}
    if _awaiting_forget(context, chat_id):
        return EMPTY_RECALL
    return parse_recall(await _call(context, "recall", payload, RECALL_TIMEOUT))


async def search(context: Any, chat_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if _awaiting_forget(context, chat_id):
        return []
    result = await _call(context, "search", {**payload, "scope": chat_id}, SEARCH_TIMEOUT)
    items  = result.get("items") if isinstance(result, dict) else None
    return [item for item in items or () if isinstance(item, dict)]


async def self_records(context: Any, chat_id: str) -> tuple[list[SelfEntry], list[OpenLoop]]:
    """抽取任务要对照的已有记录：此会话可见的有效自我记录（含往事）和还没结束的未完话头。"""

    items = await search(
        context,
        chat_id,
        {
            "kinds": ["self.state", "self.plan", "self.promise", "self.story", "person.loop"],
            "limit": 50,
        },
    )
    entries = [
        entry
        for item in items
        if (entry := _self_entry(item.get("entry") or {})) and entry.entry_id
    ]
    loops = [loop for item in items if (loop := open_loop(item.get("loop") or {})) and loop.loop_id]
    return entries, loops

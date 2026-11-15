"""角色自己的一天：每天早上安排几件生活小事，到点之后她才“经历过”。

这些小事按人设卡和〔作息〕生成，分布在她醒着的时段：课上的插曲、吃了什么、社团和宿舍里
的事。每件除了一句话，还带一句细节（是哪样东西、怎么回事、结果怎样），她说起这件事或被追问
时照着说。回复时只交给模型已经过了时间点的那几件，所以她不会提前知道下午发生什么；同一天里
各会话看到的是同一份，她在群里和私聊里说的今天是一致的。被问到昨天、前几天时，再把那几天
的小事和她当时说过的近况交给模型。

小事只是人物的日常创作，不涉及真实群友，也不带需要兑现的承诺；回复审查把它们当作人物资料。
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from core.clock import WEEKDAY_NAMES

from ..llm.gateway import LLMError, chat_completions
from ..llm.llm_config import LLMCallConfig
from ..store_base import StoreBase, coerce_finite_float
from ..utils.json_parsing import parse_first_json_object
from ..utils.wall_clock import local_datetime
from .self_timeline import SelfEntry

_KEEP_DAYS        = 14
_MIN_EVENTS       = 2
_MAX_EVENTS       = 6
_MAX_EVENT_CHARS  = 40
_MAX_DETAIL_CHARS = 80
# 生成今天的事时给模型看前几天的，免得天天是同一类事。
_AVOID_DAYS = 4
_CLOCK_RE   = re.compile(r"^(\d{1,2}):(\d{2})$")
# 对方在问过去的事时才交出前几天的生活。
_PAST_REFERENCE_RE = re.compile(
    r"昨天|昨晚|前天|前晚|上周|上礼拜|上星期|那天|前几天|这几天|最近|上次|周末|之前|这周"
)
_generation_lock = asyncio.Lock()
# 生成失败后隔一阵再试，模型不可用时不会每条消息都去试一次。
_RETRY_AFTER_FAILURE_SECONDS    = 1800.0
_last_failure: dict[str, float] = {}

_SYSTEM = """你在为一个聊天角色安排她“今天”会经历的几件生活小事，让她聊天时有自己的生活可说。只输出一个 JSON 对象。
- 写 3 到 5 件，分布在她醒着的时段，时间和作息对得上：忙的时段发生的就是手头那件事里的插曲，睡觉时段不安排。
- 都是普通、具体、可信、合乎她身份和兴趣的日常。几件事来自她生活的不同方面：课上和实验里的事，食堂、宿舍和路上的事，室友同学说的话做的事，社团的事，在追的番、在玩的游戏、在听的歌，家里发来的消息和照片。一天里同一方面最多一件。
- 其中一两件是她会想跟朋友说一句的：好笑的、倒霉的、意外的，或者让她高兴的。
- 不写大事件、生病受伤、恋爱和金钱纠纷；不写和网上群友的互动；不引入有名有姓的新人物，身边的人用泛称；不写天气（真实天气她会自己看到）。
- 不写需要之后兑现的承诺。不和前几天重样：那几天有过的同一类事、同一道菜、同一种出错，今天换成别的。
- 每件一句话，省略主语，不超过 30 字。
- 每件再写一句 detail：这件事里只有当事人说得出的具体细节，是哪一样东西、怎么回事、结果怎样。她跟人说起这件事时讲的就是它，别人顺口问“然后呢”“哪门课的”也照它答。不超过 50 字。
格式：{"events":[{"at":"08:20","text":"出门前找了半天钥匙","detail":"最后在昨天那件外套口袋里摸到的，早八踩着铃进的教室"}]}"""

_ONLINE_LIFE_RULE = "\n- 她上网刷到的具体视频、帖子和热点另有真实记录，这里不写具体的视频或帖子。"


@dataclass(frozen=True)
class DayEvent:
    minute: int
    text  : str
    # 这件事的具体细节；记录里没有时为空。
    detail: str = ""

    @property
    def clock(self) -> str:
        return f"{self.minute // 60:02d}:{self.minute % 60:02d}"


@dataclass(frozen=True)
class DayRecord:
    day         : str
    events      : tuple[DayEvent, ...]
    generated_at: float = 0.0

    def happened(self, minute_of_day: int) -> tuple[DayEvent, ...]:
        return tuple(event for event in self.events if event.minute <= minute_of_day)


def day_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def _parse_events(raw: Any) -> tuple[DayEvent, ...]:
    events: list[DayEvent] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        match = _CLOCK_RE.match(str(item.get("at") or "").strip())
        text  = " ".join(str(item.get("text") or "").split())[:_MAX_EVENT_CHARS]
        if match is None or not text:
            continue
        hours, minutes = int(match.group(1)), int(match.group(2))
        if hours > 23 or minutes > 59:
            continue
        detail = " ".join(str(item.get("detail") or "").split())[:_MAX_DETAIL_CHARS]
        events.append(DayEvent(hours * 60 + minutes, text, detail))
    events.sort(key=lambda event: event.minute)
    return tuple(events[:_MAX_EVENTS])


class DailyLifeStore(StoreBase):
    """每天一个文件，保留最近两周。"""

    def __init__(self) -> None:
        super().__init__()
        self._lock                               = threading.Lock()
        self._cache: dict[str, DayRecord | None] = {}

    def _drop_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def get(self, day: str) -> DayRecord | None:
        with self._lock:
            if day not in self._cache:
                raw = self._load_json_from_path_parts("life", "days", f"{day}.json", default=None)
                self._cache[day] = (
                    DayRecord(
                        day          = day,
                        events       = _parse_events(raw.get("events")),
                        generated_at = coerce_finite_float(
                            raw.get("generated_at"), default=0.0, minimum=0.0
                        ),
                    )
                    # 读不到文件时存储层给空对象；没有小事的记录等于今天还没安排。
                    if isinstance(raw, dict) and _parse_events(raw.get("events"))
                    else None
                )
            return self._cache[day]

    def save(self, record: DayRecord, *, now: datetime) -> None:
        with self._lock:
            self._cache[record.day] = record
            self._save_json_to_path_parts(
                "life",
                "days",
                f"{record.day}.json",
                data={
                    "day"         : record.day,
                    "generated_at": record.generated_at,
                    "events"      : [
                        {"at": e.clock, "text": e.text, "detail": e.detail} for e in record.events
                    ],
                },
            )
            folder = self._resolve_path("life", "days")
            cutoff = day_key(now - timedelta(days=_KEEP_DAYS))
            for path in folder.glob("*.json") if folder is not None and folder.is_dir() else ():
                if path.stem < cutoff:
                    path.unlink(missing_ok=True)
                    self._cache.pop(path.stem, None)


def build_messages(
    *,
    persona: str,
    routine: Sequence[str],
    moment: datetime,
    earlier: Sequence[DayRecord | None] = (),
    online_life: bool                   = False,
) -> list[dict[str, str]]:
    """``earlier`` 是前几天的记录（新的在前）：今天的事不和它们重样。"""

    past = [
        f"- {record.day[5:]}：" + "；".join(event.text for event in record.events)
        for record in earlier
        if record is not None and record.events
    ]
    user = "\n".join(
        [
            f"今天：{moment:%Y-%m-%d}（周{WEEKDAY_NAMES[moment.weekday()]}）",
            "作息：" + ("；".join(routine) if routine else "（未写，按人设推一个合理的作息）"),
            "前几天发生过的事：" + ("\n" + "\n".join(past) if past else "（没有记录）"),
        ]
    )
    return [
        {
            "role"   : "system",
            "content": f"{persona}\n\n{_SYSTEM}{_ONLINE_LIFE_RULE if online_life else ''}",
        },
        {"role": "user", "content": user},
    ]


async def ensure_today(
    *,
    store: DailyLifeStore,
    persona: str,
    routine: Sequence[str],
    secrets: dict[str, Any],
    llm: LLMCallConfig,
    now: float | None = None,
    online_life: bool = False,
) -> DayRecord | None:
    """今天的小事还没安排时生成一次；已经有了直接返回。生成失败时今天就没有小事。"""

    moment = local_datetime(now)
    day    = day_key(moment)
    if (record := await asyncio.to_thread(store.get, day)) is not None:
        return record
    if secrets.get("_ai") is None:
        return None
    if moment.timestamp() - _last_failure.get(day, 0.0) < _RETRY_AFTER_FAILURE_SECONDS:
        return None
    async with _generation_lock:
        if (record := await asyncio.to_thread(store.get, day)) is not None:
            return record
        earlier = await asyncio.to_thread(recent_days, store, moment, count=_AVOID_DAYS)
        try:
            raw = await chat_completions(
                route    = "reasoning",
                secrets  = secrets,
                messages = build_messages(
                    persona     = persona,
                    routine     = routine,
                    moment      = moment,
                    earlier     = earlier,
                    online_life = online_life,
                ),
                **llm.as_kwargs(),
            )
        except LLMError:
            _last_failure[day] = moment.timestamp()
            return None
        events = _parse_events((parse_first_json_object(raw) or {}).get("events"))
        if len(events) < _MIN_EVENTS:
            _last_failure[day] = moment.timestamp()
            return None
        record = DayRecord(day=day, events=events, generated_at=moment.timestamp())
        await asyncio.to_thread(store.save, record, now=moment)
        return record


_TODAY_HEADER = (
    "今天你身上已经发生的小事（你自己的生活。话题相关、或别人问你今天怎么样时自然提起，"
    "不用每次都说，也不要一次全讲；括号里是这件事的细节，说起它或被追问时照着说）："
)


def today_block(record: DayRecord | None, moment: datetime, *, header: str = _TODAY_HEADER) -> str:
    """今天已经过了时间点的小事和各自的细节；还没到点的不交出去。"""

    if record is None or record.day != day_key(moment):
        return ""
    happened = record.happened(moment.hour * 60 + moment.minute)
    if not happened:
        return ""
    lines = "\n".join(
        f"- {event.clock} {event.text}" + (f"（{event.detail}）" if event.detail else "")
        for event in happened
    )
    return f"{header}\n{lines}"


def refers_to_past(text: str) -> bool:
    return _PAST_REFERENCE_RE.search(str(text or "")) is not None


def past_block(
    days: Sequence[DayRecord | None], entries: Sequence[SelfEntry], moment: datetime
) -> str:
    """前几天的生活：那几天的小事，加上她那几天说过的近况和打算。"""

    today                        = day_key(moment)
    by_day: dict[str, list[str]] = {}
    for record in days:
        if record is not None and record.day < today:
            by_day.setdefault(record.day, []).extend(event.text for event in record.events)
    for entry in entries:
        day = day_key(local_datetime(entry.ts))
        if day < today and entry.kind in {"state", "plan"} and entry.status != "dropped":
            prefix = "说打算" if entry.kind == "plan" else "说过"
            by_day.setdefault(day, []).append(f"{prefix}{entry.text}")
    if not by_day:
        return ""
    lines = []
    for day in sorted(by_day)[-3:]:
        moment_of_day = datetime.strptime(day, "%Y-%m-%d")
        label = f"{moment_of_day.month}月{moment_of_day.day}日（周{WEEKDAY_NAMES[moment_of_day.weekday()]}）"
        lines.append(f"- {label}：" + "；".join(dict.fromkeys(by_day[day])))
    return "前几天你的生活（对方问起或聊到时照这个说，记不清的部分就说记不清）：\n" + "\n".join(
        lines
    )


def recent_days(
    store: DailyLifeStore, moment: datetime, *, count: int = 3
) -> list[DayRecord | None]:
    return [store.get(day_key(moment - timedelta(days=offset))) for offset in range(1, count + 1)]

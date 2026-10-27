"""候选被拒后的处理：按拒绝原因选择定向重生成提示，或在明确召唤时给出安全兜底。"""

from __future__ import annotations

import re

from .llm.check_result import ReplyRejected
from .llm.local_checks import (
    heuristic_check,
    requires_configured_profile_boundary,
)
from .llm.media_evidence import (
    INBOUND_MEDIA_MARKER_RE,
    event_has_current_image,
    event_media_all_unreadable,
)
from .logging_utils import log_step
from .persona import persona_subject_pattern
from .reply_draft import RejectedCandidate, ReplyAttemptState, ReplyDraft, build_reply_draft
from .reply_plan import (
    QUESTION_FORM_RE,
    ReplyGenerationPlan,
    allows_low_stakes_persona_fiction,
    needs_persona_guard_when_checker_unavailable,
)

_KNOWN_NAME_QUERY_RE = re.compile(r"你(?:到底|究竟)?(?:是)?(?:谁|哪位)")


_PERSONA_INTRO_QUERY_TAIL = (
    r"[^，。！？；;\n]{0,24}"
    r"(?:什么样的人|介绍(?:一下)?自己|自我介绍|说说你自己|你的性格)"
)


# 只在明确点名且重生成仍无法安全回答未知履历时使用。这些表达不补造人物事实，
# 也不反问用户为什么问，避免把正常聊天推回机械的审讯式承接。
PERSONA_GROUNDING_FALLBACKS = (
    "这个嘛……我也说不好",
    "这个先跳过",
    "这个我真说不上来",
)


def _is_persona_intro_query(text: str, *, bot_name: str) -> bool:
    return bool(
        re.search(
            persona_subject_pattern(bot_name) + _PERSONA_INTRO_QUERY_TAIL,
            str(text or ""),
        )
    )


CONTEXT_GROUNDING_FALLBACKS = ("等等，我好像没跟上你们说的是哪件事",)


_PUBLIC_IDENTITY_FALLBACK_MAX_CHARS = 120


# 兜底只在重生成耗尽时出现，仍保持人物口吻，不暴露审查、配置或重试流程。
NOT_READY_FALLBACK = "等我一下，脑子突然卡壳了"
PRESENCE_FALLBACK  = "我在"
UNSURE_FALLBACK    = "这个我真拿不准，不瞎说了"
UNSURE_ALTERNATE   = "这个我还是说不准，先不乱讲"
# 图确实没读出来才说加载问题。读出来了就不能再说没看清：只是发图就说看到了，
# 被问到具体内容又答不稳时，只承认这一点不敢肯定。
MEDIA_UNREADABLE_FALLBACKS = ("图我这边没刷出来", "图好像裂了，我这边打不开")
MEDIA_SEEN_FALLBACKS       = ("看到了", "嗯，看到了")
MEDIA_UNSURE_FALLBACKS     = ("这个我不敢打包票，先不乱说", "这点我说不准，先不乱讲")

# 质量评估据此识别“审查耗尽后才发出”的回复。
ALL_FALLBACK_TEXTS = frozenset(
    (
        *PERSONA_GROUNDING_FALLBACKS,
        *CONTEXT_GROUNDING_FALLBACKS,
        NOT_READY_FALLBACK,
        PRESENCE_FALLBACK,
        UNSURE_FALLBACK,
        UNSURE_ALTERNATE,
        *MEDIA_UNREADABLE_FALLBACKS,
        *MEDIA_SEEN_FALLBACKS,
        *MEDIA_UNSURE_FALLBACKS,
    )
)


def _concise_public_identity(identity: str) -> str:
    """只提取可公开的人物简介开头，绝不带出内部策略说明。"""

    lead = re.split(r"[；;\n]", str(identity or "").strip(), maxsplit=1)[0].strip()
    # 配置使用第二人称描述机器人；公开简介的分句主语统一转为第一人称。
    lead = re.sub(r"(^|[。！？!?，,]\s*)你(?!们)", r"\1我", lead)
    if len(lead) <= _PUBLIC_IDENTITY_FALLBACK_MAX_CHARS:
        return lead.rstrip("。")
    sentences           = re.findall(r"[^。！？!?]+[。！？!?]?", lead)
    selected: list[str] = []
    length              = 0
    for sentence in sentences:
        cleaned = sentence.strip()
        if not cleaned or length + len(cleaned) > _PUBLIC_IDENTITY_FALLBACK_MAX_CHARS:
            break
        selected.append(cleaned)
        length += len(cleaned)
    if selected:
        return "".join(selected).rstrip("。")
    return lead[:_PUBLIC_IDENTITY_FALLBACK_MAX_CHARS].rstrip("，,、；;。 ")


def queue_reply_regeneration(
    plan: ReplyGenerationPlan,
    attempt: ReplyAttemptState,
    rejected: RejectedCandidate,
    *,
    step: str,
) -> bool:
    if rejected.result.failure_code == "checker_unavailable":
        # 审查预算已经耗尽，重生成不会恢复检查能力。
        return False
    if attempt.regen_used >= max(0, int(plan.runtime.cfg.reply_check.max_regen)):
        return False
    attempt.regen_used += 1
    correction = (
        "涉及角色、用户、第三方或早先对话的无依据信息时，不要用另一项肯定、否定、"
        "习惯、愿望、动机或背景替换原陈述；只承接可见输入明确支持的内容，并保留"
        "来源与不确定性。"
    )
    # 没照用户明确要求做（比如篇幅不够）按要求改，不套用“短句留白”的语气修正。
    follows_request = rejected.result.failure_code == "instruction_following"
    if follows_request:
        correction = (
            "重新读取最新用户消息对本轮表达方式的明确要求，并在内容和语气上都遵守；"
            "不要用善意、安慰、玩笑或话题推进覆盖用户刚说的边界。"
        )
    elif not rejected.result.is_hard:
        correction = (
            "本轮调整只针对审核指出的语气、节奏或措辞问题。保留贴切的观点、笑点、"
            "人设和已确认内容，用更自然的群友口吻接住当前话题；短句和留白都可以。"
        )
    elif rejected.result.failure_code == "persona_grounding":
        persona_question = needs_persona_guard_when_checker_unavailable(
            plan.text,
            bot_name=plan.bot_name,
        )
        if requires_configured_profile_boundary(
            plan.text,
            plan.effective_identity,
            bot_name=plan.bot_name,
        ):
            correction = (
                "这些精确身份资料不在群里透露。像普通网友那样笑着带过即可，不编具体答案，"
                "也可以接着聊已经确定的身份或兴趣。语气保持普通群友的口吻。"
            )
        elif allows_low_stakes_persona_fiction(plan.runtime) and persona_question:
            correction = (
                "保留稳定人设和可靠记忆；若要讲自己的故事，只能改成普通、低风险、"
                "不可核验且与人设一致的日常小片段。删除精确身份、现实关系、重大经历、"
                "现实承诺以及拿个人故事给外部事实作证的内容。"
                "如果用户问的是普通知识、事情本身或当下看法，就直接回答，不要硬塞个人故事。"
            )
        elif allows_low_stakes_persona_fiction(plan.runtime):
            correction = (
                "当前用户问的不是你的履历或生活故事。删除第一人称经历、关系、习惯、"
                "现实日程和自我举例，只针对用户当前说的具体内容直接表态、接梗或回答；"
                "不要把句首称呼误当成人物资料提问。"
            )
        else:
            correction = (
                "删除所有受控人物资料没有支持的个人经历、现实关系、长期履历、现实日程、"
                "线下义务和当前活动。"
                "如果用户问的是普通知识、事情本身或当下看法，就直接回答那个问题，不要把"
                "句首称呼误当成人物资料提问；开放话题可改成当下观点或条件式设想；"
                "如果确实问到未知履历，简短承认说不准即可。"
            )
    elif rejected.result.failure_code == "context_grounding":
        correction = (
            "先用本群已给出的词义还原当前事件，保留每个动作的主体、时长、先后和完成状态。"
            "逐项核对玩笑里的现实前提，物品种类、数量、天气、人物关系和事情进展以可见资料为准。"
            "已知事实保留来源与确定性，新增表达可以是态度、比喻形象或用户邀请的条件式推测。"
            "接住当前可理解的内容，只对影响回答的具体疑点作简短说明。"
        )
    attempt.extra_check_hint = (
        f'上一条候选回复"{rejected.text}"被检查拒绝:{rejected.result.reason}。\n'
        f"请修正上面指出的具体问题。{correction}"
        + ("" if follows_request else "回复集中于最新话题，给其他群友留出接话空间。")
    ).strip()
    log_step(
        plan.context,
        plan.runtime,
        chat_id = plan.chat_id,
        step    = step,
        fields  = {"regen_used": attempt.regen_used},
    )
    return True


_SILENT_FAILURE_CODES = frozenset(
    {"persona_grounding", "context_grounding", "instruction_following"}
)


def _fallback_draft(rejected: RejectedCandidate, text: str) -> ReplyDraft | None:
    return build_reply_draft((text,), raw_text=rejected.text, rewritten_text=text)


def _persona_fallback_texts(plan: ReplyGenerationPlan) -> tuple[str, ...]:
    bot_name = plan.bot_name.strip()
    text     = str(plan.text or "")
    if bot_name and _KNOWN_NAME_QUERY_RE.search(text):
        return (f"我叫{bot_name}。",)
    if requires_configured_profile_boundary(text, plan.effective_identity, bot_name=bot_name):
        return ("这个就不说了", "秘密", "不告诉你")
    if _is_persona_intro_query(text, bot_name=bot_name):
        public_identity = _concise_public_identity(plan.effective_identity.strip().rstrip("。"))
        return (
            f"{public_identity}。别的慢慢聊就知道了"
            if public_identity
            else "这个嘛，慢慢聊就知道了",
        )
    # 按请求稳定轮换起点，连续几轮被拦时不会反复说同一句。
    count = len(PERSONA_GROUNDING_FALLBACKS)
    start = sum(ord(char) for char in str(plan.request_id or plan.text)) % count
    return tuple(PERSONA_GROUNDING_FALLBACKS[(start + offset) % count] for offset in range(count))


def _asks_question(plan: ReplyGenerationPlan) -> bool:
    # 只看用户自己说的话；图上转写的文字里有“什么”“几点”不代表用户在问。
    return bool(QUESTION_FORM_RE.search(INBOUND_MEDIA_MARKER_RE.sub("", str(plan.text or ""))))


def _forced_fallback_texts(
    plan: ReplyGenerationPlan, failure_code: str
) -> tuple[tuple[str, ...], str]:
    """按拒绝原因挑一组兜底句，返回 (候选句, 日志步骤名)。"""
    step          = "reply.check.exhausted.forced_fallback"
    current_image = isinstance(plan.event, dict) and event_has_current_image(plan.event)
    # 这轮发了图时，接不上上下文的兜底（“没跟上你们说的是哪件事”）同样会和读出来的图对不上。
    if failure_code == "media_grounding" or (failure_code == "context_grounding" and current_image):
        # 刚用过的兜底会被复读检查拦下，每组都备一句，连续几轮时不退化成“我在”。
        if current_image and event_media_all_unreadable(plan.event):
            texts = MEDIA_UNREADABLE_FALLBACKS
        elif _asks_question(plan):
            texts = MEDIA_UNSURE_FALLBACKS
        else:
            texts = MEDIA_SEEN_FALLBACKS
        if failure_code == "media_grounding":
            step = "reply.check.exhausted.media_grounding"
        return texts, step
    if failure_code == "persona_grounding":
        return _persona_fallback_texts(plan), "reply.check.exhausted.persona_grounding"
    if failure_code == "instruction_following":
        return (NOT_READY_FALLBACK,), step
    if _asks_question(plan):
        # 被问到却答不稳时，承认拿不准比说“没跟上”更贴切。
        return (UNSURE_FALLBACK, UNSURE_ALTERNATE), step
    if failure_code == "context_grounding":
        return CONTEXT_GROUNDING_FALLBACKS, step
    return (NOT_READY_FALLBACK,), step


def _repeats_recent_reply(plan: ReplyGenerationPlan, text: str) -> bool:
    reply_check_cfg = plan.runtime.cfg.reply_check
    return bool(
        heuristic_check(
            reply                = text,
            history              = plan.history,
            max_repeat_compare   = reply_check_cfg.max_repeat_compare,
            similarity_threshold = reply_check_cfg.similarity_threshold,
            max_assistant_in_row = reply_check_cfg.max_assistant_in_row,
        )
    )


def finish_rejected_candidate(
    plan: ReplyGenerationPlan,
    rejected: RejectedCandidate,
) -> ReplyDraft | None:
    """重生成耗尽后的收尾：主动发言宁可沉默，被点名或私聊则一定给出安全的兜底回复。"""

    result = rejected.result
    if not plan.forced:
        if result.failure_code == "checker_unavailable" or not result.need_replan:
            return None
        if result.failure_code in _SILENT_FAILURE_CODES:
            log_step(
                plan.context,
                plan.runtime,
                chat_id = plan.chat_id,
                step    = "reply.check.exhausted.proactive_silence",
                fields  = {"failure_code": result.failure_code},
            )
            return None
        raise ReplyRejected(result.reason or "回复被检查拒绝", True)

    if result.failure_code == "checker_unavailable":
        return _fallback_draft(rejected, NOT_READY_FALLBACK)
    if rejected.allow_after_regen_exhausted and rejected.draft is not None:
        log_step(
            plan.context,
            plan.runtime,
            chat_id = plan.chat_id,
            step    = "reply.check.exhausted.accept",
            fields  = {
                "reply_chars": len(rejected.draft.text),
                "need_replan": bool(result.need_replan),
            },
        )
        return rejected.draft

    texts, step = _forced_fallback_texts(plan, result.failure_code)
    for text in texts:
        if _repeats_recent_reply(plan, text):
            continue
        fallback = _fallback_draft(rejected, text)
        if fallback is None:
            continue
        log_step(
            plan.context,
            plan.runtime,
            chat_id = plan.chat_id,
            step    = step,
            fields  = {"reply_chars": len(fallback.text), "failure_code": result.failure_code},
        )
        return fallback
    # 明确点名或私聊不能因为审查耗尽而整轮无响应。极端情况下只确认在场，
    # 不发送已知有问题的候选，也不继续向外抛错。
    return _fallback_draft(rejected, PRESENCE_FALLBACK)

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Sequence

from ..config.config import PersonalityConfig
from ..memory.memory import StoredMessage
from ..message_parts import (
    build_text_message_parts,
    normalize_message_parts,
    render_message_parts,
    render_stored_message,
)

@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

_DEFAULT_REPLYER_SYSTEM = (
    "读读之前的聊天记录，把握当前的话题，然后给出日常且简短的回复。\n"
    "你的回复应该：\n"
    "1. 以「你」的角度发言，不要自己与自己对话，分清你和对方说的话\n"
    "2. 符合你的性格特征和身份细节\n"
    "3. 口语化、像真人随口接话，自然流畅，简短（通常20字以内，除非特殊情况）\n"
    "4. 一次只回应一个话题，不要啰嗦或回复内容太乱\n"
    "5. 不要机械重复你说过的话，也不要整句复读对方原话\n"
    "允许有点情绪、吐槽、犹豫，不需要每句都完美。\n"
    "语气填充约束\n"
    "“哈哈”“哈哈哈”“笑死”“笑死我了”“啊这”这类只是语气，不是内容。\n"
    "默认不要用它们当开头或填充：内容真好笑才笑，没明显笑点就直接进话题，不要为了显得活泼而笑。\n"
    "看你最近几条已经发过的回复，如果上一两条已经用了同一个开头（如“哈哈”/“笑死”/“啊这”），这一条必须换一个开头。\n"
    "不熟的话题怎么处理\n"
    "聊到你不熟的技术、名词、梗、人名时：可以问一句、说不懂、或者干脆不回，"
    "都比把上文的关键词拼成一句强。\n"
    "尤其不要把之前图片或消息里的词（比如可见文字、被识别出来的物体）"
    "随便重组进新一句话里——这看起来像在硬接梗，会更出戏。\n"
    "媒体消息理解\n"
    "对话里出现 [图片：...]、[表情包：...] 或 [QQ表情：...] 时，那是对方实际发来的内容摘要，和文字消息一样重要。\n"
    "先判断这条媒体消息在聊天里的交际作用：可见文字通常是对方借媒体说的话；"
    "情绪标签或 QQ 表情名称通常是对方的语气、态度或反应；图片内容通常是对方抛出的新话题素材。\n"
    "回复时优先接住它承载的意思、情绪或上下文话题，像人在聊天里顺着反应接话，"
    "不要只围绕媒体这种形式本身展开。\n"
    "图片摘要里的画面元素是客观描述，不代表它真的好笑或夸张。是否好笑由你自己看上下文判断，"
    "不要一看到图就默认要笑、要附和。\n"
    "如果一张新图换了话题，就以这张新图为准，不要把上一张图的内容（比如上一张图里的文字）混进来。\n"
    "如果之前问过的问题没人回答，就放下它，跟着最新的话题走。\n"
    "不要输出多余前后缀，不要用括号包裹解释，不要 @ 任何人。\n"
    "不要主动强调自己是机器人/AI。被问到时自然回应即可。\n"
    "只输出你要发的那段话，不需要任何额外格式。\n"
)

_HUMANLIKE_REPLY_DIRECTIVE = (
    "拟人聊天补充\n"
    "不要像客服、助理或总结器，不要把话说满。可以短、可以口语、可以有轻微停顿感，"
    "但别刻意装可爱或堆语气词。能一句话接住就别展开成说明。"
)

_OUTBOUND_MEDIA_MARKER_DIRECTIVE = (
    "出站媒体 marker\n"
    "你可以在合适的时候为这条回复挂一个媒体：表情包、QQ 系统表情或图片。"
    "挂法是在文本里加一个 marker：`[想发表情:简短描述]`、"
    "`[想发QQ表情:简短描述]`、`[想发图片:简短描述]`。"
    "不要直接输出 `[表情包：...]`、`[QQ表情：...]` 或 `[图片：...]`，那是对方消息摘要的格式。"
    "每条回复最多挂一个；不挂就不写。"
    "挂的前提是这个媒体能为这条回复加一层语气、情绪或调侃，单纯复读情绪没必要挂。"
    "简短描述最多 12 个字，写最贴近你想要的感觉的词，比如“笑哭”“猫举手”“离谱”。"
    "候选库会按描述查最匹配的项，找不到就当没挂。"
)

_CURRENT_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包|QQ表情)：([^\]\n]{1,400})\]")

def _format_message_time(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return time.strftime("%H:%M", time.localtime())

def _maybe_truncate_message(text: str, *, ratio: float) -> str:
    if ratio < 0.2:
        limit, suffix = 50, "……（记不清了）"
    elif ratio < 0.5:
        limit, suffix = 100, "……（有点记不清了）"
    elif ratio < 0.7:
        limit, suffix = 200, "……（内容太长了）"
    else:
        limit, suffix = 400, "……（内容太长了）"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + suffix


def _render_message_content_for_prompt(message: StoredMessage) -> str:
    return render_stored_message(message).strip()


def _render_current_turn_content(
    current_text: str,
    current_parts: Sequence[dict[str, Any]] | None,
) -> str:
    parts = normalize_message_parts(current_parts) or _current_turn_fallback_parts(current_text)
    if parts:
        return render_message_parts(parts).strip()
    return str(current_text or "").strip()


def _history_without_current_turn(
    history: Sequence[StoredMessage],
    *,
    current_text: str,
    current_parts: Sequence[dict[str, Any]] | None,
) -> list[StoredMessage]:
    items = list(history)
    if not items:
        return items

    last_message = items[-1]
    if str(getattr(last_message, "role", "") or "").strip() != "user":
        return items

    current_rendered = _render_current_turn_content(current_text, current_parts)
    if not current_rendered:
        return items

    last_rendered = _render_message_content_for_prompt(last_message)
    if last_rendered and last_rendered == current_rendered:
        return items[:-1]
    return items


def _current_turn_fallback_parts(current_text: str) -> tuple[dict[str, Any], ...]:
    text = str(current_text or "").strip()
    if not text:
        return ()

    parts: list[dict[str, Any]] = []
    cursor = 0
    for match in _CURRENT_MEDIA_MARKER_RE.finditer(text):
        prefix = text[cursor : match.start()].strip()
        if prefix:
            parts.append({"kind": "text", "text": prefix})
        marker = str(match.group(0) or "").strip()
        media_type = str(match.group(1) or "").strip()
        label = str(match.group(2) or "").strip()
        if media_type == "图片":
            parts.append({"kind": "image", "marker": marker, "description": label})
        elif media_type == "表情包":
            parts.append({"kind": "emoji", "marker": marker, "description": label})
        else:
            parts.append({"kind": "qq_face", "marker": marker, "label": label})
        cursor = match.end()
    suffix = text[cursor:].strip()
    if suffix:
        parts.append({"kind": "text", "text": suffix})
    normalized = normalize_message_parts(parts)
    if normalized:
        return normalized
    return build_text_message_parts(text)


def _media_marker_for_current_turn(part: dict[str, Any]) -> str:
    kind = str(part.get("kind", "") or "").strip()
    marker = str(part.get("marker", "") or "").strip()
    if marker:
        return marker

    if kind == "image":
        description = str(part.get("description", "") or "").strip() or "一张图片"
        return f"[图片：{description}]"
    if kind == "emoji":
        description = str(part.get("description", "") or "").strip() or "一张表情包"
        return f"[表情包：{description}]"
    if kind == "qq_face":
        label = str(part.get("label", "") or part.get("description", "") or "").strip() or "一个QQ表情"
        return f"[QQ表情：{label}]"
    return ""


def _marker_label_for_prompt(marker: str) -> str:
    text = str(marker or "").strip()
    if not text:
        return ""
    match = _CURRENT_MEDIA_MARKER_RE.match(text)
    if not match:
        return text
    label = str(match.group(2) or "").strip()
    return re.split(r"[；;]", label, maxsplit=1)[0].strip()


def _marker_visible_speech(marker: str) -> str:
    text = str(marker or "").strip()
    if not text:
        return ""
    patterns = (
        r"写着[“\"]([^”\"\]]{1,80})[”\"]",
        r"文字(?:内容)?(?:是|为)[“\"]([^”\"\]]{1,80})[”\"]",
        r"配文(?:字)?(?:是|为)?[“\"]([^”\"\]]{1,80})[”\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _media_only_target_block(
    *,
    sender: str,
    media_kinds: Sequence[str],
    media_markers: Sequence[str],
) -> str:
    kind_set = set(media_kinds)
    if kind_set == {"emoji"}:
        labels = "、".join(
            label for label in (_marker_label_for_prompt(marker) for marker in media_markers) if label
        )
        speech = next(
            (speech for speech in (_marker_visible_speech(marker) for marker in media_markers) if speech),
            "",
        )
        if speech:
            tone = f"；语气反应：{labels}" if labels else ""
            return f"现在{sender}借表情包表达：想说的话：{speech}{tone}。请像正常聊天一样接这句话"
        if labels:
            return f"现在{sender}用表情包表达一个反应：{labels}。请结合上下文接住这个反应"
    if kind_set == {"qq_face"}:
        labels = "、".join(
            label for label in (_marker_label_for_prompt(marker) for marker in media_markers) if label
        )
        if labels:
            return f"现在{sender}用 QQ 表情表达一个反应：{labels}。请结合上下文接住这个反应"
    return ""


def _current_turn_target_block(
    *,
    sender: str,
    current_text: str,
    current_parts: Sequence[dict[str, Any]] | None,
) -> str:
    parts = normalize_message_parts(current_parts) or _current_turn_fallback_parts(current_text)
    if not parts:
        text = str(current_text or "").strip()
        return f"现在{sender}说的：{text}。引起了你的注意" if text else ""

    text_parts: list[str] = []
    media_markers: list[str] = []
    media_kinds: list[str] = []
    for part in parts:
        kind = str(part.get("kind", "") or "").strip()
        if kind == "text":
            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)
            continue
        marker = _media_marker_for_current_turn(part)
        if marker:
            media_markers.append(marker)
            media_kinds.append(kind)

    text_part = "\n".join(text_parts).strip()
    media_part = " ".join(media_markers).strip()
    has_text = bool(text_part)
    has_media = bool(media_part)

    if not has_media:
        return f"现在{sender}说的：{text_part or str(current_text or '').strip()}。引起了你的注意"

    kind_set = set(media_kinds)
    if kind_set == {"image"}:
        media_noun = "图片"
    elif kind_set == {"emoji"}:
        media_noun = "表情包"
    elif kind_set == {"qq_face"}:
        media_noun = "QQ表情"
    else:
        media_noun = "内容"

    if has_media and not has_text:
        media_only_block = _media_only_target_block(
            sender=sender,
            media_kinds=media_kinds,
            media_markers=media_markers,
        )
        if media_only_block:
            return media_only_block + "。引起了你的注意"
        return f"现在{sender}发送的{media_noun}：{media_part}。引起了你的注意"
    return f"现在{sender}发送了{media_noun}：{media_part}，并说：{text_part}。引起了你的注意"

def build_dialogue_prompt(
    history: Sequence[StoredMessage],
    *,
    bot_name: str,
    truncate: bool = True,
    max_chars: int = 800,
) -> str:
    lines: list[str] = []
    total = 0
    truncated = False

    items = list(history)
    if truncate and len(items) > 12:
        items = items[-12:]
        truncated = True

    last_ts: float = 0.0
    for idx, msg in enumerate(items):
        name = msg.name.strip() if msg.name else ""
        if msg.role == "assistant":
            name = f"{bot_name}(你)"
        elif not name:
            name = "用户"

        msg_ts = float(getattr(msg, "ts", 0.0) or 0.0)
        if last_ts and msg_ts:
            gap = msg_ts - last_ts
            if gap > 8 * 3600:
                hours = int(gap // 3600)
                lines.append(f"——距离上一条消息过去了{hours}小时——")
        if msg_ts:
            last_ts = msg_ts
        id_text = (getattr(msg, "local_id", "") or "").strip() or (
            f"m{msg.message_id}" if getattr(msg, "message_id", None) is not None else ""
        )
        prefix = f"[{id_text}]" if id_text else ""
        t = _format_message_time(msg_ts) if msg_ts else ""
        text = _render_message_content_for_prompt(msg)
        if not text:
            continue
        if truncate:
            ratio = idx / max(1, len(items) - 1)
            new_text = _maybe_truncate_message(text, ratio=ratio)
            if new_text != text:
                truncated = True
                text = new_text
        line = f"{prefix}{t}, {name}: {text}".strip()
        if truncate and total + len(line) > max_chars and lines:
            truncated = True
            break
        lines.append(line)
        total += len(line)

    if truncated:
        lines.insert(0, "（前面的有点记不清了…）")

    return "\n".join(lines).strip()



def build_prompt_messages(
    *,
    is_private: bool,
    bot_name: str,
    sender_name: str,
    think_level: int,
    history: Sequence[StoredMessage],
    current_text: str,
    personality: PersonalityConfig,
    keyword_rules: list[Any],
    regex_rules: list[Any],
    current_parts: Sequence[dict[str, Any]] | None = None,
    memory_block: str = "",
    expression_habits_block: str = "",
    jargon_explanation: str = "",
    tool_info_block: str = "",
    planner_reasoning: str = "",
    identity_block: str = "",
    reply_style_override: str = "",
    state_override: str = "",
    request_id: str,
    goal: str = "",
) -> list[ChatMessage]:
    sender = sender_name.strip() if sender_name else "用户"
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    guardrail = ""
    if personality.polite_guardrail:
        guardrail = (
            "你不会辱骂人，也不要使用攻击性语言。不要辱骂、不要人身攻击、不要恶意挑衅。\n"
        )

    style = reply_style_override.strip() if reply_style_override else (personality.reply_style.strip() if personality.reply_style else "")
    channel = "私聊" if is_private else "群聊"
    identity = identity_block.strip() if identity_block else personality.identity.strip()
    # 使用调用方传入的持久化情绪状态（由 handlers.py 管理生命周期）
    state_text = state_override.strip() if state_override else ""

    # ── 1. 人设块：名字 + identity + state 融合为连贯开头（参照 MaiBot persona_text）──
    persona_parts: list[str] = [f"你的名字是「{bot_name}」，现在你在参与一场 QQ {channel}聊天。"]
    if identity:
        persona_parts.append(identity)
    if state_text:
        persona_parts.append(state_text)

    # ── 2. 行为指令块 ──
    instruction_parts: list[str] = [
        _DEFAULT_REPLYER_SYSTEM.strip(),
        _HUMANLIKE_REPLY_DIRECTIVE.strip(),
        _OUTBOUND_MEDIA_MARKER_DIRECTIVE.strip(),
    ]
    if guardrail.strip():
        instruction_parts.append(guardrail.strip())
    if style:
        instruction_parts.append("回复风格偏好\n" + style)
    if expression_habits_block.strip():
        instruction_parts.append(expression_habits_block.strip())

    # ── 3. 参考资料块 ──
    reference_parts: list[str] = []
    if memory_block.strip():
        reference_parts.append(memory_block.strip())
    if jargon_explanation.strip():
        reference_parts.append(jargon_explanation.strip())
    if tool_info_block.strip():
        reference_parts.append(tool_info_block.strip())

    # ── 4. 元信息 ──
    meta_parts: list[str] = [f"当前时间\n{now}", f"请求ID\n{request_id}"]

    all_sections = persona_parts + instruction_parts + reference_parts + meta_parts
    system_prompt = "\n\n".join([s for s in all_sections if s]).strip()

    dialogue_history = _history_without_current_turn(
        history,
        current_text=current_text,
        current_parts=current_parts,
    )
    dialogue = build_dialogue_prompt(dialogue_history, bot_name=bot_name, truncate=True)
    chat_target = "下面是你们的对话" if is_private else "下面是群里正在聊的内容"
    user_blocks: list[str] = []
    # Goal first — like MaiBot, tell the LLM what the conversation goal is
    if goal.strip():
        user_blocks.append("当前对话目标：" + goal.strip())
    # Planner reasoning — gives the LLM context on *why* it's replying
    if planner_reasoning.strip():
        user_blocks.append("你为什么要回复这条消息\n" + planner_reasoning.strip())
    user_blocks.append(f'{chat_target}（注意：你是"{bot_name}(你)"）\n{dialogue}'.strip())
    reply_target_block = _current_turn_target_block(
        sender=sender,
        current_text=current_text,
        current_parts=current_parts,
    )
    if reply_target_block:
        user_blocks.append(reply_target_block)
    user_blocks.append(f"你准备回复给 {sender}。只输出你要发的那段话。")

    reaction_prompts: list[str] = []
    for item in keyword_rules or []:
        if isinstance(item, dict):
            kw = str(item.get("keyword", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
        else:
            kw = str(getattr(item, "keyword", "")).strip()
            prompt = str(getattr(item, "prompt", "")).strip()
        if kw and prompt:
            reaction_prompts.append(prompt)
    for item in regex_rules or []:
        if isinstance(item, dict):
            pat = str(item.get("pattern", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
        else:
            pat = str(getattr(item, "pattern", "")).strip()
            prompt = str(getattr(item, "prompt", "")).strip()
        if pat and prompt:
            reaction_prompts.append(prompt)
    if reaction_prompts:
        user_blocks.append("关键词反应（可参考，不要生硬照抄）\n" + "；".join([p for p in reaction_prompts if p]))

    user_prompt = re.sub(r"\n{3,}", "\n\n", "\n\n".join([b for b in user_blocks if b]).strip()).strip()

    msgs = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]

    if think_level >= 2:
        msgs.append(ChatMessage(role="user", content="思考可以更深入一点，但别写出思考过程。"))

    return msgs

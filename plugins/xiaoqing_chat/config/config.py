from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

class DebugConfig(BaseModel):
    show_planner_prompt: bool = False
    show_planner_reasoning: bool = False
    show_reply_prompt: bool = False
    show_memory_prompt: bool = False
    log_plan_reply: bool = False
    log_latency: bool = False
    log_steps: bool = True

class ResponseSplitterConfig(BaseModel):
    enable: bool = True
    max_length: int = 256
    max_sentence_num: int = 3

class ChineseTypoConfig(BaseModel):
    enable: bool = True
    error_rate: float = 0.01
    tone_error_rate: float = 0.1
    word_replace_rate: float = 0.006

class ResponsePostProcessConfig(BaseModel):
    enable_response_post_process: bool = True
    splitter: ResponseSplitterConfig = Field(default_factory=ResponseSplitterConfig)
    chinese_typo: ChineseTypoConfig = Field(default_factory=ChineseTypoConfig)

class PersonalityConfig(BaseModel):
    polite_guardrail: bool = True
    identity: str = (
        "你叫小青，是一个大二在读女大学生，现在正在上网和群友聊天。"
        "性格比较随和，偶尔会吐槽但不刻薄，有点自己的口癖和偏好。"
        "你不追求完美回答，更在意像个真实的人：会犹豫、会吐槽、会说不知道。"
        "别人问你是谁/是不是机器人，就随口回一句，别像自我介绍模板。"
    )
    states: list[str] = Field(
        default_factory=lambda: [
            "是一个女大学生，喜欢上网聊天，会刷小红书和抖音，对美食和穿搭有自己的看法。",
            "是一个大二心理学生，最近在准备考试，偶尔会刷贴吧和知乎摸鱼。",
            "是一个赛博网友，最近很想吐槽人，看到什么都想接一嘴。",
            "现在心情不错，比较放松，说话随意一些，容易被逗笑。",
            "刚吃完饭有点犯困，回复比较简短，懒得打长句子。",
            "最近迷上了一部剧/动漫/游戏，脑子里全是相关的东西。",
            "是一个喜欢音乐的文艺青年，最近在反复听一张专辑。",
        ]
    )
    state_probability: float = 0.30
    reply_style: str = (
        "口语化、像真人、尽量简短、不太有条理，别输出多余前后缀。不要用括号/冒号/表情包。不要复读用户原话。"
    )
    multiple_reply_style: list[str] = Field(default_factory=list)
    multiple_probability: float = 0.0

class PlannerConfig(BaseModel):
    enable_planner: bool = True
    smooth: int = 3
    mentioned_bot_reply: bool = True
    think_mode: str = "dynamic"
    llm_quote: bool = False

    def resolve_think_level(self, history_len: int = 0) -> int:
        """Map *think_mode* to a concrete integer think-level.

        * ``"dynamic"`` – scales with *history_len*: short context → 0,
          medium → 1, long → 2.
        * A numeric string (e.g. ``"1"``) – returns that fixed level.
        * Anything else – falls back to 1.
        """
        mode = self.think_mode.strip().lower()
        if mode == "dynamic":
            if history_len >= 20:
                return 2
            if history_len >= 10:
                return 1
            return 0
        try:
            return int(mode)
        except (ValueError, TypeError):
            return 1

class KeywordRule(BaseModel):
    keyword: str
    prompt: str
    probability: float = 1.0

class RegexRule(BaseModel):
    pattern: str
    prompt: str
    probability: float = 1.0

class KeywordReactionConfig(BaseModel):
    keyword_rules: list[KeywordRule] = Field(default_factory=list)
    regex_rules: list[RegexRule] = Field(default_factory=list)

class MemoryConfig(BaseModel):
    enable_memory_retrieval: bool = True
    planner_question: bool = True
    max_agent_iterations: int = 5
    agent_timeout_seconds: float = 120.0
    top_k: int = 5
    min_score: float = 0.12
    enable_thinking_back_cache: bool = True
    thinking_back_window_seconds: float = 1800.0
    thinking_back_max_entries: int = 200

class ReplyCheckConfig(BaseModel):
    enable_reply_checker: bool = True
    enable_llm_checker: bool = True
    max_repeat_compare: int = 2
    similarity_threshold: float = 0.9
    max_assistant_in_row: int = 3
    max_regen: int = 1
    max_replan: int = 1

class HeartflowConfig(BaseModel):
    enable_heartflow: bool = False
    base_score: float = 0.35
    threshold: float = 0.55
    enable_random_gate: bool = True
    # Configurable scoring weights (previously hardcoded)
    weight_private: float = 0.55
    weight_mentioned: float = 0.45
    weight_question: float = 0.12
    weight_goal_match: float = 0.06
    weight_short_text: float = -0.08
    weight_rate_limit: float = -0.35
    weight_cooldown: float = -0.45
    weight_interval: float = -0.25
    weight_no_reply_streak: float = 0.05
    weight_long_silence: float = 0.08

class GoalConfig(BaseModel):
    enable_goal: bool = True

class ReflectionConfig(BaseModel):
    enable_expression_reflection: bool = False
    require_approval_for_injection: bool = False
    operator_user_id: int = 0
    operator_group_id: int = 0
    min_interval_seconds: float = 3600.0
    max_pending: int = 10
    ask_per_check: int = 1
    enable_review_sessions: bool = False
    session_timeout_seconds: float = 7200.0
    resend_interval_seconds: float = 1800.0
    session_cooldown_seconds: float = 3600.0
    goal_lock_seconds: float = 3600.0
    max_avoid_patterns: int = 30

class BrainChatConfig(BaseModel):
    """深度对话模式配置 - 更智能、更深入的对话体验"""
    enable_private_brain_chat: bool = False
    private_planner_always_on: bool = True
    # 深度对话专用人格
    brain_identity: str = (
        "你叫小青，是一位善于深度思考和倾听的对话伙伴。"
        "在深度对话模式下，你会更认真地思考对方的观点，给出更有洞察力的回应。"
        "你会主动提出有价值的问题，引导对话向更深层次发展。"
        "你会记住对话中的重要细节，并在适当时候引用。"
        "你不会敷衍了事，而是真诚地对待每一次交流。"
    )
    # 深度对话回复风格
    brain_reply_style: str = (
        "思考性强、有条理但不生硬、真诚、有洞察力。"
        "可以适当长一点，但不要啰嗦。避免空洞的套话。"
    )
    # 深度对话思考级别 (0-3)
    brain_think_level: int = 2
    # 深度对话最大上下文
    brain_max_context_size: int = 30
    # 深度对话温度参数 (更低的温度 = 更理性的思考)
    brain_temperature: float = 0.7
    # 深度对话提示词前缀 (显示在对话开始)
    brain_mode_indicator: str = "🧠 深度对话模式"
    # 是否在回复中显示模式标识
    show_mode_indicator: bool = False

class SummarizerConfig(BaseModel):
    enable_topic_summarizer: bool = True
    min_messages_per_update: int = 12
    max_cache_topics: int = 20

class ExpressionConfig(BaseModel):
    enable_expression_learning: bool = True
    enable_expression_selector: bool = True
    max_injected: int = 5
    max_store: int = 200

class KnowledgeConfig(BaseModel):
    enable_knowledge: bool = False
    files: list[str] = Field(default_factory=list)
    top_k: int = 3

class RewriteConfig(BaseModel):
    enable_rewrite: bool = True
    probability: float = 0.6
    max_length_trigger: int = 80

class TalkScheduleEntry(BaseModel):
    """Time-period based talk frequency (MaiBot-style)."""
    hour_start: int = 0
    hour_end: int = 24
    talk_value: float = 1.0

class XiaoQingChatConfig(BaseModel):
    enable_smalltalk: bool = True
    reply_probability_base: float = 0.6
    reply_probability_private: float = 0.95
    min_reply_interval_seconds: float = 12.0
    max_replies_per_minute: int = 6
    continuous_reply_limit: int = 3
    continuous_cooldown_seconds: float = 25.0
    max_context_size: int = 30
    talk_schedule: list[TalkScheduleEntry] = Field(default_factory=list)
    timeout_seconds: float = 15.0
    max_retry: int = 2
    retry_interval_seconds: float = 10.0
    foreground_timeout_seconds: float = 12.0
    foreground_max_retry: int = 1
    foreground_retry_interval_seconds: float = 1.0
    background_timeout_seconds: float = 15.0
    background_max_retry: int = 2
    background_retry_interval_seconds: float = 10.0
    io_persist_debounce_seconds: float = 0.8
    memory_db_save_debounce_seconds: float = 20.0
    pfc_planner_timeout_seconds: float = 10.0
    pfc_planner_fail_window_seconds: float = 60.0
    pfc_planner_fail_threshold: int = 2
    pfc_planner_backoff_seconds: float = 120.0
    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 512
    think_level: int = 1
    ban_words: list[str] = Field(default_factory=list)
    ban_regex: list[str] = Field(default_factory=list)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    keyword_reaction: KeywordReactionConfig = Field(default_factory=KeywordReactionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    reply_check: ReplyCheckConfig = Field(default_factory=ReplyCheckConfig)
    heartflow: HeartflowConfig = Field(default_factory=HeartflowConfig)
    goal: GoalConfig = Field(default_factory=GoalConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    brain_chat: BrainChatConfig = Field(default_factory=BrainChatConfig)
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    expression: ExpressionConfig = Field(default_factory=ExpressionConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    postprocess: ResponsePostProcessConfig = Field(default_factory=ResponsePostProcessConfig)
    endpoint_path: str = "/v1/chat/completions"
    debug: DebugConfig = Field(default_factory=DebugConfig)

def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid json root type: {type(data)}")
    return data

def load_xiaoqing_chat_config(
    *,
    context_config: Optional[dict[str, Any]],
    plugin_dir: Path,
    filename: str = "xiaoqing_config.json",
) -> XiaoQingChatConfig:
    data: dict[str, Any] = {}
    if context_config:
        data = (
            context_config.get("plugins", {})
            .get("xiaoqing_chat", {})
        ) or {}

    file_path = plugin_dir / filename
    config_file_path = plugin_dir / "config" / filename
    if config_file_path.exists():
        data = {**data, **_read_json(config_file_path)}
    elif file_path.exists():
        data = {**data, **_read_json(file_path)}
    else:
        data = {**data}

    return XiaoQingChatConfig.model_validate(data)

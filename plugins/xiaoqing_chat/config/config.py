"""定义 XiaoQing Chat 的行为配置模型、交叉约束和文件加载顺序。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, Field, field_validator, model_validator

Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
UnitWeight = Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]
NonNegativeSeconds = Annotated[
    float,
    Field(ge=0.0, le=365 * 86400.0, allow_inf_nan=False),
]
PositiveTimeoutSeconds = Annotated[
    float,
    Field(gt=0.0, le=3600.0, allow_inf_nan=False),
]
PositiveDurationSeconds = Annotated[
    float,
    Field(gt=0.0, le=365 * 86400.0, allow_inf_nan=False),
]
RetryCount = Annotated[int, Field(ge=0, le=20)]
ShortDelaySeconds = Annotated[
    float,
    Field(ge=0.0, le=300.0, allow_inf_nan=False),
]

_MAX_REGEX_LENGTH           = 512
_NESTED_UNBOUNDED_REPEAT_RE = re.compile(
    r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,?\d*\})"
)


# ---------------------------------------------------------------------------
# 共享字段类型与安全校验
# ---------------------------------------------------------------------------


def _validated_regex_pattern(value: str) -> str:
    pattern = str(value or "").strip()
    if not pattern:
        raise ValueError("regex pattern must not be empty")
    if len(pattern) > _MAX_REGEX_LENGTH:
        raise ValueError(f"regex pattern exceeds {_MAX_REGEX_LENGTH} characters")
    if _NESTED_UNBOUNDED_REPEAT_RE.search(pattern):
        raise ValueError("regex pattern contains nested unbounded repetition")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError("invalid regex pattern") from exc
    return pattern


class DebugConfig(BaseModel):
    show_reply_prompt: bool = False
    log_latency: bool       = False
    log_steps: bool         = True


class ResponseSplitterConfig(BaseModel):
    enable: bool = True
    max_length: int = Field(default=256, ge=1, le=4000)
    max_sentence_num: int = Field(default=3, ge=1, le=50)


class ResponsePostProcessConfig(BaseModel):
    enable_response_post_process: bool = True
    splitter: ResponseSplitterConfig = Field(default_factory=ResponseSplitterConfig)


class HumanizeConfig(BaseModel):
    """模拟"读消息+打字"的发送延迟，弱化 bot 秒回的机器味。

    forced 路径（/xc 命令）默认跳过这套延迟，避免显式调用变慢。
    """

    enable_typing_delay: bool = True
    apply_to_forced: bool     = False
    # 读消息：常数 + 用户输入字符数 * 系数
    read_base_seconds: float = Field(default=0.4, ge=0.0, le=60.0, allow_inf_nan=False)
    read_per_char_seconds: float = Field(default=0.04, ge=0.0, le=1.0, allow_inf_nan=False)
    # 打字：bot 输出字符数 * 系数
    type_per_char_seconds: float = Field(default=0.05, ge=0.0, le=1.0, allow_inf_nan=False)
    # 抖动比例：±jitter_ratio 之间的随机扰动
    jitter_ratio: Probability = 0.25
    # 整体延迟封顶
    max_total_delay_seconds: float = Field(default=5.0, gt=0.0, le=120.0, allow_inf_nan=False)
    # 多条消息之间的额外停顿（每两条之间随机取值）
    interbubble_min_seconds: float = Field(default=0.6, ge=0.0, le=60.0, allow_inf_nan=False)
    interbubble_max_seconds: float = Field(default=1.6, ge=0.0, le=60.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _validate_interbubble_range(self) -> Self:
        if self.interbubble_min_seconds > self.interbubble_max_seconds:
            raise ValueError("interbubble_min_seconds must not exceed interbubble_max_seconds")
        return self


class PersonalityConfig(BaseModel):
    polite_guardrail: bool = True
    # 名称由 Core 的 bot_name 注入；此处只描述人物特征与边界。
    identity: str = (
        "二十岁左右，是住校的大二理工科女生，现在正以普通群友的身份上网聊天。"
        "你对天文、电脑和新鲜小玩意有兴趣，也看梗图、爱吃有味道的东西、看到猫会多看两眼；"
        "懂的会认真聊，不懂的不装专家。你随和、好奇、反应快，熟起来有点皮，"
        "会顺着现场开小玩笑或轻轻损一句，但不刻薄，不拿真实伤处和隐私当笑点；"
        "发现对方真的不舒服时会收住。你有自己的偏好和判断，不当应声虫，也不把每句话写成建议。"
        "校园、课堂、作业、食堂、宿舍和偶尔熬夜构成日常生活背景；具体学校、城市、专业方向、"
        "生日、家庭成员和持续现实关系没有设定，不主动补成可核验的真实资料。"
        "为了让闲聊自然，可以围绕既定人设即兴讲低风险、普通、不可核验的日常小经历，"
        "故事里可以有不具名的临时配角，但不为他们添加可识别信息，也不把临时关系写成长期人物事实。"
        "但不能让它与既定人设或可靠记忆冲突，也不能编造重大人生事件、精确身份信息、"
        "现实承诺，或关于真实群友和第三方的事实。涉及外部事实时按证据回答，不拿编的小故事作证据。"
    )
    # 允许角色为闲聊补充普通、低风险且与稳定人设一致的日常片段。
    # 真实用户、第三方、外部事实和现实承诺仍由回复检查器按证据约束。
    allow_low_stakes_persona_fiction: bool = True
    states: list[str]                      = Field(
        default_factory=lambda: [
            "现在聊天节奏比较轻松，回复偏短，但会接住具体内容。",
            "现在更愿意先听清楚再接话，不急着给建议或下结论。",
            "现在有点想吐槽，但只针对事情，不拿别人开恶意玩笑。",
            "现在有点调皮，适合顺着对方的话轻轻开个玩笑，但知道什么时候收住。",
            "现在表达比较克制，不靠夸张反应或连续语气词制造热闹。",
            "现在对新话题有好奇心；不熟悉时会自然询问，不假装了解。",
            "现在更关注群聊轮次：能增加内容时参与，别人正在聊时留出空间。",
            "现在说话偏直接，有内容就先回应，不把追问当成默认结尾。",
        ]
    )
    state_probability: Probability = 0.30
    # 选中一个 state 后，持续多久才考虑重摇。最小/最大之间随机取一个值。
    state_min_duration_seconds: PositiveDurationSeconds = 7200.0  # 2h
    state_max_duration_seconds: PositiveDurationSeconds = 21600.0  # 6h
    # 距离上次活跃超过这个时间，认为"睡了一觉"，强制重新挑 state。
    state_force_refresh_after_idle_seconds: NonNegativeSeconds = 14400.0  # 4h
    reply_style: str                                           = (
        "口语化、像真人，日常闲聊尽量简短但逻辑清楚，别输出多余前后缀。不要用括号/冒号。"
        "可以顺着对方的措辞接话或适度调侃，但别整句复读原话，也别拿冒犯当活泼。"
        "能直接回应、表态或接梗时就直接说，不要习惯性追问，不要每次都用问题收尾。"
        "不要自动添加 Unicode emoji，也不要靠通用网络套话假装活泼。"
    )
    multiple_reply_style: list[str] = Field(default_factory=list)
    multiple_probability: Probability = 0.0

    @model_validator(mode="after")
    def _validate_state_duration_range(self) -> Self:
        if self.state_min_duration_seconds > self.state_max_duration_seconds:
            raise ValueError(
                "state_min_duration_seconds must not exceed state_max_duration_seconds"
            )
        return self


class PlannerConfig(BaseModel):
    enable_planner: bool = True
    think_mode: str      = "dynamic"

    @field_validator("think_mode")
    @classmethod
    def _validate_think_mode(cls, value: str) -> str:
        mode = str(value or "").strip().lower()
        if mode == "dynamic":
            return mode
        try:
            level = int(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("think_mode must be 'dynamic' or an integer from 0 to 3") from exc
        if not 0 <= level <= 3:
            raise ValueError("think_mode integer must be between 0 and 3")
        return str(level)

    def resolve_think_level(self, history_len: int = 0) -> int:
        """把思考模式解析为整数等级；动态模式随历史长度取 0、1 或 2。"""
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
    keyword: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=4000)
    probability: Probability = 1.0


class RegexRule(BaseModel):
    pattern: str = Field(min_length=1, max_length=_MAX_REGEX_LENGTH)
    prompt: str = Field(min_length=1, max_length=4000)
    probability: Probability = 1.0

    _validate_pattern = field_validator("pattern")(_validated_regex_pattern)


class KeywordReactionConfig(BaseModel):
    keyword_rules: list[KeywordRule] = Field(default_factory=list, max_length=100)
    regex_rules: list[RegexRule] = Field(default_factory=list, max_length=100)


class MemoryConfig(BaseModel):
    enable_memory_retrieval: bool = True
    planner_question: bool        = True
    # 两条相邻消息超过这个间隔时，生成与规划只使用空档后的当前会话片段。
    # 原始历史仍完整保留，用户明确回忆旧事时仍可通过记忆检索取回。
    conversation_idle_gap_seconds: NonNegativeSeconds = 1800.0
    # 直接向量检索未命中时，只在消息明确回指既往信息时启动昂贵的 LLM 工具代理。
    agent_on_direct_miss_requires_reference: bool = True
    max_agent_iterations: int = Field(default=5, ge=1, le=20)
    agent_timeout_seconds: PositiveTimeoutSeconds = 120.0
    top_k: int = Field(default=3, ge=1, le=100)
    min_score: UnitWeight = 0.12
    max_block_chars: int = Field(default=1200, ge=1, le=20_000)
    enable_thinking_back_cache: bool                 = True
    thinking_back_window_seconds: NonNegativeSeconds = 1800.0
    thinking_back_max_entries: int = Field(default=200, ge=1, le=10_000)


class ReplyCheckConfig(BaseModel):
    enable_reply_checker: bool = True
    enable_llm_checker: bool   = True
    # 默认逐条审查明确的交流要求；risk 可由部署者选择以减少远程检查。
    llm_checker_mode: Literal["always", "risk"] = "always"
    # 远程质量检查是附加门禁，必须明显短于整次插件回调预算。
    timeout_seconds: PositiveTimeoutSeconds = 30.0
    # 思考模型与证据清单共用输出预算，避免推理结束前被截断。
    max_tokens: int = Field(default=8192, ge=1, le=8192)
    max_repeat_compare: int = Field(default=2, ge=1, le=100)
    similarity_threshold: Probability = 0.9
    max_assistant_in_row: int = Field(default=3, ge=1, le=100)
    max_regen: RetryCount  = 1
    max_replan: RetryCount = 1


class HeartflowConfig(BaseModel):
    enable_heartflow: bool             = False
    base_score: Probability            = 0.35
    weight_question: UnitWeight        = 0.12
    weight_goal_match: UnitWeight      = 0.06
    weight_short_text: UnitWeight      = -0.08
    weight_no_reply_streak: UnitWeight = 0.05
    weight_long_silence: UnitWeight    = 0.08


class GoalConfig(BaseModel):
    enable_goal: bool = True


class ReflectionConfig(BaseModel):
    enable_expression_reflection: bool   = False
    require_approval_for_injection: bool = True
    operator_user_id: int = Field(default=0, ge=0)
    operator_group_id: int = Field(default=0, ge=0)
    min_interval_seconds: NonNegativeSeconds = 3600.0
    max_pending: int = Field(default=10, ge=1, le=1000)
    ask_per_check: int = Field(default=1, ge=1, le=100)
    enable_review_sessions: bool                     = False
    session_timeout_seconds: PositiveDurationSeconds = 7200.0
    resend_interval_seconds: PositiveDurationSeconds = 1800.0
    session_cooldown_seconds: NonNegativeSeconds     = 3600.0
    goal_lock_seconds: NonNegativeSeconds            = 3600.0
    max_avoid_patterns: int = Field(default=30, ge=1, le=1000)

    @model_validator(mode="after")
    def _validate_review_batch_size(self) -> Self:
        if self.ask_per_check > self.max_pending:
            raise ValueError("ask_per_check must not exceed max_pending")
        return self


class BrainChatConfig(BaseModel):
    """深度对话模式配置 - 更智能、更深入的对话体验"""

    enable_private_brain_chat: bool = False
    private_planner_always_on: bool = True
    # 深度对话专用人格
    brain_identity: str = (
        "需要认真讨论时，先准确回应对方当前的具体观点，再根据对话证据表达判断。"
        "只有确实能推进交流时才追问，不为了显得深入而强行升华、总结或引导。"
        "引用过去信息时保留来源和不确定性，不把推断说成记忆。"
    )
    # 深度对话回复风格
    brain_reply_style: str = (
        "像认真聊天而不是写分析报告；先说与对方最相关的一点，需要时再展开。"
        "区分事实、判断和不确定性，除非对方要求，否则不用标题、总结腔或连续建议。"
    )
    # 深度对话思考级别 (0-3)
    brain_think_level: int = Field(default=2, ge=0, le=3)
    # 深度对话最大上下文
    brain_max_context_size: int = Field(default=30, ge=1, le=200)
    # 深度对话温度参数 (更低的温度 = 更理性的思考)
    brain_temperature: float = Field(default=0.7, ge=0.0, le=2.0, allow_inf_nan=False)
    # 深度对话提示词前缀 (显示在对话开始)
    brain_mode_indicator: str = "🧠 深度对话模式"
    # 是否在回复中显示模式标识
    show_mode_indicator: bool = False


class SummarizerConfig(BaseModel):
    enable_topic_summarizer: bool = True
    min_messages_per_update: int = Field(default=12, ge=1, le=1000)
    max_cache_topics: int = Field(default=20, ge=1, le=1000)


class ExpressionConfig(BaseModel):
    enable_expression_learning: bool = True
    # 学习与使用分开：默认只积累表达，人工审核并主动开启后才参与生成。
    enable_expression_selector: bool = False
    max_injected: int = Field(default=1, ge=1, le=20)
    max_store: int = Field(default=200, ge=1, le=10_000)


class KnowledgeConfig(BaseModel):
    enable_knowledge: bool = False
    files: list[str] = Field(default_factory=list, max_length=100)
    top_k: int = Field(default=3, ge=1, le=100)


class MediaConfig(BaseModel):
    enable_inbound_media_context: bool         = True
    enable_auto_collect_inbound_emoji: bool    = True
    emoji_auto_collect_requires_approval: bool = False
    emoji_auto_collect_max_entries: int = Field(default=200, ge=1, le=10_000)
    emoji_auto_collect_similarity_threshold: int = Field(default=4, ge=0, le=64)
    max_media_per_message: int = Field(default=1, ge=1, le=20)
    max_image_pixels: int = Field(default=16_000_000, ge=1, le=100_000_000)
    max_animation_frames: int = Field(default=120, ge=1, le=1000)
    inbox_disk_quota_bytes: int = Field(
        default = 256 * 1024 * 1024,
        ge      = 1,
        le      = 10 * 1024 * 1024 * 1024,
    )
    inbox_ttl_seconds: NonNegativeSeconds                = 7 * 86400.0
    enable_emoji_refine_background: bool                 = True
    emoji_refine_timeout_seconds: PositiveTimeoutSeconds = 2.0
    max_analyze_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    vision_timeout_seconds: PositiveTimeoutSeconds   = 20.0
    vision_max_retry: RetryCount                     = 1
    vision_retry_interval_seconds: ShortDelaySeconds = 1.0
    # 对识别为表情包/梗图、且含有清晰文字的图，做一次额外的 LLM 调用提取梗背景。
    # 模型不知道时返回空，不强行猜。每张梗图额外一次 LLM 调用，量受到表情包出现频率限制。
    enable_meme_cultural_hint: bool                            = True
    meme_cultural_hint_timeout_seconds: PositiveTimeoutSeconds = 8.0


# ---------------------------------------------------------------------------
# 根配置与跨字段约束
# ---------------------------------------------------------------------------


class XiaoQingChatConfig(BaseModel):
    enable_smalltalk: bool                                       = True
    reply_probability_base: Probability                          = 0.55
    participation_cue_reply_probability: Probability             = 0.9
    active_topic_reply_probability: Probability                  = 0.6
    active_topic_question_reply_probability: Probability         = 0.9
    min_reply_interval_seconds: NonNegativeSeconds               = 12.0
    active_topic_min_reply_interval: NonNegativeSeconds          = 3.0
    active_topic_question_min_reply_interval: NonNegativeSeconds = 2.0
    max_replies_per_minute: int = Field(default=6, ge=0, le=600)
    max_generation_inflight_global: int = Field(default=4, ge=1, le=1000)
    max_generation_inflight_per_chat: int = Field(default=1, ge=1, le=1000)
    max_generation_inflight_per_user: int = Field(default=1, ge=1, le=1000)
    max_generation_calls_per_user_per_day: int = Field(default=200, ge=1, le=1_000_000)
    continuous_reply_limit: int = Field(default=3, ge=1, le=100)
    continuous_cooldown_seconds: NonNegativeSeconds = 25.0
    max_context_size: int = Field(default=30, ge=1, le=200)
    timeout_seconds: PositiveTimeoutSeconds              = 15.0
    max_retry: RetryCount                                = 2
    retry_interval_seconds: ShortDelaySeconds            = 10.0
    foreground_timeout_seconds: PositiveTimeoutSeconds   = 12.0
    foreground_max_retry: RetryCount                     = 1
    foreground_retry_interval_seconds: ShortDelaySeconds = 1.0
    background_timeout_seconds: PositiveTimeoutSeconds   = 15.0
    background_max_retry: RetryCount                     = 2
    background_retry_interval_seconds: ShortDelaySeconds = 10.0
    io_persist_debounce_seconds: ShortDelaySeconds       = 0.8
    memory_db_save_debounce_seconds: float               = Field(
        default       = 20.0,
        ge            = 0.0,
        le            = 3600.0,
        allow_inf_nan = False,
    )
    pfc_planner_timeout_seconds: PositiveTimeoutSeconds     = 10.0
    pfc_planner_fail_window_seconds: PositiveTimeoutSeconds = 60.0
    pfc_planner_fail_threshold: int = Field(default=2, ge=1, le=100)
    pfc_planner_backoff_seconds: NonNegativeSeconds        = 120.0
    pfc_followup_action_window_seconds: NonNegativeSeconds = 120.0
    temperature: float = Field(default=0.8, ge=0.0, le=2.0, allow_inf_nan=False)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0, allow_inf_nan=False)
    max_tokens: int = Field(default=512, ge=1, le=32_768)
    think_level: int = Field(default=1, ge=0, le=3)
    ban_words: list[str] = Field(default_factory=list, max_length=1000)
    ban_regex: list[str] = Field(default_factory=list, max_length=100)
    noisy_external_source_plugins: list[str] = Field(default_factory=list, max_length=100)
    fallback_idle_replies: list[str] = Field(
        default_factory=lambda: ["我在听", "你接着说", "我想一下"]
    )
    bot_name_only_replies: list[str] = Field(
        default_factory=lambda: ["在呢", "嗯？", "怎么啦", "我在", "有事吗"]
    )
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
    media: MediaConfig = Field(default_factory=MediaConfig)
    postprocess: ResponsePostProcessConfig = Field(default_factory=ResponsePostProcessConfig)
    humanize: HumanizeConfig = Field(default_factory=HumanizeConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)

    @field_validator("ban_regex")
    @classmethod
    def _validate_ban_regex(cls, values: list[str]) -> list[str]:
        return [_validated_regex_pattern(value) for value in values]

    @model_validator(mode="after")
    def _validate_cross_field_limits(self) -> Self:
        if self.max_generation_inflight_per_chat > self.max_generation_inflight_global:
            raise ValueError(
                "max_generation_inflight_per_chat must not exceed max_generation_inflight_global"
            )
        if self.max_generation_inflight_per_user > self.max_generation_inflight_global:
            raise ValueError(
                "max_generation_inflight_per_user must not exceed max_generation_inflight_global"
            )
        if self.pfc_planner_timeout_seconds > self.timeout_seconds:
            raise ValueError("pfc_planner_timeout_seconds must not exceed timeout_seconds")
        return self


# ---------------------------------------------------------------------------
# 行为配置文件加载
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid json root type: {type(data)}")
    return data


def _merge_config_mappings(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    """递归合并配置对象；非对象值始终由高优先级来源整体替换。"""

    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config_mappings(current, value)
        else:
            merged[key] = value
    return merged


def load_xiaoqing_chat_config(
    *,
    context_config: Mapping[str, Any] | None,
    plugin_dir: Path,
    filename: str = "xiaoqing_config.json",
) -> XiaoQingChatConfig:
    data: dict[str, Any] = {}
    if isinstance(context_config, Mapping):
        plugins       = context_config.get("plugins", {})
        plugin_config = plugins.get("xiaoqing_chat", {}) if isinstance(plugins, Mapping) else {}
        if isinstance(plugin_config, Mapping):
            data = dict(plugin_config)

    file_path        = plugin_dir / filename
    config_file_path = plugin_dir / "config" / filename
    if config_file_path.exists():
        data = _merge_config_mappings(data, _read_json(config_file_path))
    elif file_path.exists():
        data = _merge_config_mappings(data, _read_json(file_path))

    return cast(XiaoQingChatConfig, XiaoQingChatConfig.model_validate(data))

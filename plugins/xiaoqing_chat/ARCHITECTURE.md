# xiaoqing_chat 架构与代码结构

`xiaoqing_chat` 是一个插件内聊天运行时。它挂在 XiaoQing 的命令系统和 smalltalk_provider 机制下，回复决策、上下文构建、行为规划、主 LLM 调用、多模态 marker 解析、记忆写入和 reply checker 都在插件内部完成。这个边界让小青的聊天行为可以快速迭代，同时不把拟人聊天规则塞回核心框架。

README 面向使用者说明启用和调参方式。本文件面向维护者说明代码结构、主链路和扩展边界。

## 架构目标

插件围绕四个目标设计。

1. 拟人参与：在群聊里区分“别人叫小青”和“群友自己聊”，尽量少像客服机器人。
2. 上下文连续：主回复 LLM 能看到近期历史、相关长期记忆、目标状态、人物资料和媒体上下文。
3. 多模态自然：入站文本、图片、QQ face、NapCat mface、reply 引用都进入同一条上下文链；出站通过自然语言 marker 触发表情、图片和 QQ face。
4. 可控退化：LLM、视觉模型、planner、reply checker 或媒体解析失败时安全降级，不阻塞基础文本聊天。

## 目录结构

```text
plugins/xiaoqing_chat/
├── plugin.json                     # 插件元数据、并发策略、命令与热加载文件
├── main.py                         # 框架入口和 /xc 子命令路由
├── handlers.py                     # smalltalk 主控、观察入口和回复记录
├── handlers_internal.py            # 配置、记忆、审查、模型等命令实现
├── handlers_helper.py              # 回复成功后的后台任务编排
├── handler_context.py              # 单次请求的配置、状态、AI route 和数据目录快照
├── attention_gate.py               # @、名字、reply-to-bot 和上下文共指判定
├── frequency_control.py            # 普通群聊参与门与硬频控
├── brain_chat.py                   # 私聊深度对话参数选择
├── generation_limiter.py           # 全局、会话和用户级生成配额
├── smalltalk_models.py             # 准备、生成和外发阶段的数据结构
├── smalltalk_execution.py          # 生成、投递确认、提交与回滚
├── smalltalk_media_helpers.py      # smalltalk 媒体同步与使用记录
├── reply_generator.py              # 回复草稿、上下文组装、重生成与检查
├── reply_payload.py                # 文本/媒体回复到 OneBot 批次的转换
├── reply_splitter.py               # 长回复拆分
├── message_parts.py                # 结构化文本和媒体片段
├── media_registry.py               # 稳定媒体引用、压缩正文和持久化元数据
├── context_builder.py              # 记忆、画像、表达和知识上下文
├── runtime_state.py                # per-chat 状态、后台任务和模型覆盖项
├── task_scheduler.py               # 防抖刷盘和后台任务生命周期
├── store_base.py                   # JSON 存储公共能力
├── store_binding.py                # 所有状态存储的数据目录绑定
├── helper_utils.py                 # 事件标识、配置热加载和 AI route 上下文
├── logging_utils.py                # 结构化日志与默认脱敏
├── constants.py                    # 跨模块常量和疑问句判断
├── config/
│   ├── config.py                   # Pydantic 配置模型和加载器
│   └── xiaoqing_config.json        # 插件行为配置
├── llm/
│   ├── gateway.py                  # 插件到 core AI capability 的薄适配层
│   ├── llm_client.py               # 响应提取和稳定的内部兼容导入名
│   ├── llm_config.py               # 单次模型调用参数
│   ├── prompt_builder.py           # prompt 组装
│   ├── postprocess.py              # 回复后处理
│   ├── reply_checker.py            # 回复检查器
│   └── summarizer.py               # 话题摘要
├── media/
│   ├── event_media.py              # 入站来源授权、落盘与消息顺序重建
│   ├── event_media_analysis.py     # 视觉提示、语义校验和 profile 链编排
│   ├── event_media_common.py       # 读取、编解码、缓存和 marker 公共逻辑
│   ├── marker_resolver.py          # 出站 [想发...] marker 解析
│   ├── emoji_library.py            # 会话隔离、索引校验的本地表情库
│   ├── qq_face.py                  # QQ face 描述规范化
│   ├── qq_face_catalog.py          # QQ face catalog 与使用记录
│   └── qq_face_builtin_catalog.json
├── memory/
│   ├── memory.py                   # 短期对话记忆与原子持久化
│   ├── memory_db.py                # 租户隔离的长期记忆和知识文档
│   ├── vector_store.py             # 文档向量检索
│   ├── memory_retrieval.py         # 记忆查询规划与工具循环
│   ├── person_profile.py           # 人物资料
│   ├── topic_summary_cache.py      # 话题摘要缓存
│   ├── thinking_back.py            # thinking back 缓存
│   ├── review_sessions.py          # 目标/表达人工复盘会话
│   ├── knowledge_base.py           # 管理员知识文件的原子索引
│   └── knowledge_extract.py        # 回复后人物事实提取
├── planning/
│   ├── pfc_engine.py               # PFC 引擎
│   ├── pfc_action_planner.py       # 行动规划
│   ├── pfc_goal_analyzer.py        # 目标识别
│   ├── pfc_utils.py                # 规划响应解析工具
│   ├── pfc_state.py                # PFC 状态
│   ├── planned_action.py           # 规划动作数据结构
│   ├── goal_state.py               # goal 状态
│   ├── heartflow.py                # 普通参与软评分
│   └── action_history.py           # 回复/观察 action 记录
├── expression/
│   ├── bw_message_recorder.py      # 群聊表达样本记录
│   ├── bw_expression_learner.py    # 表达学习
│   ├── bw_expression_store.py      # 表达存储
│   ├── bw_expression_reflector.py  # 人工表达复盘提问
│   ├── bw_reflect_tracker.py       # 复盘进度与结论跟踪
│   ├── bw_jargon_miner.py          # 黑话挖掘
│   ├── bw_jargon_store.py          # 会话/全局黑话存储
│   └── expr_utils.py               # 共享解析与对话渲染
├── experiments/
│   └── anthropomorphic_group.py    # 拟人大群实验 runner
├── utils/
│   ├── json_parsing.py             # JSON 提取和严格语义转换
│   └── tool_info.py                # 可用工具提示块
├── README.md                       # 使用与运维手册
├── ARCHITECTURE.md                 # 本文档
└── xiaoqing_chat测试.md             # 独立完整测试任务说明
```

## 入口层

`main.py` 暴露给 XiaoQing 框架的入口如下。

- `init(context)`: 初始化插件并记录启动状态。
- `handle(command, args, event, context)`: 处理 `/xc` 命令。
- `call_bot_name_only(context)`: 用户只喊 bot name 时的短回复。
- `observe_message(clean_text, event, context)`: 观察消息但不一定回复。
- `observe_outgoing_action(action, context)`: 观察其它插件或机器人出站行为。
- `shutdown(context)`: 限时等待或取消后台任务，并兜底刷盘所有脏存储。

`handlers.py` 是插件主控层。显式命令和 smalltalk 都会进入这里，然后再分发到配置、记忆、表达、模型切换、深度对话或普通聊天链路。

## smalltalk 主流程

当 `smalltalk_provider = xiaoqing_chat` 时，框架把群聊消息交给 `handle_smalltalk(clean_text, event, context)`。主流程可以概括为以下步骤。

```text
handle_smalltalk()
  |
  v
_prepare_smalltalk_turn()
  - rebuild effective input from message segments
  - decide_attention()
  - ordinary participation gate: _should_reply()
  - prepare goal / reflection / mood state
  |
  v
GenerationLimiter
  - global / chat / user inflight limits
  - per-user daily generation budget
  |
  v
_generate_smalltalk_turn()
  - ensure the user turn is recorded exactly once
  - forced: direct generation
  - ordinary: PFC planner or configured direct fallback
  - build context, call the main LLM, resolve media intent
  - run reply checker / bounded regeneration
  |
  v
_finalize_smalltalk_turn()
  - drop stale generated turns
  - build and send ordered OneBot batches
  - commit bot memory / PFC / goal / action history only after delivery acknowledgement
  - launch post-reply learning tasks only after commit
```

forced 场景会跳过普通插话概率；普通群聊才进入 `_should_reply()` 和 PFC planner。即使最终不回复，`observe_message()` 仍可更新上下文，让后续回复看到完整历史。

## Attention Gate

`attention_gate.py` 负责判断消息是否指向小青。核心返回值是 `AttentionDecision`。

- `forced`: 是否跳过普通概率门。
- `reason`: 触发原因。
- `direct_mentioned`: 是否直接点名或 `@`。
- `coreference_mentioned`: 是否通过上下文共指触发。
- `reply_to_bot`: 是否引用小青上一条消息。

判定包括以下因素。

- 命令和私聊。
- 群聊 `@`。
- bot name 文本匹配。
- 只喊 bot name 后的同一用户短时间追问。
- reply segment 的 message_id 是否指向小青历史回复。
- 最近历史存在小青锚点时，`她/他/ta` 等共指召唤。

共指触发是启发式加上下文锚点。没有小青锚点的普通代词不会触发 forced，以减少误回。

## Frequency Control 与 Heartflow

`frequency_control.py` 控制普通群聊插话，不处理 directed attention 的语义。它主要负责以下限制。

- `min_reply_interval_seconds`: 最小回复间隔。
- `max_replies_per_minute`: 每分钟回复上限。
- `continuous_reply_limit` 和 `continuous_cooldown_seconds`: 连续回复冷却。
- `reply_probability_base`: 普通参与基础概率。
- `active_topic_reply_probability` 和 `active_topic_min_reply_interval`: 基于上一轮目标的活跃话题参与率和间隔。
- 连续未回复补偿。

`planning/heartflow.py` 是普通参与的软评分模块，输入文本、目标状态和近期互动信号，输出一个参与倾向。它不再重复判断“是否被点名”“是否私聊”“是否超频”；这些由 attention gate 和硬频控负责。

## Planner 与 Goal

PFC planner 面向普通群聊。它会决定回复时机，并维护行为意图。

- 当前话题目标。
- 是否继续追问。
- 是否只是观察。
- 是否结束话题。
- 是否需要根据上下文调整回复风格。

forced 场景通常不需要 planner 才能回复，因为用户已经明确叫小青。普通群聊里 planner 能让小青更像群友：在有话题时接一句，在噪音或刷屏时沉默。

回复门控先读取上一轮 goal，再根据本轮观察更新 goal，因此消息不会把自己提前变成活跃话题。纯“乐/哈哈/草/噔噔咚”等低信息反应保留已有目标，不单独创建新目标。PFC 返回 `wait` 后不会再被短句长度规则改写；群聊规划失败也按 `wait` 保守降级。

## 主回复 LLM

主回复 LLM 由 `reply_generator.py`、`context_builder.py`、`llm/prompt_builder.py` 和 `llm/gateway.py` 协作完成。`llm/llm_client.py` 只负责解析响应和兼容现有内部导入，不再建立 HTTP 请求。模型传输、凭据、重试和跨 profile fallback 统一由 `core.ai` 执行。主回复能看到以下上下文。

- 当前有效用户输入。
- 当前 chat 最近一次长空档后的连续历史；完整原始历史仍保留给显式记忆检索。
- 相关长期记忆和人物资料。
- goal state 和 PFC planner 结果。
- expression store 中经过人工审核、且管理员主动启用的单条表达习惯；默认只学习不注入。
- 入站媒体 marker 和视觉描述。
- 媒体回复 marker 协议说明。

主 LLM 的输出先形成 `ReplyDraft`，再进入 postprocess、媒体 marker 解析和 reply checker。

## 远程数据边界

聊天和视觉 provider 是管理员在项目级统一注册表中配置的 OpenAI-compatible 第三方服务。公开连接与模型 profile 位于 `config.ai`，API Key 位于 `secrets.ai.providers`。本插件声明 `chat`、`reasoning`、`checker` 和 `vision` 四条 route：普通闲聊优先低延迟模型，数值/科学关系和回复复核使用高质量文本模型，图片理解使用视觉模型。插件上下文拿不到统一密钥，只能通过绑定了插件名的 `context.capabilities.ai` 调用自己的 route。

插件处理消息时会把当前输入及生成回复所需的历史、记忆、人物资料、规划结果和媒体上下文发送给最终命中的 provider；回复后的摘要、表达学习和人物事实提取也会按各自功能开关调用远程模型。部署者负责确认所选服务的用途、保留、训练和删除政策。普通日志只记录长度、指纹、错误类别和脱敏 profile，不写完整 prompt、响应、URL 或凭据。

## 多模态管线

### 入站

`media/event_media.py` 读取 OneBot 原始 segment，按顺序生成上下文文本。

- `face` 显示为 QQ face marker。
- `mface` 显示为表情包 marker。
- `image` 根据配置调用视觉模型或保守 marker。
- 文本和媒体保持原始顺序。

入站图片如果被识别为表情包，会被复制到本地 library，后续出站可复用。

### 出站

`media/marker_resolver.py` 解析主 LLM 输出中的 `[想发...]` marker。

- 先解析 marker 类型和 hint。
- 从表情包库、图片库、历史媒体和 QQ face catalog 收集候选。
- 按 hint 匹配候选。
- 转换为 OneBot image 或 face segment。
- 清理文本中的 marker 残留。

如果解析失败，回复仍以纯文本发送。

## Reply Checker

`llm/reply_checker.py` 做两层检查，并把拒绝分为 hard 和 soft。

- 启发式检查：重复、过长、连续回复、明显空泛或格式异常。
- 独立 LLM route 检查：分别判断上下文、说话人、人物声明是否属于既定事实或允许的低风险日常创作、事实可行性和模板化程度。

上下文错位、说话人混淆、人物创作越界为精确身份/重大经历/现实承诺、事实/数量级/因果错误和媒体错位属于 hard，重生成耗尽后仍不能发送；符合稳定人设的普通低风险日常小片段可以没有历史证据。轻微措辞或口癖问题属于 soft，强制回复场景可在重生成耗尽后采用最后一个结构安全候选。

## 状态和存储

`runtime_state.py` 管理 per-chat runtime。核心状态包括以下内容。

- recent messages。
- pending bot-name call。
- last reply metadata。
- reply gate logs。

`memory.conversation_idle_gap_seconds` 建立即时会话边界。相邻消息超过阈值后，
插件保留 `MemoryStore` 原始记录，只清除 goal、PFC、action history、thinking-back
和 topic summary 等旧话题临时状态；回复生成、PFC 和动态思考级别统一使用空档后的后缀。
- background media refine tasks。
- PFC state、goal state、action history、heartflow。

长期存储分布在 memory、expression、media library 和各自 store 中。状态应按 chat_id 隔离，避免不同群或私聊串台。

## 配置读取

插件相关配置分三层。

- `plugins/xiaoqing_chat/config/xiaoqing_config.json`: 行为配置，包括频控、planner、memory、reply checker、media、personality、postprocess、debug；不含 provider 字段。
- `config/config.json`: `ai.providers`、`ai.models` 以及 `plugins.xiaoqing_chat.ai.routes`；聊天和视觉 route 都是有序 profile 列表。
- `config/secrets.json`: 只在 `ai.providers.<name>.api_key` 保存统一 provider 密钥。

每次模型调用读取一份新的 core 原子配置快照。运行时通过 `/xc 配置` 查看行为摘要，通过 `/xc 模型` 查看 profile、严格固定聊天模型或用 `默认` 恢复自动 fallback。

## 实验和测试

自动化测试主要覆盖以下内容。

- 命令和 smalltalk 主路径。
- attention gate 和 coreference。
- media segment 接收、marker 解析、图片/表情/QQ face 出站。
- reply checker。
- memory、planner、state reset。

常用命令如下。

```powershell
python -m pytest tests/plugins -k "xiaoqing_chat or reply_checker" -q
```

`experiments/anthropomorphic_group.py` 是拟人大群实验 runner。它支持 matrix、dry-run 和 real 模式，real 模式走真实 `observe_message()` 和 `handle_smalltalk()`，但不发送到 live OneBot。

## 扩展边界

扩展能力时优先遵守以下边界。

- 新触发条件放在 `attention_gate.py`，不要塞进 `_should_reply()`。
- 普通群聊插话概率和硬频控放在 `frequency_control.py`。
- 主回复 prompt 相关内容放在 `context_builder.py` 或 `llm/prompt_builder.py`。
- 入站媒体解析放在 `media/event_media.py`。
- 出站媒体 marker 解析放在 `media/marker_resolver.py`。
- 长期记忆能力放在 `memory/`。
- 表达学习放在 `expression/`。
- planner 行为放在 `planning/`。

这能避免触发、频控、规划、生成和媒体处理互相重复，也便于针对单一模块写回归测试。

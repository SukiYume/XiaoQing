# 🏗️ xiaoqing_chat 架构

本文面向插件维护者，说明 `xiaoqing_chat` 的模块划分、单轮数据流、回复审查、状态提交和持久化规则。功能、命令和配置字段见 [README.md](README.md)，测试与验收见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

---

## 🏗️ 职责边界

插件建立在 XiaoQing Core 之上，自己负责：

1. 判断消息与角色的关系，决定是否参与、何时参与。
2. 构建连续会话片段、长期记忆、人物资料、目标和媒体上下文。
3. 规划回复、观察、等待和目标调整。
4. 调用 `chat`、`reasoning`、`checker`、`vision` 四条 AI route，生成并审查候选。
5. 把文本和媒体意图转换为有序 OneBot 批次。
6. 在投递确认后提交记忆、规划和学习状态。

Core 负责配置快照、权限主体、AI 凭据与传输、插件数据目录、消息投递与回执、生命周期和执行预算。插件只通过 `PluginContext`、`context.capabilities` 和 Core 服务使用这些能力。

```text
XiaoQing Core
  ├─ Router ─────────────── /xc ─────────────────────────┐
  ├─ Dispatcher ─────────── observe_message / handle_smalltalk / call_bot_name_only
  ├─ Core service ───────── core.observe_outgoing_action │
  ├─ 服务调用 ───────────── agent.handle_turn / agent.report_delivery → xiaoqing_agent
  ├─ AI capability ──────── chat / reasoning / checker / vision
  ├─ onebot_media ───────── get_msg / get_image         │
  └─ Delivery ───────────── ordered batches + receipt    │
                    │                                    │
                    ▼                                    ▼
xiaoqing_chat
  ├─ 入口与编排        main / handlers / commands
  ├─ 召唤与参与        attention_gate / participation / frequency_control / generation_limiter
  ├─ 单轮三阶段        turn_generation → reply_plan + reply_generator → turn_delivery
  ├─ 模型与审查        llm/
  ├─ 规划              planning/
  ├─ 记忆与知识        memory/
  ├─ 表达学习          expression/
  ├─ 媒体              media/ + media_registry + reply_media + reply_payload
  └─ 运行状态与存储    runtime_state / task_scheduler / store_base / store_binding
```

---

## 📂 模块地图

### 顶层

| 模块 | 职责 |
|---|---|
| `plugin.json` | Manifest：`/xc` 命令树、`onebot_media` capability、`core.observe_outgoing_action` 服务、使用 `agent.handle_turn` 与 `agent.report_delivery`、热加载文件 |
| `main.py` | 生命周期（`init` / `shutdown`）、`/xc` 子命令路由、Core 观察入口 |
| `handlers.py` | 消息观察、闲聊门控、单轮编排与生成配额；`call_bot_name_only`、`observe_outgoing_action` |
| `agent_bridge.py` | 明确召唤时把这一轮交给 `xiaoqing_agent` 判断（decide），同时照常生成聊天回复；接手则放弃生成、让出会话生成名额，执行（act）并把回复连同消息 ID 写入聊天记录、向 agent 回报是否发出；否则用已生成的回复 |
| `commands.py` | `/xc` 管理子命令：统计、重置、配置、记忆、表达、黑话、审查、模型切换 |
| `handler_context.py` | 单轮复用的上下文快照（runtime、state、chat_id、bot_name、secrets）与 `handle_errors` 装饰器 |
| `chat_event.py` | 从 OneBot 事件读取会话 ID、发送者和对角色的称呼 |
| `chat_runtime.py` | 配置热加载、AI route 上下文、`llm_call_config`、屏蔽规则与会话锁 |
| `attention_gate.py` | 明确召唤：`@`、名字、名字续问、引用角色、上下文共指 |
| `participation.py` | 普通群聊线索：全群邀请、发图请求、开放提问、起哄、激动分享、复读；低信息与点名他人 |
| `frequency_control.py` | 硬频控、参与概率、Heartflow 软加分、沉默补偿，记录 `ReplyGateDecision` |
| `generation_limiter.py` | 全局、会话、用户并发配额与用户日配额 |
| `brain_chat.py` | 私聊深度对话：在说话方式后追加人设卡的〔深度对话〕一节，思考等级、上下文长度和温度 |
| `persona.py` | 名字解析；解析人设卡（`config/persona.md`），按标记取出人物事实、聊天说话方式与示例、agent 办事部分和心境 |
| `smalltalk_models.py` | `PreparedSmalltalkTurn`、`GeneratedSmalltalkTurn` 阶段模型 |
| `turn_generation.py` | 会话锁内记录用户消息；锁外按强制 / PFC / 直接三条路径生成 |
| `reply_plan.py` | `ReplyGenerationPlan`：一次性收集上下文块、反应规则、规划目标和人设边界 |
| `reply_generator.py` | 生成尝试循环：预读本轮、请求、本地预检、媒体解析、审查、上下文收缩 |
| `reply_draft.py` | 回复草稿、分段规范化、出站媒体附加、尝试状态 |
| `reply_fallbacks.py` | 被拒后的定向重写提示和明确召唤兜底句（`ALL_FALLBACK_TEXTS`） |
| `turn_delivery.py` | 打字延迟、过期丢弃、分批外发、投递后提交与回滚 |
| `post_reply_tasks.py` | 投递成功后的摘要、事实提取、表达学习和策略复盘任务 |
| `reply_media.py` | 回复链路的媒体桥接与素材使用记录 |
| `reply_payload.py` | 把消息部件编排为展示文本和可投递批次 |
| `message_parts.py` | 结构化文本 / 媒体片段与 `content` / `media_items` 表示之间的无损转换 |
| `media_registry.py` | 聊天历史中的紧凑、稳定媒体引用 |
| `context_builder.py` | 记忆、人物资料、知识、表达和工具上下文 |
| `runtime_state.py` | 插件级存储、会话状态、后台任务、生成配额与模型覆盖 |
| `task_scheduler.py` | 后台任务登记、防抖刷盘与关闭收尾 |
| `store_base.py` / `store_binding.py` | JSON store 公共协议；把全部 store 绑定到 `context.data_dir` |
| `logging_utils.py` | `log_step` 结构化日志与脱敏 |
| `constants.py` | 跨模块稳定边界常量 |

### `llm/`：模型调用与审查

| 模块 | 职责 |
|---|---|
| `gateway.py` | 到 Core AI capability 的薄适配：路由、固定模型、响应提取 |
| `llm_config.py` | `LLMCallConfig`：超时、重试与采样参数；`bounded()` 收紧温度和输出预算，`as_kwargs()` 传给网关 |
| `prompt_builder.py` | 按人物边界、对话历史和当前媒体构建回复提示 |
| `postprocess.py` | 保留回复内容，按自然边界组织气泡 |
| `communication_constraints.py` | 原话中的交流要求（停聊、句数、条目数、篇幅字数）与群内定义 |
| `media_evidence.py` | 图片证据块：附件状态、缺图说明；`INBOUND_MEDIA_MARKER_RE`、`marker_visible_speech` |
| `local_checks.py` | 本地规则：复读、近似照搬、连续发言、凭空经历、身份一致性、纯媒体回复，以及是否需要语义审查 |
| `reply_checker.py` | 审查入口：本地规则 → 审查模型 → 证据契约解释 → 明确篇幅要求的本地字数核对（其他都通过后才看，不够按 soft 处理）；`TurnReadingCache` 缓存本轮独立阅读 |
| `evidence.py` | 证据契约：审查模型列出的陈述必须能在原文找到出处，并保持主体与语气 |
| `arithmetic.py` | 数值依据：份数、折扣、单位换算与两项加减 |
| `check_result.py` | `ReplyCheckResult`、`ReplyRejected` 与标准拒绝结果 |
| `summarizer.py` | 话题摘要的增量压缩 |

### `planning/`：行为规划

| 模块 | 职责 |
|---|---|
| `pfc_engine.py` | PFC 会话准备、动作规划、等待、重想和状态提交 |
| `pfc_action_planner.py` | 把上下文转换为有界动作计划 |
| `pfc_goal_analyzer.py` | 依据可见对话和行动历史更新短期目标 |
| `pfc_state.py` | PFC 状态持久化与并发写入 |
| `pfc_utils.py` | 从模型返回值中提取结构化列表 |
| `planned_action.py` | 不可变的规划动作 |
| `goal_state.py` | 会话目标与活跃话题 |
| `heartflow.py` | 回复节奏与参与软评分 |
| `action_history.py` | 已确认动作的持久化，每会话保留 200 条 |

### `memory/`：记忆与知识

| 模块 | 职责 |
|---|---|
| `memory.py` | `StoredMessage` 与按会话持久化的近期历史 |
| `local_ids.py` | 会话内 `mN` 消息编号的分配、查找与替换 |
| `memory_db.py` | 长期记忆文档、人物资料和向量索引 |
| `vector_store.py` | 本地哈希向量索引，文档与矩阵一致性缓存 |
| `memory_retrieval.py` | 带来源、受范围约束的记忆上下文与检索代理 |
| `person_profile.py` | 按会话和主体保存人物资料，重置代际 |
| `knowledge_extract.py` | 从原话提取带主体证据的人物事实 |
| `fact_extraction_checkpoint.py` | 事实提取水位 |
| `knowledge_base.py` | 管理员批准的知识文件，全有或全无的有界索引 |
| `topic_summary_cache.py` | 话题摘要缓存 |
| `thinking_back.py` | 近期回想缓存 |
| `review_sessions.py` | 人工复盘会话与回复策略 |

### `expression/`：表达与黑话

| 模块 | 职责 |
|---|---|
| `bw_expression_learner.py` | 从用户消息抽取、审核、持久化表达 |
| `bw_expression_store.py` | 表达的原子持久化与增量合并 |
| `bw_expression_reflector.py` | 抽样待审表达，向操作者发起确认 |
| `bw_reflect_tracker.py` | 反思队列与操作者判断 |
| `bw_jargon_miner.py` | 挖掘黑话并补全本地释义 |
| `bw_jargon_store.py` | 按会话或全局范围持久化黑话 |
| `bw_message_recorder.py` | 学习水位，防止重复抽取 |
| `expr_utils.py` | 共享工具 |

### `media/`：媒体

| 模块 | 职责 |
|---|---|
| `event_media.py` | 把入站 OneBot 媒体解析为有界、隐私安全的上下文 |
| `event_media_analysis.py` | 视觉分析流水线、梗背景提示 |
| `sources.py` | 获取媒体字节：下载、OneBot 补取、data URL / base64、本地路径授权、落盘与上限 |
| `loading.py` | 阻塞任务限流、有界读文件、来源识别、图片检测、动图拼帧 |
| `markers.py` | `[图片：…]` / `[表情包：…]` 标记、描述质量判断与降级描述 |
| `render_cache.py` | 按媒体哈希缓存摘要，带容量、过期与提示词版本校验 |
| `models.py` / `settings.py` | 数据类型、支持范围；媒体配置读取与收件目录 |
| `emoji_library.py` | 按隐私范围隔离的表情库，派生元数据修复 |
| `marker_resolver.py` | 解析出站媒体意图并绑定到已授权的真实素材 |
| `qq_face.py` / `qq_face_catalog.py` | QQ 表情描述；内置目录与运行时观察的合并 |

### 其他

| 目录 | 内容 |
|---|---|
| `config/` | `config.py`（Pydantic 模型、交叉约束、加载顺序）与 `xiaoqing_config.json` |
| `experiments/` | `anthropomorphic_group.py`：拟人大群实验 runner |
| `utils/` | `json_parsing.py`（收窄与修复模型 JSON）、`text.py`（`clip_text`）、`wall_clock.py`（按 Core 时区格式化时间） |

---

## ⌨️ 框架入口

| 入口 | 调用方 | 职责 |
|---|---|---|
| `init(context)` | PluginManager | 开放后台任务、绑定 store、加载媒体注册表 |
| `handle(command, args, event, context)` | Router | `/xc` 命令；非子命令的内容作为明确召唤交给 `handle_smalltalk` |
| `call_bot_name_only(context)` | Dispatcher | 只喊名字时的短应答，并登记 60 秒名字续问 |
| `observe_message(clean_text, event, context)` | Dispatcher | 记录入站消息与原始消息段 |
| `handle_smalltalk(clean_text, event, context)` | Dispatcher | 闲聊决策与回复链路 |
| `observe_outgoing_action(action, context)` | Core service | 记录其他插件以角色身份发出的消息；本插件自己的回复已在回执提交时连同消息 ID 记下，这里跳过；带敏感标记的动作直接跳过 |
| `shutdown(context)` | PluginManager | 停止接收任务、刷盘、取消并等待后台任务 |

---

## 💾 单轮数据流

```text
OneBot event
  │
  ▼
observe_message
  ├─ 保留原始消息段，重建有效输入（文本 + 媒体标记）
  └─ 写入一次作用域内的用户消息
  │
  ▼
handle_smalltalk → _prepare_smalltalk_turn
  ├─ 停聊请求 / 屏蔽词 → 结束
  ├─ 长空档 → 切换短期状态（保留原始历史）
  ├─ attention_gate.decide_attention        明确召唤？
  ├─ frequency_control.decide_reply         普通参与：硬频控 → 概率 → Heartflow → 沉默补偿
  ├─ 记录本轮观察到的目标
  └─ PreparedSmalltalkTurn
  │
  ▼
agent_bridge（明确召唤且 agent 已开启时）
  ├─ agent decide 与下面的聊天生成同时开始
  ├─ 判断为闲聊 / agent 不可用 → 继续用生成好的回复
  └─ agent 接手 → 放弃生成并让出名额 → agent act → 回复带回执返回 Core
  │
  ▼
generation_limiter.admit                    全局 / 会话 / 用户 / 日配额；会话配额排队，最新一轮优先
  │
  ▼
turn_generation.generate_smalltalk_turn
  ├─ 会话锁内：确保用户消息已记录，取 PFC 快照
  ├─ 后台预取记忆
  └─ 三条路径之一：
       forced  → 直接起草
       planner → run_pfc_once → 规划器决定发言时回调起草
       direct  → 规划关闭时直接起草
         │
         ▼
       reply_plan.build → reply_generator 尝试循环
         ├─ 预读本轮（always 模式，与生成并行，各次尝试共用）
         ├─ prompt_builder → gateway(chat) → postprocess
         ├─ marker_resolver 解析媒体意图
         ├─ reply_checker：本地规则 + 语义审查
         └─ 被拒 → reply_fallbacks：定向重写 / 重规划 / 兜底 / 安静
  │
  ▼
turn_delivery.finalize_smalltalk_turn
  ├─ 过期检查（打字期间的新消息、会话重置）
  ├─ 打字延迟（目标时长扣除已用时间），分批发送，气泡间隔
  ├─ 投递回执确认 → 提交：助手消息（连同回执收集的全部消息 ID）、PFC、目标、Heartflow、行动历史
  │                 → post_reply_tasks：摘要、事实提取、表达学习、策略复盘
  └─ 投递拒绝 / 异常 → 回滚待提交状态
```

阶段之间只传递 `PreparedSmalltalkTurn`、`GeneratedSmalltalkTurn` 和 `ReplyPayload`，不共享松散字典。规划器选择 `wait` / `listening` 时本轮立即以不发言结束，不在 handler 里等待：Core 按“群 + 用户”串行处理事件，等待会推迟同一个人后续消息的处理，还会占住会话生成配额。被审查拒绝的普通参与轮次会写回 PFC 快照，让下一轮从新状态出发。

---

## 📌 召唤与参与

`attention_gate.decide_attention` 返回 `AttentionDecision`：

| 字段 | 含义 |
|---|---|
| `forced` | 明确召唤，直接进入生成 |
| `force_reason` | 召唤来源 |
| `mentioned` / `direct_mentioned` | 名字或 `@` 命中 |
| `coreference_mentioned` | 上下文共指命中（要求近期历史有角色锚点） |
| `reply_to_bot` | 引用的 `message_id` 是角色历史消息 |

普通参与由 `frequency_control.decide_reply` 决定，顺序固定：

1. `_hard_limit_reason`：最小间隔（活跃话题与追问使用更短的间隔）、每分钟上限、连续冷却、低信息、点名他人。
2. `_participation_probability`：基础概率；参与线索、活跃话题、活跃话题追问分别抬到各自上限，取最大值。
3. Heartflow：`score - base_score` 的正值部分按 `probability + (1 - probability) × Δ` 抬高。
4. `_streak_adjusted`：连续沉默 3 轮 ×1.2（≤ 0.90），5 轮 ×1.4（≤ 0.95）。
5. 掷骰，结果与概率、掷值、沉默轮数一起写入 `ReplyGateDecision`，`smalltalk.no_reply` 日志直接输出这些字段。

Core 提取正文时只保留文本段。`handlers._label_other_mentions` 把 `@` 其他成员的段落按原位置还原为 `@名字` 再进入有效文本、媒体合成和历史，硬限制、参与线索、规划器和回复提示都依据这份文本判断这句话在叫谁。

私聊深度对话模式跳过第 2～4 步。通过后，规划开启时进入 PFC，关闭时直接起草；参与线索只影响概率，不改变路径。

---

## 🧠 规划与生成

PFC（`planning/pfc_engine.py`）每轮执行一次 `run_pfc_once`：读取会话片段、行动历史、目标和知识，由 `pfc_action_planner` 在 `reasoning` 路由上选择动作：

| 动作 | 结果 |
|---|---|
| `direct_reply` / `send_new_message` | 回调 `_TurnGenerator.planner_reply` 按动作模式起草 |
| `wait` / `listening` | 不发言，本轮立即结束；`wait_seconds` 只写入日志 |
| `rethink_goal` | `pfc_goal_analyzer` 更新目标 |
| `fetch_knowledge` | 检索知识库后再规划 |
| `end_conversation` | 结束话题，状态置为 ended |
| `block_and_ignore` | 忽略本轮 |

规划器解析失败时，私聊回退为 `direct_reply`，群聊回退为 `wait`。规划超时取 `pfc_planner_timeout_seconds` 与 `timeout_seconds` 的较小值，连续失败进入退避。

起草由 `reply_plan.py` 与 `reply_generator.py` 完成：

1. `ReplyGenerationPlan` 一次性收集：近期历史、记忆与人物资料（`context_builder`）、表达与黑话、知识、关键词反应规则、规划目标、人设边界和前台调用参数。每次重试复用同一份计划。
2. `llm/prompt_builder.py` 生成消息列表；`llm/gateway.py` 通过当前插件的 AI capability 调用 `chat` 路由，会话或全局模型覆盖通过 `pinned_model` 生效。
3. `llm/postprocess.py` 清理并分段；`media/marker_resolver.py` 解析 `[想发…]` 标记。
4. `llm/reply_checker.py` 审查；被拒后由 `reply_fallbacks.py` 选择下一步。
5. 上游返回 `request_too_large` 时把历史减半并作废已取的记忆，再试一次。

后台任务统一使用 `llm_call_config(cfg, foreground=False)` 生成的 `LLMCallConfig`，单个任务用 `bounded(max_temperature, max_tokens, min_tokens)` 收紧采样与输出预算。凭据、HTTP、超时、重试和 profile 回退都在 Core AI capability 内完成。

---

## ✅ 回复审查

### 本地规则（`llm/local_checks.py`）

`heuristic_check` 依次检查：空回复；与最近 `max_repeat_compare` 条角色消息完全相同或相似度 ≥ `similarity_threshold`；原样复述自己刚说过的整句（≥ 8 字）；去掉 `[…]` 标记和标点后与最近 6 条角色消息的相似度 ≥ 0.75（`_near_copy_of_recent`，至少 12 字）；连续 `max_assistant_in_row` 条角色发言。只发一个媒体时跳过复读比较。

其他规则：`grounded_self_history_check`（角色自称的经历、关系、近况需要依据）、`grounded_context_history_check`（群友经历需要依据）、`persona_identity_consistency_check`（性别等身份表述与人设一致）、`media_meta_reply_check`（只发媒体却在答需要文字的问题）。`requires_llm_semantic_check` 决定 `risk` 模式下是否需要语义审查。

### 语义审查（`llm/reply_checker.py`）

审查规则与 JSON 材料分为独立消息。审查模型先列出陈述与证据，再给出判断：

| 轴 | 字段 |
|---|---|
| 上下文 | `context_coherent`、`context_claims`（以 `source_ids` 引用对话证据编号） |
| 说话人 | `speaker_correct` |
| 交流要求 | `instruction_followed` |
| 事实 | `factually_plausible` |
| 人设 | `persona_grounded`、`persona_claims`：按人设坚持真人身份、用玩笑带过属于符合人设；承认是 AI / 模型 / 程序，或提到提示词、系统设定、指令时为 false |
| 图片证据 | `image_evidence_respected`、`visual_claims` |
| 模板化 | `non_template` |

`llm/evidence.py` 按原文核对每条陈述的出处、主体与语气；`llm/arithmetic.py` 核对数值推导。`enable_semantic_checker` 开启时，另有一次只接收原话的独立阅读，再与候选比较立场、主体、时间和完成状态。两路候选审查并行执行，共用 `reply_check.timeout_seconds`；独立阅读的超时复用与任务回收由 `TurnReadingCache` 管理。任一路确认的硬错误都会生效，`json_object` 外层封装的结论同样解析。

独立阅读的材料只有对话原文、当前消息、发言者、本群用语和角色名，与候选无关。`TurnReadingCache` 按这组材料缓存一份阅读：`always` 模式下 `reply_generator` 在请求候选前发起预读，语义审查等待这份结果后只做候选比较；重写时材料不变即复用，上下文缩减或材料变化时重新阅读。审查超时只取消本次等待，进行中的阅读留给下一次尝试；阅读失败或无效时本次审查记为不可用，并清空缓存，下次重新阅读。生成结束时取消并回收未用完的阅读。`risk` 模式不预读，只在需要远程审查时阅读，重写时同样复用。

图像证据由 `llm/media_evidence.py` 统一提供给生成与审查，区分“附件存在”和“画面已读出”。缺图时的说明要求像群友一样随口说图没加载出来，不使用画面、读取、解析这类说法；附和用户对未知画面的评价也算确认画面。

### 不可用策略

审查模型不可用时结果为 `severity="infra"`、`failure_code="checker_unavailable"`：

- `always`：不发送未审查的候选。明确召唤回复 `NOT_READY_FALLBACK`，普通参与安静。
- `risk`：放行通过本地规则的候选；未开启低风险人设发挥且问题涉及人设时，按 `persona_grounding` 拒绝。

### 重写与兜底（`reply_fallbacks.py`）

| 情况 | 处理 |
|---|---|
| 还有重写次数 | 按 `failure_code` 生成定向修正提示（soft 只调语气措辞，hard 只承接可见依据） |
| 普通参与，耗尽 | `persona_grounding` / `context_grounding` / `instruction_following` 安静；其他需要重规划的问题抛出 `ReplyRejected` 交给 PFC；不需要重规划时安静 |
| 明确召唤，耗尽，只剩 soft 且本地规则通过 | 接受该候选（`reply.check.exhausted.accept`） |
| 明确召唤，耗尽，仍有 hard | 按原因选择兜底句（`reply.check.exhausted.forced_fallback`），跳过与最近发言重复的句子，最后退到 `PRESENCE_FALLBACK` |

兜底句集合为 `ALL_FALLBACK_TEXTS`，评测脚本据此识别审查耗尽后的回复。

---

## 🖼️ 多模态管线

### 入站

`media/event_media.py` 验证来源，按原始顺序重建 `text`、`at`、`reply`、`face`、`mface`、`image`。`media/sources.py` 取字节：事件 URL、data URL / base64、授权本地路径，失效时通过 `onebot_media` capability 调用 `get_msg` / `get_image`（多附件按文件标识匹配）。`media/loading.py` 有界读取和解码，`media/event_media_analysis.py` 调用 `vision` 路由；梗图额外请求一次梗背景提示。摘要按媒体哈希写入 `media/render_cache.py`，提示词版本变化时失效重算。

所有媒体受字节数、像素、动图帧数、超时、重试和收件区配额约束。表情包进入按隐私范围隔离的 `emoji_library`，相似图按感知哈希合并；`media_registry` 为历史保留稳定引用，历史图片在窗口截短后仍以摘要形式提供给后续追问。

### 出站

`media/marker_resolver.py` 解析 `[想发表情:…]`、`[想发QQ表情:…]`、`[想发图片:…]`，从表情库、图片库、历史媒体和 QQ 表情目录中选择已授权素材；没有匹配时移除标记。`reply_payload.py` 编排有序批次，`reply_media.py` 在投递确认后记录素材使用。

---

## 💾 状态与持久化

### 进程内状态（`runtime_state.py`）

- 每个会话的统计、锁、回复时间戳、连续回复计数、冷却与当前回复元数据。
- 名字续问登记（60 秒）。
- 会话级与全局模型覆盖。
- 媒体细化与后台学习任务、防抖刷盘任务。
- `GenerationLimiter`。

### 持久化

所有 store 继承 `store_base.py`，由 `store_binding.py` 绑定到 `context.data_dir`，通过 Core 原子存储发布并保留 `.bak`。读取时校验对象根、整数、有限浮点、布尔和集合元素；主文件损坏时恢复有效备份，主备都不可用时隔离损坏文件。`io_persist_debounce_seconds` 和 `memory_db_save_debounce_seconds` 控制防抖。

| 规则 | 实现 |
|---|---|
| 消息编号 | 首次分配时读取已有历史的最大 `mN`，覆盖重启、闲置重置和缓存淘汰后的续写 |
| QQ 消息 ID | 角色发言在投递回执提交时连同全部消息 ID 写入（`message_id` 为第一条，`extra_message_ids` 为拆分后的其余各条），不事后回填；引用判断检查记录的全部 ID |
| 会话锁 | 锁缓存 + 弱引用注册表；缓存淘汰后持有者与等待者仍共享原锁 |
| 重置代际 | 人物资料目录的 `.generation` 标记；重置在共享路径锁内更新标记、清理 JSON 与备份、删除向量记录；后台任务在远程调用前记录代际，提交时核对 |
| 表达与黑话 | 加载结果携带基线，保存时以基线向最新磁盘状态合并增量 |
| 表情库 | 收集和使用计数在索引路径锁内提交；耗时修复在锁外分析，按旧快照校验后提交差量 |
| 隔离记忆 | 面向聊天的记忆查询统一排除 `quarantined_` 类型 |
| 连续会话边界 | 到达 `conversation_idle_gap_seconds` 时保留原始历史，目标、PFC、行动历史、近期回想和话题摘要切换到新片段 |

### 关闭

`shutdown()` 先停止接收新任务并刷盘，再取消后台任务并等待至多 2 秒。全部结束后执行最终刷盘；仍有任务存活时抛出 `TimeoutError`，由 Core 生命周期记录关闭失败。Core 的关闭预算由所有插件共享。

---

## ⚙️ 配置生命周期

| 来源 | 内容 |
|---|---|
| `config/config.json` → `bot_name` | 角色名字 |
| `config/config.json` → `plugins.xiaoqing_chat` | AI route、模型别名；也可写行为字段 |
| `plugins/xiaoqing_chat/config/xiaoqing_config.json` | 行为、频控、规划、记忆、媒体、人设开关、调试（与上一项合并，文件优先） |
| `plugins/xiaoqing_chat/config/persona.md`（或 `personality.persona_file`） | 人设卡：人物事实、说话方式、回复示例、心境 |
| `config/secrets.json` | Provider API Key |

`chat_runtime.py` 读取 Core 原子快照并维护文件热加载缓存（配置文件或人设卡任一改动都会重新加载）；`config/config.py` 校验字段范围与交叉约束（如单会话、单用户并发不超过全局并发，规划超时不超过 `timeout_seconds`）。每轮只使用自己的快照。

人设卡在加载配置时读入 `PersonalityConfig.card`。不带标记的人物事实交给规划、表达学习和审查（作为受控人物资料）；生成时人物事实放在系统提示开头，〔聊天〕各节（说话方式、示例）放在系统提示末尾、离对话最近；agent 交接时只交人物事实和〔办事〕各节。`persona.py` 把快照中的 `bot_name` 填进人设卡的 `{{name}}`；人设卡不写名字。历史中的角色消息按 `assistant` 角色存储，渲染时使用当前名字。

---

## 🔐 错误与日志

模块内的解析、类型转换和文件操作捕获具体异常。AI provider、动态 capability、媒体解码、后台任务和生命周期属于隔离边界，统一记录脱敏错误类别并返回受控降级结果。

`logging_utils.log_step` 以 `xiaoqing_chat step=<步骤>` 输出结构化字段，QQ 号、群号、URL、profile 和自由文本按规则脱敏。主要步骤：

| 步骤 | 含义 |
|---|---|
| `smalltalk.recv` | 收到消息，带召唤判定字段 |
| `smalltalk.no_reply` | 门控未通过，带原因、概率、掷值 |
| `smalltalk.explicit_silence` / `smalltalk.ignore` | 停聊请求 / 屏蔽词 |
| `smalltalk.generation_limited` | 生成配额不足 |
| `smalltalk.forced_direct` / `smalltalk.pfc.done` / `smalltalk.planner.disabled` | 三条生成路径 |
| `reply.check.start` / `result` / `regen` / `timeout` / `error` | 审查过程 |
| `reply.check.exhausted.accept` / `forced_fallback` / `proactive_silence` | 重写耗尽后的处理 |

---

## 🛠️ 扩展位置

| 要改的行为 | 位置 |
|---|---|
| 召唤条件 | `attention_gate.py` |
| 普通参与线索 | `participation.py` |
| 频控与概率 | `frequency_control.py` |
| 规划 | `planning/` |
| 生成上下文 | `context_builder.py`、`reply_plan.py`、`llm/prompt_builder.py` |
| 人设 | `config/persona.md`（人设卡）、`persona.py`、`config/config.py` 的 `PersonalityConfig` |
| 审查规则 | `llm/local_checks.py`（本地）、`llm/reply_checker.py`（语义） |
| 被拒后的处理 | `reply_fallbacks.py` |
| 入站媒体 | `media/event_media.py`、`media/sources.py`、`media/event_media_analysis.py` |
| 出站媒体 | `media/marker_resolver.py`、`reply_payload.py` |
| 长期记忆和人物资料 | `memory/` |
| 表达与黑话 | `expression/` |
| 持久化 | `store_base.py` |

新增领域入口配套单元测试；跨层流程配套 `handle_smalltalk()` 级测试或真实模型评测剧本；持久化变更配套损坏输入、并发和重启测试。见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

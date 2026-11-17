# 🏗️ xiaoqing_chat 架构

本文面向插件维护者，说明 `xiaoqing_chat` 的模块划分、单轮数据流、回复审查、状态提交和持久化规则。功能、命令和配置字段见 [README.md](README.md)，测试与验收见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

---

## 🏗️ 职责边界

插件建立在 XiaoQing Core 之上，自己负责：

1. 判断消息与角色的关系，决定是否参与、何时参与。
2. 构建连续会话片段、目标和媒体上下文，从统一记忆（`xiaoqing_memory`）取这一轮的记忆。
3. 规划回复、观察、等待和目标调整。
4. 调用 `chat`、`reasoning`、`checker`、`vision` 四条 AI route，生成并审查候选。
5. 把文本和媒体意图转换为有序 OneBot 批次。
6. 在投递确认后提交聊天记录、规划和学习状态，把消息推给统一记忆。

Core 负责配置快照、权限主体、AI 凭据与传输、插件数据目录、消息投递与回执、生命周期和执行预算。插件只通过 `PluginContext`、`context.capabilities` 和 Core 服务使用这些能力。

```text
XiaoQing Core
  ├─ Router ─────────────── /xc ─────────────────────────┐
  ├─ Dispatcher ─────────── observe_message / handle_smalltalk / call_bot_name_only
  ├─ Core service ───────── core.observe_outgoing_action │
  ├─ 服务调用 ───────────── agent.handle_turn / agent.report_delivery → xiaoqing_agent
  │                         memory.record / recall / search / forget → xiaoqing_memory
  ├─ AI capability ──────── chat / reasoning / checker / vision
  ├─ onebot_media ───────── get_msg / get_image         │
  ├─ inbound_backlog ────── 同一发送者排着的后续消息    │
  └─ Delivery ───────────── ordered batches + receipt    │
                    │                                    │
                    ▼                                    ▼
xiaoqing_chat
  ├─ 入口与编排        main / handlers / commands
  ├─ 召唤与参与        attention_gate / participation / frequency_control / generation_limiter
  ├─ 单轮三阶段        turn_generation → reply_plan + reply_generator → turn_delivery
  ├─ 模型与审查        llm/
  ├─ 规划              planning/
  ├─ 记忆与知识        memory/（统一记忆的客户端、近期历史、知识库）
  ├─ 表达学习          expression/
  ├─ 生活连续性        life/
  ├─ 媒体              media/ + media_registry + reply_media + reply_payload
  └─ 运行状态与存储    runtime_state / task_scheduler / store_base / store_binding
```

---

## 📂 模块地图

### 顶层

| 模块 | 职责 |
|---|---|
| `plugin.json` | Manifest：`/xc` 命令树、`onebot_media` 与 `inbound_backlog` capability、`core.observe_outgoing_action` 服务、使用 `agent.handle_turn` 与 `agent.report_delivery`、`life.turn_context` 与 `life.export_memes`、`memory.record` / `recall` / `search` / `forget`、定时任务、热加载文件 |
| `main.py` | 生命周期（`init` / `shutdown`）、`/xc` 子命令路由、Core 观察入口、定时任务（生活小事、梗图同步、统一记忆补推、主动开口） |
| `handlers.py` | 消息观察、闲聊门控、单轮编排与生成配额，`observe_outgoing_action`；一轮出故障时由 `_reply_after_failure` 收口（见“错误与日志”）；有会话等着清长期记忆时，任何会话来消息都经 `_retry_pending_forgets` 触发一次后台重试（见“状态与持久化”） |
| `agent_bridge.py` | 私聊和群里明确跟她说的话交给 agent 判断，与聊天生成并行（流程见“单轮数据流”）；没人叫她的群消息不交给 agent。一件事分几条说时把连着的几条连成一句请求；agent 要反问而对方的下一条已经到了时，这句反问不发。执行结果与投递回执写回原会话 |
| `commands.py` | `/xc` 管理子命令：统计、重置（同时清空统一记忆里这个会话）、配置、记忆（`memory.search`）、表达、黑话、审查、模型切换 |
| `handler_context.py` | 单轮复用的上下文快照（runtime、state、chat_id、bot_name、secrets）与 `handle_errors` 装饰器 |
| `chat_event.py` | 从 OneBot 事件读取会话 ID、发送者和对角色的称呼 |
| `chat_runtime.py` | 配置热加载、AI route 上下文、`llm_call_config`、屏蔽规则与会话锁 |
| `follow_ups.py` | 对方连着发几条时等他说完再回：经 `inbound_backlog` 看同一个人后面有没有排着的聊天消息（命令、单独一个链接、@ 了别人的话和空消息不算）；让出这一轮时把“在跟她说话”和没回的话带给下一轮（45 秒内有效） |
| `attention_gate.py` | 记录 `@`、引用、叫名字和命令来源；私聊和群里明确跟她说的话设置直接回复标记 |
| `participation.py` | 识别只有协议表情码、无内容媒体和标点的低信息群消息，这类消息不启动规划 |
| `frequency_control.py` | 硬频控、参与概率、Heartflow 软加分、沉默补偿，记录 `ReplyGateDecision` |
| `generation_limiter.py` | 全局、会话、用户并发配额与用户日配额。上一轮回复交给 Core 后到回执结算前，同会话下一轮在配额内等待 |
| `brain_chat.py` | 私聊深度对话：在说话方式后追加人设卡的〔深度对话〕一节，思考等级和上下文长度 |
| `persona.py` | 名字解析。解析人设卡（`config/persona.md`），按标记取出人物事实、聊天说话方式与示例、agent 办事部分和心境 |
| `smalltalk_models.py` | `PreparedSmalltalkTurn`、`GeneratedSmalltalkTurn` 阶段模型 |
| `turn_generation.py` | 会话锁内记录用户消息。锁外按强制 / PFC / 直接三条路径生成 |
| `reply_plan.py` | `ReplyGenerationPlan`：一次性收集上下文块、反应规则、规划目标和人设边界；`recall_for_reply` 每轮从统一记忆取一次（挂在事件上，规划前取的那次生成时复用），`planner_memory` 是交给规划器的说话人页面和群页 |
| `name_call.py` | `call_bot_name_only`：只喊名字、只 `@` 她时从 `bot_name_only_replies` 随机回一句，发出后记进聊天记录，这个人一分钟内的下一句算在跟她说；他已经发了下一条时不应 |
| `reply_generator.py` | 生成尝试循环：预读本轮、请求、本地预检、媒体解析、审查、上下文收缩 |
| `reply_draft.py` | 回复草稿、分段规范化、出站媒体附加、尝试状态 |
| `reply_fallbacks.py` | 被拒后的定向重写提示、只删没依据的读图气泡（`trim_unsupported_bubbles`）和直接回复兜底句（`ALL_FALLBACK_TEXTS`） |
| `turn_delivery.py` | 打字延迟、过期丢弃与引用原消息（阈值见“单轮数据流”）、分批外发、按气泡回执结算、投递后提交与回滚 |
| `post_reply_tasks.py` | 投递成功后的表达学习、策略复盘、未完话头结算和生活记录抽取任务 |
| `reply_media.py` | 回复链路的媒体桥接与素材使用记录 |
| `reply_payload.py` | 把消息部件编排为展示文本和可投递批次 |
| `message_parts.py` | 结构化文本 / 媒体片段与 `content` / `media_items` 表示之间的无损转换 |
| `media_registry.py` | 聊天历史中的紧凑、稳定媒体引用 |
| `context_builder.py` | 知识、表达、黑话和工具上下文 |
| `runtime_state.py` | 插件级存储、会话状态、后台任务、生成配额与模型覆盖 |
| `task_scheduler.py` | 后台任务登记、防抖刷盘（聊天记录落盘后推给统一记忆） |
| `store_base.py` / `store_binding.py` | JSON store 公共协议。把全部 store 绑定到 `context.data_dir` |
| `timing.py` | 一轮对话各阶段的耗时累计（`accumulate_timing`、`append_timing`） |
| `constants.py` | 跨模块稳定边界常量 |

### `llm/`：模型调用与审查

| 模块 | 职责 |
|---|---|
| `gateway.py` | 到 Core AI capability 的薄适配：路由、固定模型、响应提取 |
| `llm_config.py` | `LLMCallConfig`：超时、重试与采样参数。从主配置路由生成参数快照，`as_kwargs()` 传给网关，长文请求按字数提高预算 |
| `prompt_builder.py` | 按人物边界、对话历史和当前媒体构建回复提示；`build_dialogue_prompt` 把对话写成逐行文本，截短时最多留最近 20 条（`max_items`）和 `max_chars` 字（回复 1400，规划器 1200，话题收尾判断 2200）；读多少历史由调用方决定：回复读 `max_context_size` 条，规划器读 `max(60, max_context_size × 3)` 条；按她最近几条回复给出换开头、不带装饰、回短一点的提醒 |
| `postprocess.py` | 保留回复内容，只按自然边界组织气泡（空行分段、按句、按逗号与各字数阈值的规则见 README“投递”），并清掉开头泄漏的“你：”示例标签 |
| `decorations.py` | 发出前去掉句尾垃圾词（`strip_junk_tail`），并按她最近 6 条回复收拾装饰、把句尾打成文字的表情名换成 QQ 表情（规则见 README“投递”）。agent 当轮交回的办事回复也过一遍：没有表情素材，表情名直接去掉 |
| `communication_constraints.py` | 原话中的交流要求（停聊、句数、条目数、篇幅字数）与群内定义 |
| `media_evidence.py` | 图片证据块：附件状态、缺图说明。`INBOUND_MEDIA_MARKER_RE`、`marker_visible_speech` |
| `local_checks.py` | 本地规则：复读、近似照搬、连续发言、凭空经历、身份一致性、纯媒体回复、没人问原因却以“因为”起头解释自己为什么接话（`narrated_motive_check`），以及是否需要语义审查 |
| `reply_checker.py` | 审查入口：本地规则 → 审查模型 → 证据契约解释 → 明确篇幅要求的本地字数核对（其他都通过后才看，不够按 soft 处理）。`TurnReadingCache` 缓存本轮独立阅读 |
| `evidence.py` | 证据契约：审查模型列出的陈述必须能在原文找到出处，并保持主体与语气 |
| `arithmetic.py` | 数值依据：份数、折扣、单位换算与两项加减 |
| `check_result.py` | `ReplyCheckResult`、`ReplyRejected` 与标准拒绝结果 |
| `topic_shift.py` | 新用户消息到达后通过 `reasoning` 复核叫停、换题与参与时机；复核失败时保留旁听 |

### `planning/`：行为规划

| 模块 | 职责 |
|---|---|
| `pfc_engine.py` | PFC 会话准备、动作规划、等待、重想和状态提交 |
| `pfc_action_planner.py` | 把上下文转换为有界动作计划 |
| `pfc_goal_analyzer.py` | 依据可见对话和行动历史更新短期目标 |
| `pfc_state.py` | PFC 状态持久化与并发写入。取快照时登记轮次号，更新的轮次写回后，迟到的旧快照不再写回（回复本身照常记进聊天记录） |
| `pfc_utils.py` | 从模型返回值中提取结构化列表 |
| `planned_action.py` | 不可变的规划动作 |
| `goal_state.py` | 会话目标与活跃话题 |
| `heartflow.py` | 回复节奏与参与软评分 |
| `action_history.py` | 已确认动作的持久化，每会话保留 200 条 |

### `memory/`：记忆与知识

| 模块 | 职责 |
|---|---|
| `client.py` | 统一记忆的客户端：近期历史里没推过的消息推给 `memory.record`（推过的键记在 `memory_outbox.json`，失败时留着下次补推），第一次推送某个会话时导入它已有的数据文件（见 `legacy.py`）；`recall` / `search` / `forget_chat` 包装，能力缺失、超时或出错一律当空；会话重置后的待清与重试见“状态与持久化”的“长期记忆的清除” |
| `legacy.py` | 第一次推送某个会话时导入的数据源（`vdb/memory.docs.json` 里的人物事实和话题摘要、`person_profiles/<会话>/` 下的人物资料、`life/self_timeline.json`、`life/open_loops/`），写成导入操作，只读不改 |
| `memory.py` | `StoredMessage` 与按会话持久化的近期历史 |
| `local_ids.py` | 会话内 `mN` 消息编号的分配、查找与替换 |
| `memory_db.py` | 管理员知识库的向量索引；同一份索引（`vdb/memory.docs.json`）里的会话文档只在首次导入时由 `legacy.py` 读取，重置时按会话删除 |
| `vector_store.py` | 本地哈希向量索引，文档与矩阵一致性缓存 |
| `person_profile.py` | 会话的重置代际标记（`person_profiles/<会话>/.generation`）：重置时更新，后台任务提交前核对；重置时一并删掉该目录里的人物资料 JSON 和 `hippo_memorizer/<会话>.json` |
| `knowledge_base.py` | 管理员批准的知识文件，全有或全无的有界索引 |
| `review_sessions.py` | 人工复盘会话与策略 |

### `life/`：生活连续性

| 模块 | 职责 |
|---|---|
| `daily_life.py` | 每天的生活小事：生成（给模型看前四天的记录，几件事来自不同方面；失败后退避）、每件一句话加一句细节、按日期存取、只交出已过时间点的、被问到过去时组合前几天的生活 |
| `schedule.py` | 解析人设卡〔作息〕，取此刻命中的时段，给出作息底色、今天这一天的安排（`day_plan_text`）、插话概率与打字延迟的乘数（`routine_factor`） |
| `closing_guard.py` | 私聊收尾沉默：对方只回收尾话、角色已经收过尾时本轮不回复 |
| `self_timeline.py` | 角色说过的近况、打算、承诺和讲过的往事：数据结构、校验（类别、存活时间、只讲她自己时各会话共用）和提示块。记录存在统一记忆里；往事（`story`）留 90 天以上、不和近况抢名额，单独取用 |
| `open_loops.py` | 群友有时间点的未完话头：数据结构、同一件事的判断、回复里有没有问到和提示块。记录存在统一记忆里，按说话人取“顺口问 / 对方在答”由统一记忆的取用给出 |
| `extract.py` | 投递后一次 `reasoning` 调用同时抽取自我陈述和未完话头（读取时机和办事回复的处理见 README“生活连续性”，办事回复以 `source=agent` 区分）；已有记录经 `memory.search` 读取，校验类别、说话人、ID 与时间后写成笔记操作，连同没推过的消息一次交给 `memory.record`；按会话串行，提交前核对重置代际 |
| `proactive.py` | 群聊主动开口（行为见 README“主动开口”）：时段、作息、群安静程度与节奏的门控，组装材料，起草 → 本地复读检查 → 发送前自检（拦下时带理由重写一次），投递回执提交时写聊天记录和节奏记录 |
| `proactive_private.py` | 私聊主动联系（`proactive.private_user_ids` 白名单，默认空即不运行）：判断由头并逐项检查次数、间隔和回复状态（见 README“私聊主动联系”）；人物页取自统一记忆；起草、本地复读检查、发送前自检和回执复用群聊流程 |
| `proactive_state.py` | 节奏记录：每个会话上次主动开口的时间、当天次数、上次尝试时间、已分享的网上经历 ID（最近 20 个）和最近 5 次主动说的话；群聊键是 `g<群号>`，私聊键是 `private:<QQ号>` |
| `push_log.py` | 推送记录：其他插件刚推送的全文（至多 3000 字）和本地配图路径，每个会话最近 6 条、一天半以内；配图第一次要用时经 `vision` 路由看一遍，看到的内容写成文字存回记录 |
| `online.py` | 经 `life.turn_context` 向 `xiaoqing_life` 取网上经历、用语与表达（契约见表后），并把梗图同步进表情库（`sync_life_memes`） |

`online.py` 的契约：

- 请求不含用户 ID 与群号。能力缺失、开关关闭、超时、异常或格式不对一律当空。
- 群聊请求带统一记忆给的本群话题词：回复时是这两三天的几个，主动开口取分享候选时是这个群还记着的全部，`xiaoqing_life` 据此标出 `heard_here`。
- 群聊请求还带本群黑话块这一轮按已确认的释义解释过的说法（`explained_terms`，网上生活不再解释一遍；本群只是猜测的不算）。
- `reply_plan` 先取网上生活，再拼生活块：`life_lines` 追加到 `life_block`；自我页里只引用了 `shown_keys` 里那几条经历、查明白的事或日记的行不再放（同一件事只说一遍，细节以网上生活给的为准）；`slang_notes` 追加到 `jargon_explanation`；`expression_hint` 只在没有群内表达时补位。
- `sync_life_memes` 按游标（`life/life_memes_cursor.json`）逐页拉 `life.export_memes`，经 `emoji_library.import_external_emoji` / `remove_external_emoji` 导入和删除，只动 `life:` 来源的条目。

### `expression/`：表达与黑话

| 模块 | 职责 |
|---|---|
| `bw_expression_learner.py` | 从用户消息抽取、审核、持久化表达 |
| `baseline_store.py` | 表达库和黑话库共用的“基线 + 增量”合并存储：读时记下版本作基线，写回时只把相对基线的改动合到磁盘上最新的版本；整表清理一个会话时抛出写失败，并让备份文件也是清理后的内容 |
| `bw_expression_store.py` | 表达的原子持久化与增量合并（同一条记录按字段合并） |
| `bw_expression_reflector.py` | 抽样待审表达，向操作者发起确认 |
| `bw_reflect_tracker.py` | 反思队列与操作者判断 |
| `bw_jargon_miner.py` | 挖掘黑话；每条词条记下它出现处的前后文。原句解释过的释义，或至少两段互不重叠的用法、模型明说证据充分并引用了其中至少两段、释义本身不含糊的推断，才标为已确认（`confirmed`），其余只是候选 |
| `bw_jargon_store.py` | 按会话持久化黑话；词条在哪个会话可用由 `JargonRecord.visible_to` 统一判断（回复、网上生活和 `/xc` 统计页用同一口径）；没有会话的旧记录对任何会话都不可见，读写时原样保留 |
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
| `models.py` / `settings.py` | 数据类型、支持范围。媒体配置读取与收件目录 |
| `emoji_library.py` | 按隐私范围隔离的表情库，派生元数据修复 |
| `marker_resolver.py` | 解析出站媒体意图并绑定到已授权的真实素材 |
| `qq_face.py` / `qq_face_catalog.py` | QQ 表情描述。内置目录与运行时观察的合并 |

### 其他

| 目录 | 内容 |
|---|---|
| `config/` | `config.py`（Pydantic 模型、交叉约束、加载顺序）、`xiaoqing_config.json`（行为默认值）、`persona.md`（默认人设卡，见“配置生命周期”）和 `agent.md`（agent 判断与执行两个阶段的提示词，由 `xiaoqing_agent` 的 `prompts.py` 按 `## 判断`、`## 执行` 分节逐轮读取） |
| `media/qq_face_builtin_catalog.json` | QQ 表情内置目录，`qq_face_catalog.py` 与运行时观察目录合并；`plugin.json` 的 `watch_files` 监听它 |
| `utils/` | `json_parsing.py`（收窄与修复模型 JSON）、`text.py`（`clip_text`）、`wall_clock.py`（按 Core 时区格式化时间） |

---

## ⌨️ 框架入口

| 入口 | 调用方 | 职责 |
|---|---|---|
| `init(context)` | PluginManager | 开放后台任务、绑定 store、加载媒体注册表 |
| `handle(command, args, event, context)` | Router | `/xc` 命令。`/xc` 后的聊天内容交给 `handle_smalltalk`，按直接回复起草（`force_reason=command`） |
| `observe_message(clean_text, event, context)` | Dispatcher | 记录入站消息与原始消息段 |
| `handle_smalltalk(clean_text, event, context)` | Dispatcher | 闲聊决策与回复链路 |
| `call_bot_name_only(context)` | Core | 只喊名字、只 `@` 她时的预设短应答（`name_call.py`，由 `main.py` 导出），不走模型 |
| `observe_outgoing_action(action, context)` | Core service | 记录其他插件以角色身份发出的消息。本插件自己的回复已在回执提交时连同消息 ID 记下，这里跳过。带敏感标记的动作直接跳过 |
| `scheduled_daily_life(context)` | 定时任务（每天 05:30） | 安排今天的生活小事；`life.enable_daily_life` 关闭时不做事 |
| `scheduled_life_memes(context)` | 定时任务（每小时 7、37 分） | 把 `xiaoqing_life` 的梗图同步进表情库；`life.enable_life_memes` 关闭或能力缺失时不做事 |
| `scheduled_memory_sync(context)` | 定时任务（每小时 13、43 分） | 先重试待清的长期记忆，再把各会话还没推给统一记忆的消息推过去 |
| `scheduled_proactive(context)` | 定时任务（每 20 分钟，目标群） | 群聊与私聊主动开口；`proactive.enabled` 关闭时不做事 |
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
  ├─ 用户配置的屏蔽词 → 结束
  ├─ 长空档 → 切换短期状态（保留原始历史）
  ├─ attention_gate.decide_attention        记录显式受众与私聊直接回复标记
  ├─ life.closing_guard                     私聊里只是收尾，且角色已收尾 → 结束
  ├─ life.schedule                          作息底色、插话与延迟乘数写到事件上
  ├─ frequency_control.decide_reply         群聊：硬频控 → 概率 → Heartflow → 沉默补偿 → 作息乘数
  ├─ 记录本轮观察到的目标
  ├─ follow_ups                             同一个人后面还排着聊天消息 → 这条记下不回，等最后一条一起回
  └─ PreparedSmalltalkTurn
  │
  ▼
agent_bridge（私聊或群里明确跟她说话，且 agent 已开启时）
  ├─ agent decide 与下面的聊天生成同时开始
  ├─ 判断为闲聊 / agent 不可用 / 出错 → 继续用生成好的回复（生成先完成时等判断出来再发）
  └─ agent 接手 → 放弃生成并让出名额 → agent act → 回复带回执返回 Core
  │
  ▼
generation_limiter.admit                    全局 / 会话 / 用户 / 日配额；会话配额排队，最新一轮优先；等上一轮回复结算
  │
  ▼
turn_generation.generate_smalltalk_turn
  ├─ 会话锁内：确保用户消息已记录，取 PFC 快照
  └─ 三条路径之一：
       forced  → 私聊和群里明确跟她说的话直接起草
       planner → 没人叫她的群消息进入 run_pfc_once → 发言决定后起草
       direct  → 私聊可选规划关闭时直接起草
         │
         ▼
       reply_plan.build（memory.recall 取一次）→ reply_generator 尝试循环
         ├─ 预读本轮（always 模式，与生成并行，各次尝试共用）
         ├─ prompt_builder → gateway(chat) → postprocess
         ├─ marker_resolver 解析媒体意图
         ├─ reply_checker：本地规则 + 语义审查
         └─ 被拒 → reply_fallbacks：定向重写 / 重规划 / 兜底 / 安静
  │
  ▼
turn_delivery.finalize_smalltalk_turn
  ├─ 过期检查（超出最近 15 条、会话重置）；新用户消息触发上下文复核，气泡之间与末条前再次检查
  ├─ 生成期间对方又发来了消息（follow_ups）→ 这条回复不发，由排在后面的那一轮回
  ├─ 要接的话不在最近 5 条里时首个气泡引用原消息
  ├─ 打字延迟（目标时长扣除已用时间），分批发送，气泡间隔（正向 WS 下中间气泡绕过 Core 的事件收集直接发出）
  ├─ 每个气泡单独结算回执：中途因过期停发，或某个气泡发送失败、结果未知时，聊天记录只记已发出的气泡
  │  （结果未知的算可能已被看到），没发出去的不记
  ├─ 投递回执确认 → 提交：助手消息（连同回执收集的全部消息 ID）、PFC、目标、Heartflow、行动历史
  │                 → 聊天记录落盘后推给 memory.record
  │                 → post_reply_tasks：表达学习、策略复盘、未完话头结算、生活记录抽取
  └─ 一个气泡都没发出 / 投递拒绝 / 异常 → 回滚待提交状态
```

阶段之间通过 `PreparedSmalltalkTurn`、`GeneratedSmalltalkTurn` 和 `ReplyPayload` 模型传递数据。规划器选择 `wait` / `listening` 时本轮立即以不发言结束，Core 按“群 + 用户”串行处理事件。handler 内等待会推迟同一个人后续消息的处理，并占住会话生成配额。被审查拒绝的普通参与轮次会写回 PFC 快照，让下一轮从新状态出发。

---

## 📌 召唤与参与

`attention_gate.decide_attention` 返回 `AttentionDecision`：

| 字段 | 含义 |
|---|---|
| `forced` | 直接回复标记：私聊（按配置），以及群里 `@` 她、引用她的话或叫她名字（`chat_event.is_called_by_name`；`@` 了别人而没 `@` 她时不算） |
| `force_reason` | `command`、`private`、`addressed`，或 `continued`（他上一条是在跟她说话、她等他说完没回，接着发的这一条也算） |
| `mentioned` / `direct_mentioned` | 命令、私聊、引用、叫名字或 `@` 的显式来源信息 |
| `reply_to_bot` | 引用的 `message_id` 是角色历史消息 |

普通参与由 `frequency_control.decide_reply` 决定，顺序固定：

1. `_hard_limit_reason`：最小间隔（活跃话题使用更短的间隔）、每分钟上限、连续冷却、低信息。点名他人不拦，交给规划器按话题判断。
   紧跟在她刚发的回复后面（两分钟内、没 `@` 别人）的那句话多半在接她的话：不算每分钟上限、不掷骰子，过了最小间隔和连续冷却就直接交给规划器（`follows_own_reply`）。
2. `_participation_probability`：基础概率。活跃话题按配置提高概率，取最大值。
3. Heartflow：`score - base_score` 的正值部分按 `probability + (1 - probability) × Δ` 抬高。
4. `_streak_adjusted`：连续沉默 3 轮 ×1.2（≤ 0.90），5 轮 ×1.4（≤ 0.95）。
5. 作息乘数：忙或睡时乘 `busy_participation_factor` / `asleep_participation_factor`，连续沉默越久压制越轻，6 轮以上减半（`_routine_factor_with_streak`）。
6. 掷骰，结果与概率、掷值、沉默轮数一起写入 `ReplyGateDecision`，`smalltalk.no_reply` 日志直接输出这些字段。

Core 提取正文时只保留文本段。`handlers._label_other_mentions` 把 `@` 其他成员的段落按原位置还原为 `@名字` 再进入有效文本、媒体合成和历史，规划器和回复提示依据这份文本及上下文判断接收对象和参与时机。

私聊深度对话模式跳过第 2～5 步。群里明确跟她说的话（`forced`）不经这里，和私聊一样直接起草（与 agent 并行见“单轮数据流”）。私聊通过后按配置选择 PFC 或直接起草。其余群消息通过后统一由 PFC 根据完整上下文决定回复或旁听。中间动作未完成时返回 wait。

---

## 🧠 规划与生成

PFC（`planning/pfc_engine.py`）每轮执行一次 `run_pfc_once`：读取会话片段、行动历史、目标和知识，由 `pfc_action_planner` 在 `reasoning` 路由上选择动作：

| 动作 | 结果 |
|---|---|
| `direct_reply` / `send_new_message` | 回调 `_TurnGenerator.planner_reply` 按动作模式起草 |
| `wait` / `listening` | 不发言，本轮立即结束。`wait_seconds` 只写入日志 |
| `rethink_goal` | `pfc_goal_analyzer` 更新目标 |
| `fetch_knowledge` | 经 `memory.search` 查当前会话能看到的笔记、页面和原始记录后再规划 |
| `end_conversation` | 结束话题，状态置为 ended |
| `block_and_ignore` | 忽略本轮 |

规划器的“最近行动概要”把连续的 `wait` / `listening` 合成一行条数，不附当时不开口的理由；群聊里她已经连续 3 条以上没出声时，时间提示写明沉默了多少条。否则前几轮的旁听理由会成为下一轮的先例，越听越不开口。

规划器解析失败时，私聊回退为 `direct_reply`，群聊回退为 `wait`。规划请求的单次与总超时取主配置 `reasoning` 路由，连续失败进入退避。

起草由 `reply_plan.py` 与 `reply_generator.py` 完成：

1. `ReplyGenerationPlan` 一次性收集：近期历史、统一记忆的取用结果（取哪些见 README“记忆取用”，取用时机见 `reply_plan.py` 一行）、表达与黑话、知识、关键词反应规则、规划目标、人设边界和前台调用参数。每次重试复用同一份计划。
2. `llm/prompt_builder.py` 生成消息列表。`llm/gateway.py` 通过当前插件的 AI capability 调用 `chat` 路由，会话或全局模型覆盖通过 `pinned_model` 生效。
   可发的表情和图片不是每轮都摆给她（`reply_plan.offers_outbound_media`）：对方要表情、要图时一定给，平常她最近 6 条没带过装饰才有三成机会给，其余轮次提示词里只说“这一条只发文字”。
   规划器的想法以“你刚才心里的盘算”交给生成；私聊和被点名的轮次没有经过规划器，提示词里不出现这一块。
3. `llm/postprocess.py` 清理并分段（规则见 README“投递”）；`media/marker_resolver.py` 解析 `[想发…]` 标记。
4. `llm/reply_checker.py` 审查。被拒后由 `reply_fallbacks.py` 选择下一步。
5. 上游返回 `request_too_large` 时把历史减半，再试一次。

后台任务统一使用 `llm_call_config(cfg, foreground=False)` 取得主配置 `reasoning` 路由的 `LLMCallConfig`，网关调用显式选择 `reasoning`。凭据、HTTP、超时、重试和 profile 回退都在 Core AI capability 内完成。

---

## ✅ 回复审查

### 本地规则（`llm/local_checks.py`）

规则的行为描述见 README“回复审查”，这里列函数和阈值。`heuristic_check` 依次检查前五项，其余各由自己的函数承担：

| 规则 | 函数 / 判定 |
|---|---|
| 空回复 | `heuristic_check` |
| 复读 | 与最近 `max_repeat_compare` 条角色消息完全相同或相似度 ≥ `similarity_threshold`；只发一个媒体时跳过复读比较 |
| 整句照搬 | 原样复述自己刚说过的整句（≥ 8 字） |
| 近似照搬 | 去掉 `[…]` 标记和标点后与最近 6 条角色消息的相似度 ≥ 0.75（`_near_copy_of_recent`，至少 12 字） |
| 连续发言 | 连续 `max_assistant_in_row` 条角色发言 |
| 自称 AI | `persona_break_check`：自称 AI、模型、机器人或谈起自己的提示词；否认、反问和假设不算 |
| 凭空链接 | `ungrounded_link_check`：网址、域名或邮箱在别人发的消息、读图结果和资料里都找不到；生成后、送审前就查 |
| 自述经历 | `grounded_self_history_check`：角色自称的经历、关系、近况需要依据 |
| 群友经历 | `grounded_context_history_check`：群友经历需要依据 |
| 身份一致 | `persona_identity_consistency_check`：性别等身份表述与人设一致 |
| 只发媒体 | `media_meta_reply_check`：只发媒体却在答需要文字的问题 |
| 编造动机 | `narrated_motive_check`：没人问原因，回复却以“因为”起头解释自己为什么接话；生成后、送审前就查，按 soft 重写 |

`requires_llm_semantic_check` 决定 `risk` 模式下是否需要语义审查。

### 语义审查（`llm/reply_checker.py`）

审查提示词与 JSON 材料分为独立消息。三段提示词（证据审查 `_EVIDENCE_REVIEW_PROMPT`、独立阅读 `_TURN_READING_PROMPT`、语义比较 `_SEMANTIC_REVIEW_PROMPT`）和人设卡一样写成几条原则，具体的边界交给人设卡和本地证据核对。审查模型先列出陈述与证据，再给出判断：

| 轴 | 字段 |
|---|---|
| 上下文 | `context_coherent`、`context_claims`（以 `source_ids` 引用对话证据编号） |
| 说话人 | `speaker_correct` |
| 交流要求 | `instruction_followed` |
| 事实 | `factually_plausible` |
| 人设 | `persona_grounded`、`persona_claims`：按人设坚持真人身份、用玩笑带过属于符合人设。承认是 AI / 模型 / 程序，或提到提示词、系统设定、指令时为 false。`agrees_offline` 单独记她有没有答应线下见面、一起出门、收寄东西或语音视频；人设卡写明“只在线上来往”时为 true 即判人设硬错误 |
| 图片证据 | `image_evidence_respected`、`visual_claims` |
| 模板化 | `non_template` |

`llm/evidence.py` 按原文核对每条陈述的出处、主体与语气；这一轮从统一记忆取到的、关于人和群的每一行也编进证据索引（`m1`、`m2`……），关于群友的陈述可以引用她记得的事。`llm/arithmetic.py` 核对数值推导。部分模型把 `response_format` 当成要输出的外壳，把审查结论放进 `{"type": "json_object", "content": …}` 的 `content`（对象或 JSON 字符串），`_checker_payload` 同样解析。

`enable_semantic_checker` 开启时，另有一次只接收原话的独立阅读，再与候选比较立场、主体、时间和完成状态。独立阅读使用对话原文、当前消息、发言者、本群用语和角色名，候选内容独立于这些材料。两路候选审查并行执行，共用主配置 `ai.routes.checker.total_timeout_seconds`，任一路确认的硬错误都会生效。`TurnReadingCache` 按材料缓存一份阅读，并管理它的超时复用与任务回收：

- `always` 模式下，`reply_generator` 在请求候选前启动预读；`risk` 模式在需要远程审查时启动阅读。
- 语义审查等待阅读结果，再比较候选。
- 重写时材料相同则复用；上下文缩减或材料变化时重新阅读。
- 审查超时取消本次等待，进行中的阅读留给下一次尝试。
- 阅读失败或无效时，当前审查记为不可用并清空缓存，下一次尝试重新阅读。
- 生成结束时取消并回收剩余阅读任务。

图像证据由 `llm/media_evidence.py` 统一提供给生成与审查，区分“附件存在”和“画面已读出”。缺图时的说明要求像群友一样随口说图没加载出来，不使用画面、读取、解析这类说法。附和用户对未知画面的评价也算确认画面。

### 不可用策略

审查模型不可用时结果为 `severity="infra"`、`failure_code="checker_unavailable"`：

- `always`：不发送未审查的候选。私聊直接回复路径返回 `NOT_READY_FALLBACK`，普通参与安静。
- `risk`：放行通过本地规则的候选。未开启低风险人设发挥且问题涉及人设时，按 `persona_grounding` 拒绝。

### 重写与兜底（`reply_fallbacks.py`）

| 情况 | 处理 |
|---|---|
| soft 风格问题（`instruction_following`、`media_grounding`、`checker_unavailable` 除外）且本地规则通过 | 照发，记 `reply.check.soft_pass`，不重写 |
| 只有几处读图说法没依据、其余都审过 | 删掉含这几处的气泡照发其余的（`reply.check.trimmed`）；删完不剩时按下一行处理 |
| 还有重写次数 | 按 `failure_code` 生成定向修正提示（soft 只调语气措辞，hard 只承接可见依据） |
| 普通参与，耗尽 | `persona_grounding` / `context_grounding` / `instruction_following` 安静。其他需要重规划的问题抛出 `ReplyRejected` 交给 PFC。不需要重规划时安静 |
| 私聊直接回复，耗尽，本地规则通过，且只剩 soft，或 hard 问题不属于人设、读图、没审成这三类 | 接受该候选（`reply.check.exhausted.accept`） |
| 私聊直接回复，耗尽，人设 / 读图类 hard 或本地规则不过 | 按原因选择兜底句（`reply.check.exhausted.forced_fallback`），跳过与最近发言重复的句子，最后退到 `PRESENCE_FALLBACK` |

兜底句集合为 `ALL_FALLBACK_TEXTS`，评测脚本据此识别审查耗尽后的回复。

---

## 🖼️ 多模态管线

### 入站

`media/event_media.py` 验证来源，按原始顺序重建 `text`、`at`、`reply`、`face`、`mface`、`image`。`media/sources.py` 取字节：事件 URL、data URL / base64、授权本地路径，失效时通过 `onebot_media` capability 调用 `get_msg` / `get_image`（多附件按文件标识匹配）。`media/loading.py` 有界读取和解码，`media/event_media_analysis.py` 调用 `vision` 路由。梗图额外请求一次梗背景提示。摘要按媒体哈希写入 `media/render_cache.py`，提示词版本变化时失效重算。

所有媒体受字节数、像素、动图帧数、超时、重试和收件区配额约束。表情包进入按隐私范围隔离的 `emoji_library`，相似图按感知哈希合并。`media_registry` 为历史保留稳定引用，历史图片在窗口截短后仍以摘要形式提供给后续追问。

### 出站

`media/marker_resolver.py` 解析 `[想发表情:…]`、`[想发QQ表情:…]`、`[想发图片:…]`，从表情库、图片库、历史媒体和 QQ 表情目录中选择已授权素材。没有匹配时移除标记。`reply_payload.py` 编排有序批次，`reply_media.py` 在投递确认后记录素材使用。

---

## 💾 状态与持久化

### 进程内状态（`runtime_state.py`）

- 每个会话的统计、锁、回复时间戳、连续回复计数、冷却与当前回复元数据。
- 会话级与全局模型覆盖。
- 媒体细化与后台学习任务、防抖刷盘任务。
- `GenerationLimiter`。

### 持久化

所有 store 继承 `store_base.py`，由 `store_binding.py` 绑定到 `context.data_dir`，通过 Core 原子存储发布并保留 `.bak`。读取时校验对象根、整数、有限浮点、布尔和集合元素。主文件损坏时恢复有效备份，主备都不可用时隔离损坏文件。`io_persist_debounce_seconds` 控制防抖。

| 规则 | 实现 |
|---|---|
| 消息编号 | 首次分配时读取已有历史的最大 `mN`，覆盖重启、闲置重置和缓存淘汰后的续写 |
| QQ 消息 ID | 角色发言在投递回执提交时连同全部消息 ID 写入（`message_id` 为第一条，`extra_message_ids` 为拆分后的其余各条），不事后回填。引用判断检查记录的全部 ID |
| 会话锁 | 锁缓存 + 弱引用注册表。缓存淘汰后持有者与等待者仍共享原锁 |
| 重置代际 | `person_profiles/<会话>/.generation` 标记。`reset_chat_session` 先清聊天历史，再在共享路径锁内更新标记、清理人物资料文件和话题摘要文件（`hippo_memorizer/`）、删除这个会话在向量库里的文档，接着清各 store 里这个会话的状态，最后清长期记忆（下一行）。后台任务在远程调用前记录代际，提交时核对；推给统一记忆的幂等键也带代际 |
| 长期记忆的清除 | `memory/client.py` 的 `forget_chat(context, chat_id)` 在这个会话的推送锁内让记忆插件清空它的范围（`memory.forget`，`all=True`，超时 `RECORD_TIMEOUT` 8 秒）。**返回值**：`True` 只表示记忆插件确认已清掉；服务不在、超时或出错时返回 `False`，本地状态照样已清空，`commands.py` 的重置回复据此说明没能确认。**待清记录**：没得到确认的会话记进 `memory_outbox.json` 的 `forgetting`（`mark_forgetting`），确认后 `mark_cleared`。**待清期间不推不读**：`push_pending` 先重试清除，没清成就不推；`recall` / `search` 经 `_awaiting_forget` 直接返回空。**重试时机**：这个会话的下一次推送；`scheduled_memory_sync` 开头的 `retry_forgets`（每小时 13、43 分，覆盖重置后还没有新消息、没有历史文件的会话）；任何会话来消息时，`handlers._retry_pending_forgets` 经 `forget_retry_due` 在后台试一次，两次之间至少隔 `FORGET_RETRY_SECONDS`（30 秒，按数据目录计），不等结果 |
| 表达与黑话 | 加载结果携带基线，保存时以基线向最新磁盘状态合并增量 |
| 表情库 | 收集和使用计数在索引路径锁内提交。耗时修复在锁外分析，按旧快照校验后提交差量 |
| 连续会话边界 | 到达 `conversation_idle_gap_seconds` 时保留原始历史，目标、PFC 和行动历史切换到新片段 |

### 关闭

`shutdown()` 先停止接收新任务并刷盘，再取消后台任务并等待至多 2 秒。全部结束后执行最终刷盘。仍有任务存活时抛出 `TimeoutError`，由 Core 生命周期记录关闭失败。Core 的关闭预算由所有插件共享。

---

## ⚙️ 配置生命周期

| 来源 | 内容 |
|---|---|
| `config/config.json` → `bot_name` | 角色名字 |
| `config/config.json` → `plugins.xiaoqing_chat` | AI route、模型别名。也可写行为字段 |
| `plugins/xiaoqing_chat/config/xiaoqing_config.json` | 行为、频控、规划、记忆、媒体、人设开关（与上一项逐层合并，config 优先） |
| `plugins/xiaoqing_chat/config/persona.md`（或 `personality.persona_file`） | 人设卡：人物事实、说话方式、回复示例、心境 |
| `config/secrets.json` | Provider API Key |

`chat_runtime.py` 读取 Core 原子快照并维护文件热加载缓存（配置文件或人设卡任一改动都会重新加载）。`config/config.py` 校验字段范围与交叉约束（如单会话、单用户并发不超过全局并发）。每轮只使用自己的快照。

人设卡在加载配置时读入 `PersonalityConfig.card`。不带标记的人物事实交给规划、表达学习和审查（作为受控人物资料）。生成时人物事实放在系统提示开头，〔聊天〕各节（说话方式、示例）放在系统提示末尾、离对话最近。agent 交接时只交人物事实和〔办事〕各节。`persona.py` 把快照中的 `bot_name` 填进人设卡的 `{{name}}`。人设卡不写名字。历史中的角色消息按 `assistant` 角色存储，渲染时使用当前名字。

---

## 🔐 错误与日志

模块内的解析、类型转换和文件操作捕获具体异常。AI provider、动态 capability、媒体解码、后台任务和生命周期属于隔离边界，统一记录脱敏错误类别并返回受控降级结果。闲聊轮次里没预料到的异常（包括聊天模型整条链失败）由 `handlers._reply_after_failure` 收口：经 `log_public_error` 记一条 `public_error` 日志，聊天里不出现“操作失败：…”那样的错误说明；没人叫她的轮次不出声，私聊或被点名时回一句“卡了”，同一会话十分钟内只说一次。

每个模块用 `logging.getLogger(__name__)` 取 logger。流程中的每一步记一条 INFO 级的 `LogEvent(<步骤名>, chat_id=…, …)`，没有开关；读图失败、换服务商、agent 退回普通聊天这类降级记 WARNING。字段按 Core 的统一规则脱敏：QQ 号、群号、`chat_id`、profile 和原因码这类标签明文，消息正文、回复、提示词和 URL 只记长度和指纹，异常记类型、位置和状态码一类的属性，不记异常消息。发给模型的提示词按消息记 DEBUG 级的 `reply.prompt`（角色、序号、正文长度与指纹）。各阶段耗时由 `timing.py` 累计进本轮记录，在 `smalltalk.done` 里展开成 `timings.<阶段>` 字段，`elapsed_s` 是整轮耗时。主要步骤：

| 步骤 | 含义 |
|---|---|
| `smalltalk.recv` | 收到消息，带召唤判定字段 |
| `smalltalk.no_reply` | 门控未通过，带 `reason_code`、概率、掷值 |
| `smalltalk.ignore` / `smalltalk.closing_silence` | 屏蔽词 / 私聊已经收过尾 |
| `smalltalk.yield_to_follow_up` | 同一个人后面还排着消息，这一条不回 |
| `smalltalk.stale.drop` | 生成好的回复不发：目标过期、话题已变，或对方又发来了消息（`reason_code=sender_continued`） |
| `smalltalk.agent.decided` / `declined` / `handled` / `question_withheld` | agent 判断接手 / 不接手 / 已交回当轮回复 / 反问被收回 |
| `smalltalk.generation_limited` | 生成配额不足 |
| `smalltalk.forced_direct` / `smalltalk.pfc.done` / `smalltalk.planner.disabled` | 三条生成路径 |
| `reply.checker.skip` | 本地规则在送审前拦下候选 |
| `reply.check.start` / `result` / `regen` / `timeout` / `error` | 审查过程 |
| `reply.check.soft_pass` / `reply.check.trimmed` | 软问题照发 / 删掉没依据的读图气泡后照发 |
| `reply.check.exhausted.accept` / `forced_fallback` / `proactive_silence` | 重写耗尽后的处理 |

---

## 🛠️ 扩展位置

| 要改的行为 | 位置 |
|---|---|
| 召唤条件 | `attention_gate.py` |
| 低信息消息 | `participation.py` |
| 叫她名字的识别 | `chat_event.py` |
| 连发消息的等待 | `follow_ups.py` |
| 频控与概率 | `frequency_control.py` |
| 规划 | `planning/` |
| 生成上下文 | `context_builder.py`、`reply_plan.py`、`llm/prompt_builder.py` |
| 人设 | `config/persona.md`（人设卡）、`persona.py`、`config/config.py` 的 `PersonalityConfig` |
| 审查规则 | `llm/local_checks.py`（本地）、`llm/reply_checker.py`（语义） |
| 被拒后的处理 | `reply_fallbacks.py` |
| 入站媒体 | `media/event_media.py`、`media/sources.py`、`media/event_media_analysis.py` |
| 出站媒体 | `media/marker_resolver.py`、`reply_payload.py` |
| 长期记忆的读写 | `memory/client.py`；记忆本身在 `plugins/xiaoqing_memory/` |
| 表达与黑话 | `expression/` |
| 持久化 | `store_base.py` |

新增领域入口配套单元测试。跨层流程配套 `handle_smalltalk()` 级测试或真实模型评测剧本。持久化变更配套损坏输入、并发和重启测试。见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

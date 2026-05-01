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
├── main.py                         # 插件入口：init / handle / observe_message / shutdown
├── handlers.py                     # 主命令、smalltalk 主流程、观察消息、回复记录
├── attention_gate.py               # directed attention 判定：@、名字、reply-to-bot、上下文共指
├── frequency_control.py            # 普通群聊 participation gate 与硬频控
├── reply_generator.py              # 回复 draft、planner 输出合并、媒体 marker 附加
├── reply_payload.py                # 回复结果结构
├── context_builder.py              # 主回复 prompt 上下文构造
├── runtime_state.py                # per-chat runtime、短期状态和全局状态管理
├── handler_context.py              # 运行时 context 绑定
├── smalltalk_execution.py          # smalltalk 执行模型
├── smalltalk_media_helpers.py      # smalltalk 媒体辅助逻辑
├── config/
│   ├── config.py                   # 配置 dataclass 和 loader
│   └── xiaoqing_config.json        # 插件行为配置
├── llm/
│   ├── llm_client.py               # OpenAI-compatible chat completion client
│   ├── llm_config.py               # provider 读取与切换
│   ├── prompt_builder.py           # prompt 组装
│   ├── postprocess.py              # 回复后处理
│   ├── reply_checker.py            # 回复检查器
│   ├── summarizer.py               # 话题摘要
│   └── control_payload.py          # LLM 控制输出结构
├── media/
│   ├── event_media.py              # 入站媒体 segment 渲染与视觉描述
│   ├── event_media_analysis.py     # 图片/表情分析
│   ├── marker_resolver.py          # 出站 [想发...] marker 解析
│   ├── emoji_library.py            # 本地表情包库
│   ├── qq_face.py                  # QQ face 输出
│   ├── qq_face_catalog.py          # QQ face catalog 查询
│   └── qq_face_builtin_catalog.json
├── memory/
│   ├── memory.py                   # 对话记忆主接口
│   ├── memory_db.py                # 存储
│   ├── vector_store.py             # 向量检索
│   ├── memory_retrieval.py         # 相关记忆检索
│   ├── person_profile.py           # 人物资料
│   ├── topic_summary_cache.py      # 话题摘要缓存
│   ├── thinking_back.py            # thinking back 缓存
│   └── knowledge_*.py              # 可选知识库
├── planning/
│   ├── pfc_engine.py               # PFC 引擎
│   ├── pfc_action_planner.py       # 行动规划
│   ├── pfc_goal_analyzer.py        # 目标识别
│   ├── pfc_state.py                # PFC 状态
│   ├── planned_action.py           # action 数据结构
│   ├── goal_state.py               # goal 状态
│   ├── heartflow.py                # 普通参与软评分
│   └── action_history.py           # 回复/观察 action 记录
├── expression/
│   ├── bw_message_recorder.py      # 群聊表达样本记录
│   ├── bw_expression_learner.py    # 表达学习
│   ├── bw_expression_store.py      # 表达存储
│   ├── bw_jargon_miner.py          # 黑话挖掘
│   └── bw_jargon_store.py
├── experiments/
│   └── anthropomorphic_group.py    # 拟人大群实验 runner
└── utils/
    ├── json_parsing.py
    └── tool_info.py
```

## 入口层

`main.py` 暴露给 XiaoQing 框架的入口如下。

- `init(context)`: 绑定运行时配置和全局状态。
- `handle(command, args, event, context)`: 处理 `/xc` 命令。
- `call_bot_name_only(context)`: 用户只喊 bot name 时的短回复。
- `observe_message(clean_text, event, context)`: 观察消息但不一定回复。
- `observe_outgoing_action(action, context)`: 观察其它插件或机器人出站行为。
- `shutdown(context)`: 清理后台任务。

`handlers.py` 是插件主控层。显式命令和 smalltalk 都会进入这里，然后再分发到配置、记忆、表达、模型切换、深度对话或普通聊天链路。

## smalltalk 主流程

当 `smalltalk_provider = xiaoqing_chat` 时，框架把群聊消息交给 `handle_smalltalk(clean_text, event, context)`。主流程可以概括为以下步骤。

```text
handle_smalltalk()
  |
  v
_prepare_smalltalk_turn()
  - resolve chat_id / user_id
  - rebuild effective user text from message segments
  - ensure user message recorded
  - decide_attention()
  - ordinary gate: _should_reply()
  - maybe PFC planner
  |
  v
_generate_smalltalk_turn()
  - build context
  - call main reply LLM
  - attach or resolve media marker
  - run reply checker
  |
  v
_finalize_smalltalk_turn()
  - record bot reply
  - update action history / goal / heartflow
  - return OneBot segments
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
- active topic 短间隔。
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

## 主回复 LLM

主回复 LLM 由 `reply_generator.py`、`context_builder.py`、`llm/prompt_builder.py` 和 `llm/llm_client.py` 协作完成。它能看到以下上下文。

- 当前有效用户输入。
- 当前 chat 的近期历史。
- 相关长期记忆和人物资料。
- goal state 和 PFC planner 结果。
- expression store 中选出的口癖、黑话或表达习惯。
- 入站媒体 marker 和视觉描述。
- 媒体回复 marker 协议说明。

主 LLM 的输出先形成 `ReplyDraft`，再进入 postprocess、媒体 marker 解析和 reply checker。

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

`llm/reply_checker.py` 做两层检查。

- 启发式检查：重复、过长、连续回复、明显空泛或格式异常。
- 可选 LLM 检查：判断回复是否自然、是否应该重写或重新规划。

检查器会与 `max_regen`、`max_replan` 配合：能重写就重写，能重新规划就重新规划，超过上限后按安全策略放行或沉默。

## 状态和存储

`runtime_state.py` 管理 per-chat runtime。核心状态包括以下内容。

- recent messages。
- pending bot-name call。
- last reply metadata。
- reply gate logs。
- background media refine tasks。
- PFC state、goal state、action history、heartflow。

长期存储分布在 memory、expression、media library 和各自 store 中。状态应按 chat_id 隔离，避免不同群或私聊串台。

## 配置读取

插件配置分两层。

- `plugins/xiaoqing_chat/config/xiaoqing_config.json`: 行为配置，包括频控、planner、memory、reply checker、media、personality、postprocess、debug。
- `config/secrets.json`: LLM provider 和视觉 provider 的 API Base、API Key、Model、Endpoint、Proxy。

运行时通过 `/xc 配置` 查看摘要，通过 `/xc 模型` 查看或切换聊天 provider。

## 实验和测试

自动化测试主要覆盖以下内容。

- 命令和 smalltalk 主路径。
- attention gate 和 coreference。
- media segment 接收、marker 解析、图片/表情/QQ face 出站。
- reply checker。
- memory、planner、state reset。

常用命令如下。

```powershell
python -m pytest tests/plugins/test_xiaoqing_chat.py -q
python -m pytest tests/plugins/test_xiaoqing_chat_media.py -q
python -m pytest tests/plugins/test_reply_checker.py -q
python -m pytest tests -k "xiaoqing or reply_checker" -q
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

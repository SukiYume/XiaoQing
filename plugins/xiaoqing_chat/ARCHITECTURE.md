# 🏗️ xiaoqing_chat 架构

本文面向插件维护者，说明 `xiaoqing_chat` 的服务边界、主数据流、状态提交规则和扩展位置。用户入口、配置与排障见 [README.md](README.md)。

---

## 🏗️ 架构职责

插件在 XiaoQing Core 提供的命令、闲聊、AI、存储和 OneBot 能力上构建聊天运行时，负责以下领域：

1. 判断消息与小青的关系及参与时机。
2. 构建连续会话、长期记忆、人物资料、目标和媒体上下文。
3. 规划回复、观察、等待和目标调整动作。
4. 调用聊天、推理、检查和视觉 route。
5. 将文本与媒体意图转换为 OneBot 批次。
6. 在投递确认后提交记忆、规划和学习状态。

Core 负责配置快照、权限主体、AI 凭据与传输、插件数据目录、消息投递和生命周期控制。插件通过 `PluginContext` 与 capability 接口使用这些服务。

---

## 🏗️ 分层结构

```text
XiaoQing Core
  ├─ command router ──────────────── /xc
  ├─ smalltalk dispatcher ───────── observe_message / handle_smalltalk
  ├─ AI capability ──────────────── chat / reasoning / checker / vision
  ├─ OneBot delivery ────────────── ordered batches + acknowledgement
  └─ PluginContext ──────────────── config / principal / data_dir / logger
                    │
                    ▼
xiaoqing_chat
  ├─ entry and command handlers
  ├─ attention and participation
  ├─ planning and generation
  ├─ reply checking and media rendering
  └─ scoped stores and background learning
```

---

## 🏗️ 目录与所有权

```text
plugins/xiaoqing_chat/
├── plugin.json                  # Manifest、命令、能力与热加载文件
├── main.py                      # 框架入口和命令路由
├── handlers.py                  # 闲聊主控与投递后提交
├── handlers_internal.py         # 管理命令实现
├── handlers_helper.py           # 投递后后台任务编排
├── handler_context.py           # 单轮配置、状态、route 和路径快照
├── attention_gate.py            # 明确召唤与上下文共指
├── participation.py             # 普通群聊参与线索
├── frequency_control.py         # 回复间隔、速率、冷却与概率
├── generation_limiter.py        # 全局、会话和用户生成配额
├── brain_chat.py                # 私聊深度对话参数
├── smalltalk_models.py          # 准备、生成、外发阶段模型
├── smalltalk_execution.py       # 生成、投递、提交与回滚
├── smalltalk_media_helpers.py   # 媒体同步与使用记录
├── reply_generator.py           # 上下文组装、草稿、重生成和检查
├── reply_payload.py             # 回复到 OneBot 批次的转换
├── reply_splitter.py            # 长回复拆分
├── message_parts.py             # 结构化文本与媒体片段
├── context_builder.py           # 记忆、画像、表达与知识上下文
├── runtime_state.py             # 运行状态、后台任务和模型覆盖
├── task_scheduler.py            # 防抖刷盘与后台任务生命周期
├── store_base.py                # JSON store 公共协议
├── store_binding.py             # Store 与 data_dir 绑定
├── media_registry.py            # 媒体引用和持久化元数据
├── helper_utils.py              # 标识、热加载与 route 上下文
├── logging_utils.py             # 结构化日志与脱敏
├── constants.py                 # 跨模块常量
├── config/                      # 行为配置模型与 JSON
├── llm/                         # AI gateway、prompt、后处理与检查器
├── media/                       # 入站分析、素材库与出站 marker
├── memory/                      # 记忆、画像、向量、摘要与审查
├── planning/                    # PFC、Goal、Heartflow 与行动历史
├── expression/                  # 表达、黑话、反思与样本记录
├── experiments/                 # 拟人大群实验 runner
└── utils/                       # JSON 解析与工具信息
```

每个模块维护单一领域语义。`handlers.py` 编排流程，领域判断保留在对应模块中，持久化由 store 层统一完成。

---

## ⌨️ 框架入口

`main.py` 暴露以下入口：

| 入口 | 调用方 | 职责 |
|---|---|---|
| `init(context)` | PluginManager | 开放后台任务、绑定 store、加载媒体注册表 |
| `handle(command, args, event, context)` | Router | 处理 `/xc` 命令 |
| `call_bot_name_only(context)` | Dispatcher | 响应单独 Bot 名称 |
| `observe_message(clean_text, event, context)` | Dispatcher | 记录入站消息与原始 segment |
| `handle_smalltalk(clean_text, event, context)` | Dispatcher | 执行闲聊决策与回复链路 |
| `observe_outgoing_action(action, context)` | Core service | 记录其它插件和 Bot 的出站动作 |
| `shutdown(context)` | PluginManager | 停止任务并刷盘脏状态 |

Manifest 声明 `onebot_media` capability，并向 Core 提供 `core.observe_outgoing_action` 服务回调。

---

## 💾 单轮数据流

```text
OneBot event
  │
  ▼
observe_message
  ├─ preserve raw segments
  ├─ normalize effective input
  └─ append scoped user memory once
  │
  ▼
prepare turn
  ├─ refresh idle conversation state
  ├─ decide attention
  ├─ evaluate ordinary participation
  ├─ apply generation limits
  └─ snapshot runtime and routes
  │
  ▼
generate turn
  ├─ PFC action for ordinary participation
  ├─ build context
  ├─ call chat or reasoning route
  ├─ post-process and resolve media intent
  └─ deterministic + semantic reply check
  │
  ▼
finalize turn
  ├─ reject stale candidate
  ├─ send ordered OneBot batches
  ├─ receive delivery acknowledgement
  ├─ commit assistant memory and planning state
  └─ schedule summary, profile and expression tasks
```

`_PreparedSmalltalkTurn`、`_GeneratedSmalltalkTurn` 和 `_ReplyEnvelope` 明确划分准备、生成与外发阶段。阶段模型减少跨函数共享的松散字典。

---

## 📌 召唤与普通参与

`attention_gate.py` 返回 `AttentionDecision`：

- `forced`：直接进入回复生成。
- `reason`：召唤来源。
- `direct_mentioned`：名称或 `@` 命中。
- `coreference_mentioned`：上下文共指命中。
- `reply_to_bot`：reply 指向 Bot 历史消息。

私聊、显式命令、群聊 `@`、Bot 名称、名称续问、reply-to-bot 和带锚点的共指属于明确召唤。

普通群聊由 `participation.py` 提取开放话题、目标对象、活跃话题和低信息信号；`frequency_control.py` 应用间隔、速率、冷却与参与概率；`planning/heartflow.py` 计算软评分；PFC 选择回复、观察、等待或目标调整。

`generation_limiter.py` 在模型调用前获取全局、会话和用户配额，并记录用户日用量。释放动作位于统一生命周期边界。

---

## 🧠 规划与生成

PFC 子系统维护四类信息：

- `goal_state.py`：当前会话目标与活跃话题。
- `pfc_state.py`：规划器观察和阶段状态。
- `planned_action.py`：本轮行动模型。
- `action_history.py`：已确认行动记录。

普通参与进入 PFC。明确召唤直接进入回复生成。开放群聊邀请在通过硬频控后可进入直接生成路径。

`reply_generator.py` 依次完成：

1. 从 `context_builder.py` 获取记忆、画像、表达、知识和媒体块。
2. 从 `llm/prompt_builder.py` 生成模型输入。
3. 通过 `llm/gateway.py` 调用绑定当前插件的 AI capability。
4. 由 `llm/postprocess.py` 清理和拆分文本。
5. 解析媒体 marker。
6. 执行回复检查与有界重生成。

`llm/llm_client.py` 维护响应提取接口。凭据、HTTP、超时、重试与 profile fallback 位于 Core AI capability。

---

## 🧠 AI route

插件声明四条用途明确的模型链：

| Route | 调用场景 |
|---|---|
| `chat` | 日常回复 |
| `reasoning` | PFC、科学关系、摘要和后台分析 |
| `checker` | 独立语义审查 |
| `vision` | 图片与表情语义分析 |

`handler_context.py` 为单轮创建配置与 route 快照。`/xc model` 修改运行时聊天模型覆盖项，覆盖范围分为 chat 和 global。`chat` route 接受覆盖项，其余 route 保持各自配置链。

每次调用只向 provider 传递该功能所需的输入。日志边界使用长度、指纹、profile 与错误类别表达运行状态。

---

## 🧠 多模态管线

### 入站

`media/event_media.py` 验证来源并按原始顺序重建 `text`、`at`、`reply`、`face`、`mface` 和 `image`。`event_media_analysis.py` 调用视觉 route，`event_media_common.py` 负责读取、编解码、缓存和 marker 公共逻辑。

图片分析受到字节数、像素、动画帧数、超时和重试配置约束。表情包素材进入按会话授权的本地库；媒体注册表为上下文保留稳定引用。

### 出站

`media/marker_resolver.py` 解析三种意图：

- `[想发表情:hint]`
- `[想发QQ表情:hint]`
- `[想发图片:hint]`

解析器从表情库、图片库、历史媒体与 QQ face catalog 选择候选。`reply_payload.py` 将文本与媒体转换为有序 OneBot 批次，`smalltalk_media_helpers.py` 在投递确认后记录素材使用情况。

---

## ✅ 回复检查与提交

`llm/reply_checker.py` 组合两类门禁：

- 确定性检查：重复、连续回复、结构、长度和媒体一致性。
- 语义检查：上下文、说话人、人物声明、外部事实、交流约束和模板化程度。

检查结果区分 hard 与 soft，并携带重生成或重规划建议。主动参与候选在耗尽预算后采用安静结果；明确召唤候选采用结构安全的承接结果。

投递确认是状态提交点。确认后依次提交助手记忆、PFC、Goal、Heartflow、行动历史和回复后学习任务。投递拒绝、发送异常与过期候选进入回滚路径。

---

## 💾 状态与持久化

`runtime_state.py` 管理进程内状态：

- 每个 chat 的统计、锁和当前回复元数据。
- Bot 名称续问状态。
- 模型覆盖项。
- 媒体细化与后台学习任务。
- 防抖刷盘任务。

持久化状态分布在 `memory/`、`planning/`、`expression/` 与 `media/`。所有 store 通过 `store_binding.py` 绑定 `context.data_dir`，通过 `store_base.py` 与 Core 原子存储协议完成发布。读取边界校验对象根、整数、有限浮点数、布尔值和集合元素。

`memory.conversation_idle_gap_seconds` 定义连续会话边界。到达边界时，短期原始记录继续留存，Goal、PFC、行动历史、Thinking Back 与话题摘要切换到新会话片段。

`task_scheduler.py` 管理防抖保存、任务登记、异常记录与关闭等待。`shutdown()` 先停止接收任务，再等待限时任务，最后刷盘所有脏 store。

---

## ⚙️ 配置生命周期

配置来源如下：

| 来源 | 内容 |
|---|---|
| `plugins/xiaoqing_chat/config/xiaoqing_config.json` | 行为、频控、规划、记忆、媒体、人格和调试 |
| `config/config.json` | AI provider、model profile、route 与 alias |
| `config/secrets.json` | Provider API Key |

`config/config.py` 使用 Pydantic 校验字段范围、交叉约束和正则表达式。`helper_utils.py` 读取 Core 原子快照并维护文件热加载缓存。单轮处理只使用自己的快照，下一轮获得最新有效配置。

---

## 🔐 错误与日志边界

模块内纯解析、类型转换和文件操作捕获具体异常。AI provider、动态 capability、媒体解码、后台任务和生命周期属于隔离边界，统一记录脱敏错误类别并返回受控降级结果。

`logging_utils.py` 为 chat、user、group、URL、profile 和自由文本提供脱敏表达。常规日志用于定位步骤、耗时、长度、决策原因与错误类别。

---

## 🛠️ 扩展规则

新增行为按领域放置：

- 召唤条件：`attention_gate.py`。
- 普通参与线索：`participation.py`。
- 速率与概率：`frequency_control.py`。
- 行为规划：`planning/`。
- 生成上下文：`context_builder.py` 与 `llm/prompt_builder.py`。
- 入站媒体：`media/event_media*.py`。
- 出站媒体：`media/marker_resolver.py` 与 `reply_payload.py`。
- 长期记忆和画像：`memory/`。
- 表达与黑话：`expression/`。
- 持久化公共能力：`store_base.py`。

每个新增领域入口配套单元测试；跨层流程配套 `handle_smalltalk()` 或 OneBot UAT；持久化变更配套损坏输入、并发和重启测试。完整门禁见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

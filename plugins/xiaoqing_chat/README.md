# 💬 xiaoqing_chat

`xiaoqing_chat` 为 XiaoQing 提供拟人聊天能力。插件结合群聊参与判断、近期上下文、长期记忆、人物资料、行为规划、多模态消息和回复检查，让小青以群友身份参与 QQ 对话。

本文件面向插件用户和机器人管理员。代码职责与内部数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)，上线验收流程见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

---

## 🧩 启用插件

依赖包由项目依赖统一安装。插件声明以下依赖：

- `aiohttp`：AI 与媒体请求。
- `numpy`：向量记忆。
- `pydantic`：行为配置校验。
- `Pillow`：图片与动图分析，可选。

在 `config/config.json` 中选择闲聊提供者：

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

行为配置位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`。AI provider、model profile 和 route 位于 `config/config.json`，API Key 位于 `config/secrets.json`。

完成配置后启动项目：

```bash
python main.py
```

---

## ⌨️ 命令

`/xc <内容>` 直接发起一轮对话。`/xc help` 返回插件的结构化命令目录。

| 命令 | 权限 | 作用 |
|---|---|---|
| `/xc <内容>` | 所有用户 | 与小青对话 |
| `/xc help` | 所有用户 | 查看完整命令目录 |
| `/xc reset` | 私聊用户 | 清理自己的会话状态 |
| `/xc reset confirm` | 群管理员、群主、Bot 管理员 | 确认清理当前群会话状态 |
| `/xc stats` | 所有用户 | 查看上下文、学习与运行统计 |
| `/xc brain` | 所有用户 | 查看私聊深度对话模式 |
| `/xc config` | 所有用户 | 查看行为与模型配置摘要 |
| `/xc memory <关键词>` | 所有用户 | 检索当前会话可见的长期记忆 |
| `/xc expression` | 所有用户 | 查看当前会话学到的表达方式 |
| `/xc jargon` | 所有用户 | 查看当前会话可见的黑话 |
| `/xc review <操作> <会话ID> [内容]` | 群管理员、群主、Bot 管理员 | 处理反思审查会话 |
| `/xc model` | 所有用户 | 查看聊天模型和回退顺序 |
| `/xc model <名称>` | 群管理员、群主、Bot 管理员 | 固定当前会话的聊天模型 |
| `/xc model default` | 群管理员、群主、Bot 管理员 | 恢复当前会话的配置路由 |
| `/xc model global <名称>` | Bot 管理员 | 固定全局运行时聊天模型 |
| `/xc model global default` | Bot 管理员 | 恢复全局配置路由 |

命令支持清单中的中文别名，例如 `帮助`、`清空`、`统计`、`深度`、`配置`、`记忆`、`表达`、`黑话`、`审查` 和 `模型`。模型选择保存在当前 Bot 进程内，进程重启后按配置路由恢复。

审查操作包括：

- `ok`：确认当前目标或进入下一步。
- `no`：关闭审查会话。
- `answer`：提交规则、目标或策略内容。
- `close`：关闭审查会话。

---

## 💬 群聊参与方式

插件先判断消息与小青的关系，再决定回复时机。

### 明确召唤

以下场景直接进入回复生成：

- `/xc <内容>`。
- 私聊消息。
- 群聊中 `@` 小青。
- 文本包含配置的 Bot 名称。
- 用户只喊 Bot 名称后继续追问。
- reply 引用小青的消息。
- 最近上下文已锚定小青，当前消息使用 `她`、`他` 或 `ta` 等共指召唤。

共指判定要求近期历史存在小青锚点，普通代词由群聊上下文继续处理。

### 普通参与

普通群聊消息依次经过以下控制：

1. 最小回复间隔、每分钟回复上限和连续回复冷却。
2. 基础参与概率 `reply_probability_base`。
3. 面向全群的问题、邀请和开场信号。
4. 上一轮目标形成的活跃话题状态。
5. Heartflow 参与评分。
6. PFC planner 的回复、观察、等待或目标调整决策。

点给其他群友的消息保持原对话流向。仅含 OneBot `face` 与标点的低信息消息进入安静状态。全局、会话和用户三级生成配额共同控制远程模型用量。

---

## 💬 回复链路

一轮回复按以下顺序执行：

1. 读取 OneBot 原始消息段并重建有效输入。
2. 根据空档阈值确定当前连续会话片段。
3. 完成召唤判定、普通参与控制和行为规划。
4. 组装近期历史、相关记忆、人物资料、目标、表达与媒体上下文。
5. 调用聊天 route 生成草稿。
6. 解析媒体意图并执行回复检查。
7. 构造有序 OneBot 消息批次。
8. 收到投递确认后提交助手记忆、行动记录、目标状态和后台学习任务。

投递拒绝与发送异常会丢弃当前候选状态，下一轮继续使用已确认的会话记录。

---

## 💬 多模态消息

### 入站消息

插件按原始顺序处理以下 OneBot segment：

- `text`：文本。
- `at`：群聊点名。
- `reply`：引用关系。
- `face`：QQ 原生表情。
- `mface`：NapCat 表情包。
- `image`：普通图片。

`media.enable_inbound_media_context` 控制媒体上下文。视觉 route 可为图片生成描述。表情包素材可进入 `data/xiaoqing_chat/media/library/`，供后续回复复用。

### 出站消息

聊天模型通过媒体意图 marker 表达素材需求：

```text
哈哈这个太贴切了 [想发表情:笑哭]
```

| Marker | 素材来源 |
|---|---|
| `[想发表情:hint]` | 本地表情包库 |
| `[想发QQ表情:hint]` | QQ face catalog |
| `[想发图片:hint]` | 图片库与历史图片素材 |

解析器将 marker 转换为 OneBot `image` 或 `face` segment。素材匹配为空时发送清理后的文本。

---

## 🧠 记忆与学习

插件维护以下会话信息：

- 短期历史：保存当前 chat 的原始消息；即时生成读取最近一次长空档后的连续片段。
- 语义记忆：按相似度检索长期记录。
- 人物资料：积累昵称、偏好和稳定事实。
- 话题摘要：压缩较长对话的主题。
- Goal 与 PFC：记录当前目标、观察和行动。
- Thinking Back：缓存近期回顾结果。
- 表达与黑话：学习当前群聊的表达方式和词汇。
- 反思审查：由管理员确认目标、规则和表达策略。

会话键区分群聊与私聊，各存储在读写边界应用 chat scope。`memory.conversation_idle_gap_seconds` 到期时保留原始历史，同时刷新目标、PFC、行动历史、Thinking Back 与话题摘要。

表达学习与表达注入由两个开关分别控制。当前配置开启学习，选择器由管理员按使用目标启用。人工审查结果进入反思策略。

---

## ⚙️ 行为配置

当前发行配置中的常用项如下：

```json
{
  "enable_smalltalk": true,
  "reply_probability_base": 0.55,
  "participation_cue_reply_probability": 0.9,
  "active_topic_reply_probability": 0.6,
  "active_topic_question_reply_probability": 0.9,
  "min_reply_interval_seconds": 8,
  "max_replies_per_minute": 4,
  "continuous_reply_limit": 3,
  "continuous_cooldown_seconds": 25,
  "max_context_size": 30,
  "planner": {
    "enable_planner": true,
    "think_mode": "dynamic"
  },
  "memory": {
    "enable_memory_retrieval": true,
    "conversation_idle_gap_seconds": 1800,
    "top_k": 3,
    "min_score": 0.12,
    "max_block_chars": 1200
  },
  "reply_check": {
    "enable_reply_checker": true,
    "enable_llm_checker": true,
    "llm_checker_mode": "risk",
    "timeout_seconds": 5.0,
    "max_regen": 1,
    "max_replan": 1
  },
  "heartflow": {
    "enable_heartflow": true,
    "base_score": 0.2
  },
  "expression": {
    "enable_expression_learning": true,
    "enable_expression_selector": false,
    "max_injected": 1
  },
  "media": {
    "enable_inbound_media_context": true,
    "max_media_per_message": 1
  }
}
```

完整字段、类型、范围和默认值由 `config/config.py` 中的 Pydantic 模型定义。插件在配置文件变化后加载新的有效快照；校验错误会记录字段路径并保留有效运行配置。

---

## 💾 AI 路由与数据

项目级 AI 注册表分为三部分：

- `ai.providers`：服务地址、端点和代理。
- `ai.models`：provider、模型名、模态和请求默认值。
- `plugins.xiaoqing_chat.ai.routes`：本插件的有序模型链。

插件使用四条 route：

| Route | 用途 |
|---|---|
| `chat` | 日常回复 |
| `reasoning` | 规划、科学关系和后台分析 |
| `checker` | 独立回复审查 |
| `vision` | 图片理解 |

`model_aliases` 为 `/xc model` 提供管理员可选名称。每条 route 按 `models` 顺序执行回退。Core AI capability 负责凭据、传输、超时、重试和 profile 切换。

远程 provider 会接收当前输入以及本次功能所需的历史、记忆、人物资料、规划结果和媒体上下文。部署者按 provider 条款管理数据用途、保留与删除。运行日志记录长度、指纹、profile 和脱敏错误类别。

---

## 💾 运行数据

框架为插件提供 `context.data_dir`，默认路径为 `data/xiaoqing_chat/`。其中保存：

- 对话记忆与人物资料。
- 向量索引与知识索引。
- Goal、PFC、行动历史和摘要。
- 表达、黑话与反思记录。
- 媒体库、媒体注册表和渲染缓存。

JSON 状态通过 Core 原子存储协议发布，并在字段边界完成类型校验。测试报告写入 `test_reports/runs/plugins/xiaoqing_chat/`。

---

## ✅ 验证

插件回归测试：

```bash
python -m compileall -q plugins/xiaoqing_chat
python -m pytest -q tests/plugins/test_xiaoqing*.py tests/plugins/test_*reply_checker*.py
```

拟人大群实验：

```bash
python -m plugins.xiaoqing_chat.experiments.anthropomorphic_group \
  --mode real \
  --run-id <RUN_ID> \
  --groups 20 \
  --rounds-per-group 150
```

实验 runner 调用真实 `observe_message()` 与 `handle_smalltalk()` 主路径，并把返回的 OneBot segment 和评分写入独立报告目录。上线前按 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md) 完成自动化与真实群聊验收。

---

## 🩺 排障

### 群聊参与率异常

1. 检查 `plugins.smalltalk_provider`。
2. 用 `/xc config` 查看行为摘要。
3. 检查回复间隔、每分钟上限、连续回复冷却和三级生成配额。
4. 对照日志中的 attention、participation、planner 和 reply-check 结果。

### 图片理解异常

1. 检查 `media.enable_inbound_media_context`。
2. 检查 OneBot 事件中的 `image`、`face` 与 `mface` segment。
3. 检查 `vision` route 的模型 profile 与 `image` 模态。
4. 检查媒体大小、像素、动画帧数和磁盘配额。

### 模型调用异常

1. 检查 provider、model profile、route 与 alias 的引用关系。
2. 检查 `secrets.ai.providers.<provider>.api_key`。
3. 用 `/xc model` 查看当前模型和回退顺序。
4. 检查认证、限流、代理、网络和服务端错误类别。

### 会话状态异常

1. 用 `/xc stats` 查看当前会话统计。
2. 确认事件的 `group_id`、`user_id` 与 reply `message_id`。
3. 检查 `context.data_dir` 中对应 store 的原子文件和备份。
4. 在测试会话中执行 `/xc reset` 或群聊确认流程。

---

## 🧭 相关文档

- [插件架构](ARCHITECTURE.md)
- [上线验收指南](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)
- [消息流程](../../docs/08-message-flow.md)
- [插件目录](../../docs/09-plugins.md)

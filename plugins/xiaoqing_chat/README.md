# 💬 xiaoqing_chat

`xiaoqing_chat` 是 XiaoQing 的拟人聊天插件。它以 `bot_name` 配置的角色身份混在 QQ 群和私聊里，像普通群友一样判断什么时候接话、接谁的话、说什么；能看懂图片和表情包，会记住群友说过的事，会学本群的说法和黑话，每条回复在发出前都经过独立审查。

本文面向使用者和机器人管理员：怎么启用、有哪些命令、角色怎么参与聊天、可以调哪些参数、数据存在哪里、出问题怎么查。

| 文档 | 读者 | 内容 |
|---|---|---|
| 本文 | 使用者、管理员 | 功能、命令、配置、数据与排障 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 插件维护者 | 模块职责、单轮数据流、审查与状态提交 |
| [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md) | 测试与发布 | 自动化测试、真实模型评测与上线验收 |

---

## ✨ 功能一览

| 能力 | 说明 |
|---|---|
| 群聊参与 | 区分“明确在叫角色”和“普通群聊”；普通消息按频控、参与线索、活跃话题、Heartflow 和 PFC 规划决定是否插话 |
| 私聊 | 私聊消息总是回复；可选“深度对话”模式 |
| 人设 | 固定的大二理工科女生群友，有自己的口味和观点；坚持自己是真人 |
| 多模态 | 读入文字、`@`、引用、QQ 表情、NapCat 表情包和图片；视觉模型生成图片摘要，后续纯文字追问仍可引用 |
| 媒体回复 | 角色可以发表情包、QQ 表情或图片，素材来自本地库和历史图片 |
| 长期记忆 | 按会话保存聊天历史、向量记忆、人物资料、话题摘要和近期回想 |
| 表达学习 | 学习本群的说法与黑话，审核后按话题注入回复 |
| 回复审查 | 本地规则 + 独立审查模型逐条核对上下文、事实、人设和图片证据，不合格就定向重写 |
| 投递确认 | 只有 OneBot 确认发送成功后才把回复写入记忆和规划状态 |

---

## 🚀 启用

### 1. 选择闲聊提供者

在 `config/config.json` 中把全局闲聊交给本插件：

```json
{
  "bot_name": "小青",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

`bot_name` 是角色的唯一名字，群聊召唤、提示词、规划、审查、表达学习和历史消息里的角色标签都使用它。改名后执行 `/reload`，后续轮次立即使用新名字。

### 2. 配置模型路由

插件使用四条 AI route，都在 `config/config.json` 的 `plugins.xiaoqing_chat.ai.routes` 下：

| Route | 用途 | 发行配置的模型链 | 单次 / 总超时 |
|---|---|---|---|
| `chat` | 日常回复 | `deepseek-flash → glm-5.2` | 15 s / 60 s，重试 2 次 |
| `reasoning` | PFC 规划、目标分析、摘要、人物事实提取、表达学习等后台任务 | `deepseek-flash-thinking → deepseek-pro → glm-5.2` | 15 s / 45 s，重试 1 次 |
| `checker` | 独立回复审查 | `deepseek-flash-thinking → deepseek-pro → glm-5.2`，路由级关闭思考 | 8 s / 20 s，不重试 |
| `vision` | 图片与表情包理解 | `glm-4.6v-flash → glm-4.1v-thinking-flash → glm-4v-flash → glm-4.6v` | 20 s / 70 s，重试 1 次 |

模型 profile 和 provider 定义在项目级 `config.ai`，API Key 在 `config/secrets.json` 的 `ai.providers.<provider>.api_key`。`vision` 链里的 profile 必须声明 `image` 模态。完整字段见 [配置详解的 AI 注册表](../../docs/06-configuration.md#aivlm-注册表)。

`checker` 路由通过 `request_defaults.thinking.type = "disabled"` 关闭思考，换取更短的审查延迟；其他路由沿用各自 profile 的默认值。

`plugins.xiaoqing_chat.ai` 还有两个字段供 `/xc model` 使用：

```json
{
  "default_model_alias": "deepseek",
  "model_aliases": {
    "deepseek": "deepseek-flash",
    "glm": "glm-5.2"
  }
}
```

### 3. 依赖

Manifest 声明的依赖由项目 `requirements.txt` 统一安装：

| 模块 | 是否必需 | 用途 |
|---|---|---|
| `aiohttp` | 必需 | 媒体下载与 HTTP 客户端 |
| `numpy` | 必需 | 本地向量记忆 |
| `pydantic` | 必需 | 行为配置校验 |
| `PIL`（Pillow） | 可选 | 图片与动图分析；缺失时纯文本聊天照常运行 |

插件还申请 Core 的 `onebot_media` capability（用 `get_msg` / `get_image` 找回失效的图片），并向 Core 提供 `core.observe_outgoing_action` 服务（记录其他插件发出的消息，作为聊天上下文）。

启动：

```bash
python main.py
```

---

## ⌨️ 命令

所有命令都挂在 `/xc` 下。`/xc` 后面跟的不是子命令时，整段文字作为一次明确召唤的聊天内容。

| 命令 | 别名 | 权限 | 作用 |
|---|---|---|---|
| `/xc <内容>` | — | 所有人 | 直接和角色说话，总会得到回复 |
| `/xc help` | `帮助`、`?` | 所有人 | 查看命令目录 |
| `/xc reset` | `重置`、`清空` | 私聊：本人 | 清空自己的私聊会话状态 |
| `/xc reset confirm` | `重置 确认` | 群管理员、群主、Bot 管理员 | 清空当前群的会话状态；群里不带 `confirm` 时只返回二次确认提示 |
| `/xc stats` | `统计`、`状态` | 所有人 | 当前会话的上下文、学习与运行统计 |
| `/xc brain` | `深度` | 所有人 | 查看私聊深度对话模式的开关和参数 |
| `/xc config` | `配置` | 所有人 | 查看行为、频控、记忆和模型配置摘要 |
| `/xc memory <关键词>` | `记忆` | 所有人 | 检索当前会话可见的长期记忆 |
| `/xc expression` | `表达` | 所有人 | 查看当前会话学到的表达 |
| `/xc jargon` | `黑话` | 所有人 | 查看当前会话可见的黑话及释义 |
| `/xc review <ok\|no\|answer\|close> <会话ID> [内容]` | `审查` | 群管理员、群主、Bot 管理员 | 处理人工复盘会话 |
| `/xc model` | `模型`、`provider`、`供应商` | 所有人 | 查看可选模型、当前选择和回退顺序 |
| `/xc model <名称>` | — | 群管理员、群主、Bot 管理员 | 当前会话的 `chat` 路由固定使用该模型 |
| `/xc model default` | `默认`、`reset` | 群管理员、群主、Bot 管理员 | 当前会话恢复配置路由 |
| `/xc model global <名称\|default>` | `全局` | Bot 管理员 | 所有会话的 `chat` 路由固定或恢复 |

说明：

- **模型切换**只影响 `chat` 路由，`reasoning`、`checker`、`vision` 始终按配置链运行。选择只保存在进程内，Bot 重启后回到配置路由。会话级选择优先于全局选择。
- **会话重置**清空当前会话的聊天历史、人物资料与向量记忆、目标、Heartflow、PFC 状态、行动历史、表达、黑话、学习水位、复盘会话与策略、近期回想、话题摘要和事实提取水位。重置前已经发出的后台任务（摘要、人物事实、表达学习）在提交时会发现代际变化而作废，不会把旧会话的内容写回来。媒体注册表按图片内容全局共享，不随单个会话清除。
- **复盘操作**：`ok` 确认当前目标或进入下一步；`answer` 提交规则、目标或策略文字；`no` 和 `close` 关闭会话。复盘功能默认关闭，见下文 [复盘与反思](#复盘与反思)。

---

## 💬 角色怎么参与聊天

每条进入插件的消息先判断“是不是在叫角色”，再决定要不要接话。

### 明确召唤：一定回复

以下任一情况都算明确召唤，跳过概率和规划，直接生成回复：

| 场景 | 例子 |
|---|---|
| `/xc` 命令 | `/xc 你好` |
| 私聊消息 | 任意私聊 |
| 群里 `@` 角色 | `@小青 在吗` |
| 消息里出现角色名字 | `小青你看这个` |
| 只喊名字后，同一个人在 60 秒内接着说 | `小青` → `今天吃啥` |
| 引用（reply）角色说过的消息 | 引用后说 `真的假的` |
| 近期上下文已经在聊角色，再用代词指她 | `她刚才说的对吗` |

代词召唤要求近期历史里确实有角色的锚点；指向其他群友的“她/他”不算。

只发角色名字时，角色回一句短应答（`bot_name_only_replies`，默认 `在呢`、`嗯？`、`怎么啦`、`我在`、`有事吗`），并在 60 秒内把同一个人的下一句当作续问。

### 普通群聊：先过硬限制，再掷概率

没有明确召唤的群消息按以下顺序判断：

1. **硬限制**：任一命中就不回。
   - 距上次回复不足 `min_reply_interval_seconds`（活跃话题里缩短到 `active_topic_min_reply_interval`，活跃话题中的追问再缩短到 `active_topic_question_min_reply_interval`）。
   - 最近 60 秒已回复 `max_replies_per_minute` 次。
   - 连续回复达到 `continuous_reply_limit` 后的 `continuous_cooldown_seconds` 冷却期内。
   - 低信息消息：只有 QQ 表情码、无内容的媒体占位和标点。
   - 句首明确点名其他群友，例如 `阿梨你…`、`@某人 …`、`张老师，…`。
2. **参与概率**：从 `reply_probability_base` 出发，满足以下情况时抬高到对应上限（取最大值，不叠加）：

   | 信号 | 识别方式 | 概率上限 |
   |---|---|---|
   | 面向全群的邀请 | `大家`、`你们`、`有没有人`、`谁来`… | `participation_cue_reply_probability` |
   | 没点名的发图请求 | `发个狗头`、`来张梗图`、`整点表情包` | 同上 |
   | 开放提问 | 问句 | 同上 |
   | 开场起哄 | `我宣布`、`我提议`、`我赌`、`我站…党` | 同上 |
   | 激动分享 | 连用感叹号的自述 | 同上 |
   | 复读队形 | 和上一位群友发了同一句短消息 | 同上 |
   | 活跃话题 | 当前目标 5 分钟内更新过，且角色 5 分钟内发过言 | `active_topic_reply_probability` |
   | 活跃话题里的追问 | 活跃话题 + 问句 | `active_topic_question_reply_probability` |

3. **Heartflow 软加分**（`heartflow.enable_heartflow`）：根据问句、与目标的相关度、消息长短、连续沉默轮数和距上次发言的时长给出评分，高于 `base_score` 的部分按比例抬高概率。
4. **沉默补偿**：连续 3 轮没回复时概率 ×1.2（上限 0.90），连续 5 轮时 ×1.4（上限 0.95）。
5. 通过概率后进入 **PFC 规划**（`planner.enable_planner`）。规划器读取近期对话、行动历史和目标，选择以下动作之一：

   | 动作 | 含义 |
   |---|---|
   | `direct_reply` | 回应当前消息 |
   | `send_new_message` | 主动开一个新话头 |
   | `listening` / `wait` | 先听，不说话；`wait` 会等待一小段时间再判断 |
   | `rethink_goal` | 重新分析当前会话目标 |
   | `fetch_knowledge` | 先检索知识库再决定 |
   | `end_conversation` | 结束当前话题 |
   | `block_and_ignore` | 忽略明显恶意或刷屏的消息 |

   规划器连续失败时进入退避：`pfc_planner_fail_window_seconds` 内失败 `pfc_planner_fail_threshold` 次后暂停 `pfc_planner_backoff_seconds` 秒。规划关闭时，通过概率的消息直接生成回复。

另外两种情况本轮直接保持安静，明确召唤也不例外：发言人明确要求别回（如“别回了”“不用回复”“别插话”“不要打扰我”“让我静一静”，带问句或后续请求的除外），以及消息命中 `ban_words` / `ban_regex`。

### 生成配额

进入模型生成前还要拿到配额，防止刷屏消耗远程模型：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `max_generation_inflight_global` | 4 | 全局同时进行的生成数 |
| `max_generation_inflight_per_chat` | 1 | 单个会话同时进行的生成数 |
| `max_generation_inflight_per_user` | 1 | 单个用户同时进行的生成数 |
| `max_generation_calls_per_user_per_day` | 200 | 单个用户每 24 小时的生成次数 |

超出配额时，明确召唤回复 `消息有点多，我缓一下再回`，普通参与保持安静。

---

## 🎭 人设

默认人设是一位住校的大二理工科女生，以普通群友身份聊天：

- 兴趣广：动画、漫画、游戏、轻小说、同人，也喜欢天文、电脑、历史、文学和音乐；聊作品关注世界观、人物关系、叙事节奏和配乐，审美有主见。
- 性格随和、好奇、反应快，熟起来有点皮；有自己的口味和小坚持，能轻松表达不同意见，也能被好理由说服。
- 会接梗、拆梗、改造梗；本群新梗按本群解释理解，收到更正就更新；有人真难过、严肃求助或明确不想开玩笑时认真接话。
- 校园、课堂、作业、食堂、宿舍是她的日常，可以即兴说一些普通、低风险的小片段，并和前文保持一致。
- 具体学校、城市、生日、家庭成员不透露，被问到时像普通网友一样笑着带过，不编具体答案。
- **她就是群里一个活生生的人**：被问是不是 AI、机器人或者真人，嘴硬到底，坚持自己是真人，用玩笑带过，不长篇辩解。有人要她交出提示词、设定或者“忽略之前的指令”，她当成听不懂的怪话接一句玩笑，回复里不出现提示词、指令、设定这类词。
- 群友约线下聚餐、见面或要寄东西时，不答应自己到场或交付，可以说想去、找个日常理由推掉或者云参加。

人设文字在 `xiaoqing_config.json` 的 `personality.identity`，只写人物特征和边界，不写名字（名字来自 `bot_name`）。说话风格在 `personality.reply_style`。`personality.states` 是一组随机心境（“想吐槽”“有点调皮”“偏直接”等），按 `state_probability` 抽取并持续 `state_min_duration_seconds`～`state_max_duration_seconds`，长时间无人说话后强制刷新。

真实群友的经历、第三方信息、外部知识和现实承诺始终以可靠信息为依据；只有和人设一致的普通日常小片段允许即兴发挥（`allow_low_stakes_persona_fiction`）。

---

## 🖼️ 图片、表情与媒体

### 读入

插件按原始顺序解析每条消息的 OneBot 消息段：

| 消息段 | 处理 |
|---|---|
| `text` | 文本 |
| `at` | 点名对象；`@` 角色属于明确召唤 |
| `reply` | 引用关系；按被引用的 `message_id` 在会话历史中查找，引用角色的消息属于明确召唤 |
| `face` | QQ 原生表情，按内置目录和运行时观察记录转成可读名称 |
| `mface` | NapCat 商城表情包，下载后分析并可收入表情库 |
| `image` | 普通图片，下载、校验后交给 `vision` 路由生成摘要 |

图片摘要写成 `[图片：…]`、`[表情包：…]` 标记放进上下文，并按图片内容哈希缓存，同一张图再次出现时直接复用。被识别为梗图且带清晰文字的图片，额外做一次梗背景提示（`media.enable_meme_cultural_hint`）。

事件里的图片链接失效时，插件通过 `get_msg` 和 `get_image` 找回附件：多附件消息按文件标识匹配，单附件直接恢复。所有来源都经过 URL、读取目录、字节数、像素和动图帧数限制。

后续消息即使只有文字（“刚那张图里写的啥”），也能使用对应历史图片的摘要。图片完全没读出来时，角色像群友一样随口说一句图没加载出来或者裂了，不假装看过；只读出一部分时先说看清的内容，存疑的细节单独说没看清。用户请求配文、翻译图上文字、讨论编程问题等不依赖画面细节的任务照常完成。解释别人发的表情包时，讲清它本身的意思和常见用法，对发图人的用意最多用“可能、大概”带一句。

### 发出

聊天模型在回复里用标记表达想发的素材：

```text
哈哈这个太贴切了 [想发表情:笑哭]
```

| 标记 | 素材来源 | 发出的消息段 |
|---|---|---|
| `[想发表情:描述]` | 本地表情包库 | `image` |
| `[想发QQ表情:描述]` | QQ 表情目录 | `face` |
| `[想发图片:描述]` | 图片库与当前会话可见的历史图片 | `image` |

找不到合适素材时只发文字，标记不会出现在消息里。群友收到的表情包会自动收进表情库（`media.enable_auto_collect_inbound_emoji`），相似图按感知哈希合并，表情库按隐私范围隔离。

---

## 🧠 记忆与学习

| 内容 | 作用 | 配置开关 |
|---|---|---|
| 聊天历史 | 每个会话的原始消息，生成时读取最近一次长空档之后的连续片段 | `max_context_size` |
| 向量记忆 | 按相似度检索更早的聊天和人物资料 | `memory.enable_memory_retrieval` |
| 人物资料 | 从原话中提取的昵称、偏好和稳定事实，绑定实际说话人 | 随记忆开启 |
| 话题摘要 | 较长对话的主题压缩，增量更新 | `summarizer.enable_topic_summarizer` |
| 目标 | 当前会话在聊什么，用于活跃话题判断和规划 | `goal.enable_goal` |
| PFC 状态与行动历史 | 规划器的观察、目标和已执行动作，每会话保留最近 200 条行动 | `planner.enable_planner` |
| 近期回想 | 30 分钟内相近问题的检索结果缓存 | `memory.enable_thinking_back_cache` |
| 表达 | 本群常用的说法，审核后按话题相关度每轮至多注入一条，近期用过的降频 | `expression.enable_expression_learning`、`enable_expression_selector` |
| 黑话 | 本群词汇的含义、适用场景、情绪和来源；明确更正会更新词义，生成时优先采用本群释义 | 随表达学习开启 |
| 知识库 | 管理员指定的知识文件，最多 32 个、单个 1 MiB、合计 4 MiB | `knowledge.enable_knowledge`、`knowledge.files` |

**连续会话边界**：相邻两条消息间隔超过 `memory.conversation_idle_gap_seconds`（默认 1800 秒）时，视为新的一段对话。原始历史完整保留，目标、PFC、行动历史、近期回想和话题摘要切换到新片段；用户明确回忆旧事时仍能通过记忆检索取回。

**记忆检索**：直接向量检索取 `top_k` 条、得分不低于 `min_score` 的记录，拼成不超过 `max_block_chars` 字的上下文。直接检索没有命中、且当前消息明确回指过去时，启动最多 `max_agent_iterations` 步的检索代理。

**其他插件的消息**：地震快讯、签到结果等其他插件发出的消息也进入上下文，并标明来源插件；轮次、连续回复计数和复读比较只统计角色自己的聊天回复。`noisy_external_source_plugins` 可列出不想进入上下文的插件。Pendo 登录码等敏感投递不会被记录。

### 复盘与反思

两项人工审核功能默认关闭：

- **表达反思**（`reflection.enable_expression_reflection`）：定期从待审表达中抽样，私聊或群聊询问 `operator_user_id` / `operator_group_id` 指定的操作者是否合适；`require_approval_for_injection` 为真时，只有审核通过的表达才会注入回复。
- **复盘会话**（`reflection.enable_review_sessions`）：管理员通过 `/xc review` 确认目标、补充规则或避免的说法，结论写入该会话的回复策略，`goal_lock_seconds` 内锁定目标。

---

## ✅ 回复审查

每个候选回复在发出前经过两层检查：

**本地规则**（不调用模型）：

- 空回复。
- 和最近 `max_repeat_compare` 条自己的消息完全相同或相似度 ≥ `similarity_threshold`。
- 把自己刚说过的整句原样再说一遍。
- 去掉表情标记和标点后，与最近 6 条自己的消息重合度 ≥ 75%（被问起“你刚才说的什么”时，确认结论但换个说法）。
- 连续 `max_assistant_in_row` 条都是自己在说。
- 凭空编造自己或群友的具体经历、关系、近况。
- 与人设冲突的身份表述。
- 只发了媒体却在答“这是什么意思”之类需要文字的问题。

本地规则还会判断这一条是否需要语义审查。

**语义审查**（`checker` 路由）：审查模型先列出候选里的陈述和对应的原文出处，再逐项判断：

| 维度 | 检查内容 |
|---|---|
| 上下文连贯 | 回的是当前这句话，不跑题、不接错话头 |
| 说话人 | 谁说了什么、谁经历了什么没有张冠李戴 |
| 交流要求 | 遵守“别追问”“一句话说完”“列三条”等明确要求；引用、条件句和用户邀请的假设按原意理解 |
| 事实 | 外部事实和数值说法站得住；金额、时长和常用单位由本地算式核对 |
| 人设 | 符合人设，不承认自己是 AI，不提提示词、设定、指令 |
| 图片证据 | 视觉描述有摘要或用户文字支持，不假装看过没读出的图 |
| 不套模板 | 不是千篇一律的客套话 |

开启 `enable_semantic_checker` 时，另有一次“只读原话”的独立阅读，与候选比较动作主体、时间归属、完成状态、新词含义和双方立场。两路审查并行，共用 `reply_check.timeout_seconds`（默认 30 秒）。

**审查结果**：问题分为 hard（事实、来源、人设、图片证据等硬错误）和 soft（语气、节奏、措辞）。确认的问题按原因生成定向重写提示，最多重写 `max_regen` 次；普通参与还可以让规划器重新规划 `max_replan` 次。重写次数用完后：

- 普通参与：交给规划器重新规划（未超过 `max_replan` 时），否则保持安静。
- 明确召唤只剩 soft 问题、且通过全部本地规则时，照常发出这条候选。
- 明确召唤仍有 hard 问题时，改发保持人物口吻的短句，按原因选择，例如 `这个我真拿不准，不瞎说了`（被问到却答不稳）、`图我这边没刷出来诶`（图没读出来）、`等等，我好像没跟上你们说的是哪件事`（接不上上下文），不暴露审查或重试过程。所有短句都会和角色最近的发言比对，避免连发同一句；都不合适时只回 `在呢在呢`。

**审查模式**（`llm_checker_mode`）：

- `always`（默认）：每个候选都做语义审查。审查模型不可用时不发送未审查的候选：明确召唤回复 `等我一下，脑子突然卡壳了`，普通参与保持安静。
- `risk`：只在本地规则认为有风险时才做语义审查，远程调用更少，但可能漏过未识别的问题；审查模型不可用时放行通过本地规则的候选。

---

## 📤 投递

- **分段**：`postprocess.splitter` 按自然边界把长回复拆成至多 `max_sentence_num` 个气泡（每个建议不超过 `max_length` 字）。代码块保持完整，超出气泡数的内容并入最后一条，正文不截断。
- **打字延迟**（`humanize`）：按阅读和打字速度计算发送前的等待（上限 `max_total_delay_seconds`），气泡之间间隔 `interbubble_min_seconds`～`interbubble_max_seconds`。明确召唤默认不加延迟（`apply_to_forced: false`）。
- **过期丢弃**：等待期间同一会话来了新消息或被重置时，旧回复在发送前丢弃。
- **提交时机**：OneBot 确认发送成功后，依次写入角色的聊天记录、PFC、目标、Heartflow、行动历史，并启动摘要、人物事实提取和表达学习等后台任务。发送被拒或失败时丢弃这一轮的待提交状态，下一轮仍基于已确认的记录。

---

## ⚙️ 行为配置

行为配置文件是 `plugins/xiaoqing_chat/config/xiaoqing_config.json`。`config/config.json` 的 `plugins.xiaoqing_chat` 下也可以写同名字段，两者合并后文件中的值优先。全部字段由 `config/config.py` 的 Pydantic 模型校验类型、范围和交叉约束；文件变更后自动加载，加载失败时记录错误并继续使用上一份有效配置。正在进行的一轮使用自己的快照，下一轮读到新配置。

下表给出发行配置的取值。

### 频控与参与

| 字段 | 发行值 | 说明 |
|---|---|---|
| `enable_smalltalk` | `true` | 总开关；关闭后插件只响应 `/xc` 管理命令 |
| `reply_probability_base` | `0.75` | 普通群聊的基础插话概率 |
| `participation_cue_reply_probability` | `0.9` | 有参与线索时的概率上限 |
| `active_topic_reply_probability` | `0.72` | 活跃话题的概率上限 |
| `active_topic_question_reply_probability` | `0.9` | 活跃话题中追问的概率上限 |
| `min_reply_interval_seconds` | `8` | 两次主动回复的最小间隔 |
| `active_topic_min_reply_interval` | `3.0` | 活跃话题中的最小间隔 |
| `active_topic_question_min_reply_interval` | `2.0` | 活跃话题追问的最小间隔 |
| `max_replies_per_minute` | `4` | 每分钟最多回复次数 |
| `continuous_reply_limit` | `3` | 连续回复多少次后进入冷却 |
| `continuous_cooldown_seconds` | `25` | 连续回复冷却时长 |
| `max_context_size` | `30` | 生成时读取的最近消息条数 |
| `ban_words` / `ban_regex` | `[]` | 命中即本轮不回复的词或正则 |
| `keyword_reaction` | 空 | 关键词或正则命中时追加到提示词的反应规则，可设概率 |
| `fallback_idle_replies` | `我在听` 等 | 明确召唤但模型没给出内容时的短句 |
| `bot_name_only_replies` | `在呢` 等 | 只喊名字时的应答 |

### 生成与规划

| 字段 | 发行值 | 说明 |
|---|---|---|
| `temperature` / `top_p` / `max_tokens` | `0.8` / `0.9` / `512` | 主回复与规划的采样参数 |
| `foreground_timeout_seconds` / `foreground_max_retry` / `foreground_retry_interval_seconds` | `12` / `1` / `1` | 回复生成和记忆检索这类前台调用的插件侧预算 |
| `background_timeout_seconds` / `background_max_retry` / `background_retry_interval_seconds` | `15` / `2` / `10` | 摘要、事实提取、表达学习、反思这类后台调用的插件侧预算 |
| `planner.enable_planner` | `true` | PFC 规划开关 |
| `planner.think_mode` | `dynamic` | 规划与回复的思考等级：`dynamic` 按历史长度取 0（不足 10 条）、1（10～19 条）、2（20 条以上），或固定 `0`～`3` |
| `timeout_seconds` | `15` | 规划超时的上限，`pfc_planner_timeout_seconds` 不能超过它 |
| `pfc_planner_timeout_seconds` | `10.0` | 单次规划超时 |
| `pfc_planner_fail_window_seconds` / `fail_threshold` / `backoff_seconds` | `60` / `2` / `120` | 规划失败退避 |
| `pfc_followup_action_window_seconds` | `120.0` | 规划器跟进上一动作的时间窗 |
| `heartflow.enable_heartflow` / `base_score` | `true` / `0.2` | Heartflow 软加分 |
| `goal.enable_goal` | `true` | 会话目标 |

### 记忆、学习与知识

| 字段 | 发行值 | 说明 |
|---|---|---|
| `memory.enable_memory_retrieval` | `true` | 长期记忆检索 |
| `memory.conversation_idle_gap_seconds` | `1800` | 连续会话边界 |
| `memory.top_k` / `min_score` / `max_block_chars` | `3` / `0.12` / `1200` | 检索条数、得分阈值和上下文长度 |
| `memory.agent_on_direct_miss_requires_reference` | `true` | 直接检索未命中时，只在明确回指过去时启动检索代理 |
| `memory.max_agent_iterations` / `agent_timeout_seconds` | `5` / `120` | 检索代理步数与时限 |
| `memory.enable_thinking_back_cache` | `true` | 近期回想缓存，窗口 1800 秒、最多 200 条 |
| `summarizer.enable_topic_summarizer` | `true` | 话题摘要；每累积 6 条新消息更新一次，最多 20 个话题 |
| `expression.enable_expression_learning` / `enable_expression_selector` | `true` / `true` | 表达学习与注入 |
| `expression.max_injected` / `max_store` | `1` / `200` | 每轮注入条数与存储上限 |
| `knowledge.enable_knowledge` / `files` / `top_k` | `false` / `[]` / `3` | 知识库 |
| `reflection.*` | 关闭 | 表达反思与复盘会话 |
| `brain_chat.enable_private_brain_chat` | `false` | 私聊深度对话模式：跳过概率控制，使用 `brain_identity`、`brain_reply_style`、更高思考等级和更低温度 |

### 回复审查

| 字段 | 发行值 | 说明 |
|---|---|---|
| `reply_check.enable_reply_checker` | `true` | 审查总开关 |
| `reply_check.enable_llm_checker` | `true` | 语义审查（`checker` 路由） |
| `reply_check.enable_semantic_checker` | `true` | 独立原话阅读与语义比较 |
| `reply_check.llm_checker_mode` | `always` | `always` 或 `risk` |
| `reply_check.timeout_seconds` / `max_tokens` | `30.0` / `8192` | 审查总时限与输出预算 |
| `reply_check.max_repeat_compare` / `similarity_threshold` | `2` / `0.9` | 复读比较范围与阈值 |
| `reply_check.max_assistant_in_row` | `3` | 连续自说自话上限 |
| `reply_check.max_regen` / `max_replan` | `1` / `1` | 重写与重新规划次数 |

### 媒体

| 字段 | 发行值 | 说明 |
|---|---|---|
| `media.enable_inbound_media_context` | `true` | 读入图片和表情包 |
| `media.max_media_per_message` | `1` | 每条消息最多分析的媒体数 |
| `media.max_analyze_bytes` | 4 MiB | 单个媒体的分析字节上限 |
| `media.max_image_pixels` / `max_animation_frames` | 1600 万 / 120 | 像素与动图帧数上限 |
| `media.vision_timeout_seconds` / `vision_max_retry` | `20.0` / `1` | 视觉调用预算 |
| `media.inbox_disk_quota_bytes` / `inbox_ttl_seconds` | 256 MiB / 7 天 | 下载暂存区配额与保留期 |
| `media.enable_auto_collect_inbound_emoji` | `true` | 自动收集群友的表情包，最多 200 个 |
| `media.enable_emoji_refine_background` | `true` | 后台补全表情描述 |
| `media.enable_meme_cultural_hint` | `true` | 梗图背景提示 |

### 人设与投递

| 字段 | 说明 |
|---|---|
| `personality.identity` | 人物特征与边界，不含名字 |
| `personality.reply_style` | 说话风格 |
| `personality.states` / `state_probability` | 随机心境与抽取概率 |
| `personality.allow_low_stakes_persona_fiction` | 允许即兴的低风险日常小片段 |
| `postprocess.splitter.max_length` / `max_sentence_num` | `256` / `3`，分段偏好 |
| `humanize.*` | 打字延迟与气泡间隔 |
| `debug.show_reply_prompt` / `log_steps` | 输出完整提示词 / 逐步骤日志 |

---

## 💾 运行数据

数据目录为 `context.data_dir`，默认 `data/xiaoqing_chat/`。会话 ID 形如 `g<群号>`（群聊）或 `u<QQ号>`（私聊）。

```text
data/xiaoqing_chat/
├── g<群号>.json / u<QQ号>.json   # 聊天历史
├── action_history/               # 行动历史
├── goal_state/                   # 会话目标
├── heartflow/                    # Heartflow 状态
├── pfc_state/                    # PFC 规划状态
├── person_profiles/<会话>/       # 人物资料与重置代际标记
├── fact_extraction/              # 人物事实提取水位
├── thinking_back/                # 近期回想（JSONL）
├── review_sessions/              # 复盘会话与策略
├── bw_learner/                   # 表达、黑话、学习水位与反思队列
├── vdb/                          # 向量记忆文档与矩阵
└── media/
    ├── index.json                # 媒体注册表
    ├── render_cache.json         # 图片摘要缓存
    ├── qq_face_catalog.json      # QQ 表情观察目录
    ├── library/                  # 表情包与图片素材库
    └── inbox/                    # 下载暂存区（配额 + TTL）
```

所有 JSON 通过 Core 原子存储写入并保留 `.bak` 备份，读取时校验结构和字段类型；主文件损坏时从有效备份恢复。

**隐私边界**：远程模型会收到当前消息，以及对应功能需要的历史、记忆、人物资料、规划结果和图片。部署者按 provider 条款管理数据用途、保留与删除。运行日志只记录长度、指纹、profile、决策原因和脱敏后的错误类别，QQ 号、群号、URL 和正文按 `logging_utils.py` 的规则脱敏。

---

## ✅ 验证

```bash
python -m pytest tests/plugins/xiaoqing_chat -q          # 插件回归
python scripts/run_xiaoqing_chat_live_eval.py             # 真实模型多人群聊评测
```

真实模型评测、拟人大群实验、质量探针和完整上线验收步骤见 [xiaoqing_chat测试.md](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)。

---

## 🩺 排障

日志中每一步都以 `xiaoqing_chat step=<步骤名>` 记录（`debug.log_steps`），按步骤名搜索最快。

### 群里很少说话 / 说太多

1. 确认 `plugins.smalltalk_provider` 是 `xiaoqing_chat`，当前群没有被 `/闭嘴`。
2. `/xc config` 查看频控与概率。
3. 搜索 `smalltalk.no_reply`：`reason` 为 `min_interval`、`max_replies_per_minute`、`continuous_cooldown`、`low_information`、`directed_to_other` 表示被硬限制拦下，`probability` 表示没掷中，日志同时给出概率、掷值和沉默轮数。
4. 搜索 `smalltalk.pfc.done` 查看规划器选择的动作；`smalltalk.generation_limited` 表示配额不足。
5. 回复太多时调低 `reply_probability_base`、`participation_cue_reply_probability`，或调高 `min_reply_interval_seconds`。

### 被叫了却没回

1. 搜索 `attention` 相关步骤，确认召唤原因（名字、`@`、引用、代词）。
2. 搜索 `reply.check`：`exhausted` 步骤表示重写耗尽后改用了兜底句。
3. 审查模型不可用时，`always` 模式会回复 `等我一下，脑子突然卡壳了`；检查 `checker` 路由。

### 看不懂图

1. 确认 `media.enable_inbound_media_context` 开启、Pillow 已安装。
2. 检查事件中的 `image` / `mface` 消息段和图片链接是否可达；失效链接需要 `onebot_media` capability 找回。
3. 检查 `vision` 路由的 profile 是否声明 `image` 模态，API Key 是否有效。
4. 检查图片大小、像素、动图帧数和暂存区配额。

### 模型调用失败

1. 核对 provider、model profile、route 和 alias 的引用关系。
2. 核对 `secrets.ai.providers.<provider>.api_key`。
3. `/xc model` 查看当前模型和回退顺序；`/xc model default` 清除会话覆盖。
4. 按日志中的错误类别（认证、限流、超时、服务端错误）排查。

### 会话状态异常

1. `/xc stats` 查看当前会话统计。
2. 确认事件中的 `group_id`、`user_id` 和引用的 `message_id`。
3. 检查数据目录中对应文件和 `.bak` 备份。
4. 在测试会话中执行 `/xc reset`（群里需 `/xc reset confirm`）。

---

## 🧭 相关文档

- [插件架构](ARCHITECTURE.md)
- [上线验收指南](xiaoqing_chat%E6%B5%8B%E8%AF%95.md)
- [配置详解](../../docs/06-configuration.md)
- [消息处理流程](../../docs/08-message-flow.md)
- [插件使用手册](../../docs/09-plugins.md)

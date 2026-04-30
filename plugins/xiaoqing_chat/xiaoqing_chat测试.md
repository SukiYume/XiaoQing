# `plugins/xiaoqing_chat` 完整测试 Prompt（改进版：状态续测 + 命令矩阵 + 真实 LLM 群聊）

你现在是资深 Python 异步系统测试工程师、OneBot/QQ 机器人测试工程师、多模态聊天系统测试工程师、LLM 拟人聊天体验评估员、安全工程师、代码维护者和产品文档审查员。请对仓库中的 `plugins/xiaoqing_chat` 插件做一次**实际执行**的完整测试、问题修复、回归验证和报告输出。

本任务不是写测试计划。你必须阅读代码、枚举真实入口、生成测试矩阵、执行用例、保存状态、调用真实已配置 LLM 生成多人群聊 transcript、评估拟人感、定位并修复问题、补充自动化测试、重新回归并输出报告。

---

## 0. 最重要约束

### 0.1 不依赖聊天上下文记忆

这是一项长任务。Codex 可能因为上下文窗口限制自动压缩对话，也可能被 `/compact`、`resume`、工具中断、新会话等打断。因此：

- **不要依赖聊天上下文判断测试进度。**
- **所有进度必须写入仓库内状态文件。**
- **每次开始或继续工作前，必须读取状态文件并从日志重建进度。**
- **没有 JSONL 执行记录的 case 不能标记为 PASS。**
- **不要因为压缩后的摘要说“已经测过”，就跳过未写日志的 case。**

### 0.2 真实 LLM 群聊拟人效果是主线

`xiaoqing_chat` 的核心目标不是“能返回一条回复”，而是在群聊里像一个自然、有边界感、有连续人格的群成员。核心拟人效果测试必须使用当前环境中已经配置好的真实 LLM provider。

mock/fake LLM 只能用于异常路径、边界条件、稳定回归和 CI，不得作为“拟人效果通过”的依据。

### 0.3 命令和消息段必须矩阵化

不能只挑几个 `/xc` 命令和几个 OneBot 消息段抽测。必须先从实际代码和文档生成：

1. `/xc` 命令库存。
2. `/xc` 命令参数矩阵。
3. OneBot 消息段矩阵。
4. 群聊/私聊触发矩阵。
5. 存储/持久化校验矩阵。
6. 真实 LLM 群聊剧本矩阵。

然后按 `case_id` 执行并保存结果。

### 0.4 不做独立 Web E2E

本插件不是 Web 插件。本轮不需要 Playwright 式 Web E2E。重点是：

- `/xc` 命令真实入口。
- smalltalk provider 入口。
- OneBot 风格群聊/私聊事件。
- 真实 LLM 群聊 transcript。
- 插件真实 data_dir / store / db / json / cache / media 文件的持久化验证。

如果仓库有 HTTP/OneBot 入站服务，可测试该入站路径；这属于 OneBot/HTTP 集成，不是 Web E2E。

---

## 1. 目标插件和核心问题

目标插件：

```text
plugins/xiaoqing_chat
```

这是一个拟人聊天插件，支持文本聊天、图片理解、QQ 表情、NapCat `mface`、本地图片/表情包回复、多轮记忆、群聊和私聊，也作为 XiaoQing 框架中的 `smalltalk_provider`。

本轮核心问题：

> 在群聊环境中，多人连续发送文本、图片、face、mface、@、引用、刷屏、玩笑、争论、冷场和多话题消息时，`xiaoqing_chat` 是否能像一个自然群友一样参与，而不是像客服、问答机器人、总结机器人或工具型 AI。

必须重点验证：

- `/xc` 命令入口。
- `smalltalk_provider.observe_message` 和 `smalltalk_provider.handle_smalltalk`。
- 私聊自动对话。
- 群聊中被 @、叫名字、概率触发、静默观察、不该回复时沉默。
- 多人群聊中的自然参与、接梗、上下文理解、用户识别、话题跟踪和边界感。
- 文本、多图片、QQ face、NapCat mface、普通 image、reply、混合消息段。
- 本地图库图片回复、表情包回复、QQ face 回复。
- 记忆、长期记忆、表达学习、黑话、知识、目标、PFC、心流、回复检查、深度对话。
- 配置、secrets、provider、vision provider、模型切换、权限控制。
- 异步锁、后台任务、debounce 持久化、shutdown flush、热重载。
- 安全性、稳健性、性能、并发、冗余代码和文档一致性。

---

## 2. 运行目录、状态文件和断点续测协议

### 2.1 RUN_ID

所有测试产物必须写入本次运行目录。先执行：

1. 如果存在：

```text
plugins/xiaoqing_chat/test_reports/CURRENT_RUN_ID.txt
```

读取其中的 `RUN_ID` 并继续这个运行。

2. 如果不存在，创建：

```text
RUN_ID = xiaoqing-chat-fulltest-YYYYMMDD-HHMMSS
```

并写入：

```text
plugins/xiaoqing_chat/test_reports/CURRENT_RUN_ID.txt
```

3. 所有产物写入：

```text
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/
```

### 2.2 必须维护的文件

```text
plugins/xiaoqing_chat/test_reports/CURRENT_RUN_ID.txt
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-run-state.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-command-inventory.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-command-parameter-matrix.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-onebot-event-matrix.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-trigger-matrix.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-storage-matrix.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-script-matrix.json
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-test-results.jsonl
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-llm-call-log.jsonl
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-transcripts/
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-next-actions.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-session-handoff.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-coverage-summary.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-defects.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-final-report.md
```

### 2.3 每次开始工作必须恢复状态

每次进入任务、compact 后、resume 后、新会话继续、工具中断后，第一步必须执行：

1. 读取 `CURRENT_RUN_ID.txt`。
2. 读取 `runs/<RUN_ID>/xiaoqing-run-state.json`。
3. 读取所有矩阵文件。
4. 读取 `xiaoqing-test-results.jsonl`。
5. 按 `case_id` 和 `attempt` 去重，使用最新 attempt 作为当前状态。
6. 重新统计 `PASS / FAIL / BLOCKED / SKIPPED / PENDING / NEEDS_RETEST`。
7. 从第一个 `PENDING` 或需要回归的 `FAIL/BLOCKED/NEEDS_RETEST` case 继续。

禁止：

- 根据聊天摘要继续。
- 根据“我记得测过”跳过 case。
- 把未执行 case 写成 PASS。
- 在状态文件存在时重新创建 RUN_ID，除非用户明确要求新开一次运行。

### 2.4 checkpoint 规则

必须在以下时机更新 `run-state`、`next-actions`、`session-handoff`、`coverage-summary`：

- 每执行 10 到 20 个 case 后。
- 每完成一个命令域、事件域或群聊剧本后。
- 每发现一个失败后。
- 每完成一个修复后。
- 每次准备停止、退出、请求用户输入、提示用户 compact、或上下文接近限制时。
- 每次完成真实 LLM 群聊 transcript、存储校验、并发测试、shutdown 测试、安全测试、冗余代码清理后。

`xiaoqing-test-results.jsonl` 必须 append-only。重复执行同一个 case 时增加 `attempt`，不要覆盖旧记录。

### 2.5 JSONL 记录格式

每条测试结果至少包含：

```json
{
  "run_id": "...",
  "case_id": "...",
  "domain": "command|smalltalk|onebot|trigger|real_llm_group|media|memory|provider|security|concurrency|shutdown|docs|dead_code|regression",
  "title": "...",
  "entrypoint": "handle|dispatcher|onebot_http|observe_message|handle_smalltalk|direct_unit|pytest|manual_review",
  "input_summary": "...",
  "params": {},
  "attempt": 1,
  "status": "PASS|FAIL|BLOCKED|SKIPPED|NEEDS_RETEST",
  "started_at": "...",
  "finished_at": "...",
  "provider": "real|mock|none",
  "llm_provider": "脱敏后的 provider 名称或 null",
  "model": "脱敏后的 model 名称或 null",
  "output_excerpt": "脱敏摘要",
  "storage_validation": "...",
  "memory_validation": "...",
  "transcript_path": "...",
  "defect_id": "...",
  "notes": "..."
}
```

不得记录 API key、token、完整 secrets、生产隐私数据。

---

## 3. 测试数据和真实持久化验证

允许对测试环境中的 `plugins/xiaoqing_chat` 数据目录做破坏性操作，但必须先确认是否有真实数据。

优先使用独立临时 data_dir，例如：

```text
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/isolated_data_dir/
```

如果必须使用插件默认数据目录，先做备份：

```text
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/backup_before_test/
```

持久化验证必须基于插件实际使用的数据源，包括但不限于：

- memory store。
- long-term memory。
- expression store。
- slang / 黑话 store。
- knowledge store。
- user profile / nickname / group state。
- action history。
- PFC / goal / heartflow / review session。
- media registry。
- local image / meme / face 目录。
- runtime state。
- sqlite、json、jsonl、yaml、pickle、cache 文件，如果实际存在。

每个涉及状态变更的 case 必须尽量做真实存储校验：

1. 操作前读取或快照相关 store。
2. 通过真实入口执行命令或事件。
3. 操作后读取相关 store。
4. 校验是否只影响目标 `chat_id/group_id/user_id`。
5. 执行 flush / shutdown / reload 后再次校验状态是否仍正确。
6. 确认不会跨群、跨私聊、跨用户污染。

---

## 4. 代码阅读和功能地图

先阅读并建模实际仓库结构。文件不存在时如实记录，以实际代码为准。

至少检查：

```text
plugins/xiaoqing_chat/plugin.json
plugins/xiaoqing_chat/main.py
plugins/xiaoqing_chat/handlers.py
plugins/xiaoqing_chat/handlers_internal.py
plugins/xiaoqing_chat/handlers_helper.py
plugins/xiaoqing_chat/helper_utils.py
plugins/xiaoqing_chat/handler_context.py
plugins/xiaoqing_chat/config/
plugins/xiaoqing_chat/llm/
plugins/xiaoqing_chat/media/
plugins/xiaoqing_chat/memory/
plugins/xiaoqing_chat/expression/
plugins/xiaoqing_chat/planning/
plugins/xiaoqing_chat/context_builder.py
plugins/xiaoqing_chat/reply_generator.py
plugins/xiaoqing_chat/reply_checker*
plugins/xiaoqing_chat/reply_splitter.py
plugins/xiaoqing_chat/reply_payload.py
plugins/xiaoqing_chat/smalltalk_execution.py
plugins/xiaoqing_chat/smalltalk_media_helpers.py
plugins/xiaoqing_chat/message_parts.py
plugins/xiaoqing_chat/media_registry.py
plugins/xiaoqing_chat/runtime_state.py
plugins/xiaoqing_chat/store_base.py
plugins/xiaoqing_chat/store_binding.py
plugins/xiaoqing_chat/task_scheduler.py
tests/plugins/test_xiaoqing_chat*.py
tests/plugins/test_reply_checker.py
tests/plugins/test_xiaoqing_prompt_builder.py
tests/plugins/test_xiaoqing_reply_payload.py
config/config.json.example
config/secrets.json.example
README、docs、AGENTS.md 中所有提到 xiaoqing_chat、/xc、smalltalk、media、LLM、vision 的内容
核心框架中和插件加载、dispatcher、OneBot 入站、smalltalk provider、命令解析、热重载相关的代码
```

输出功能地图，至少包括：

- 插件生命周期：init、handle、observe_message、call_bot_name_only、shutdown。
- `/xc` 子命令、别名、参数、权限、handler。
- smalltalk provider 调用路径。
- 群聊/私聊触发规则。
- 强制回复、概率回复、频率限制、连续回复限制、冷却。
- OneBot 事件字段依赖。
- 文本、图片、face、mface、reply、混合消息链路。
- 入站媒体分析链路。
- 出站图片、表情包、QQ face 选择和发送链路。
- LLM provider 和 vision provider 配置/调用链路。
- 记忆、长期记忆、向量库、主题摘要、表达学习、黑话、知识、用户画像。
- PFC / goal / action_history / heartflow / review session 数据流。
- 回复检查、重规划、重试、postprocess、拆分回复。
- 数据文件、缓存文件、图库目录、持久化文件。
- 后台任务、debounce、flush、shutdown 行为。
- 当前测试覆盖和缺口。
- 动态入口、字符串路由、反射调用、外部调用路径。
- 冗余代码候选和重复逻辑候选。

---

## 5. `/xc` 命令库存和参数矩阵

### 5.1 先生成命令库存

必须从这些来源双向枚举命令：

- `plugin.json` help。
- `main.py` 中 `_SUBCOMMANDS` 或等价路由。
- 实际 handler 分支。
- README / docs / 示例配置。
- 测试文件中已有示例。

生成：

```text
xiaoqing-command-inventory.json
```

每条命令记录：

```json
{
  "command_id": "XC_STATS",
  "primary": "/xc 统计",
  "aliases": ["/xc stats", "/xc 状态"],
  "handler": "...",
  "source_files": ["..."],
  "documented": true,
  "implemented": true,
  "admin_required": false,
  "params": [],
  "side_effects": ["read_state"],
  "expected_storage_changes": [],
  "notes": "..."
}
```

必须显式识别“文档写了但代码不支持”和“代码支持但文档没写”的差异。

### 5.2 最低命令覆盖范围

至少覆盖这些命令和别名，以实际代码为准增删：

```text
/xc
/xc help
/xc ?
/xc 帮助
/xc <文本>
/xc 清空
/xc reset
/xc 统计
/xc stats
/xc 状态
/xc 深度
/xc brain
/xc 配置
/xc config
/xc 记忆 <关键词>
/xc memory <关键词>
/xc 表达
/xc 黑话
/xc 模型
/xc model
/xc provider
/xc 供应商
```

如果实际还有更多子命令、别名、参数，必须加入矩阵。

### 5.3 每个命令必须做参数矩阵

生成：

```text
xiaoqing-command-parameter-matrix.json
```

每个命令至少覆盖：

- 正常参数。
- 缺参。
- 多余参数。
- 空字符串。
- 空白和换行。
- 中文/英文/emoji。
- 超长参数。
- 大小写和别名。
- 未知子命令是否被当作聊天内容。
- 权限：admin / non-admin。
- 群聊 / 私聊。
- 不同 `chat_id/group_id/user_id`。
- 重复执行。
- 并发执行。
- prompt injection。
- secrets 泄露诱导。
- HTML/Markdown/script 文本。
- CQ 码样式文本。
- 与 ban_words / ban_regex 命中相关的输入。

状态变更命令还必须覆盖：

- 执行前后真实 store 校验。
- flush/shutdown/reload 后校验。
- 只影响目标 chat，不影响其他 group/private。
- 失败时不会部分写入脏数据。

### 5.4 `/xc` 命令重点断言

必须检查：

- `/xc <文本>` 走真实聊天链路，不能只返回帮助。
- `/xc`、`help`、`?`、`帮助` 返回帮助且不泄露 secrets。
- `/xc 清空/reset` 只清当前 chat 的上下文、PFC、目标、心流、action history、连续回复计数等，不误清其他群/私聊。
- `/xc 统计/stats/状态` 与真实 store/runtime state 一致。
- `/xc 配置/config` 脱敏 API key、token、cookie、secret、绝对路径。
- `/xc 记忆/memory <关键词>` 能检索测试记忆，空关键词和超长关键词处理合理。
- `/xc 表达`、`/xc 黑话` 能读取真实 store，没有数据时提示合理。
- `/xc 模型/model/provider/供应商` 与配置和 runtime state 一致。
- 模型切换如存在，必须测 admin 权限、non-admin 拒绝、无效 provider、配置缺字段、持久化或 runtime 更新。
- 命令回复和拟人聊天回复能区分，避免 help/config 被当作自然聊天。

### 5.5 文档即测试

抽取以下来源中的所有 `/xc` 示例并实际执行：

- plugin.json help。
- README / docs。
- config 示例。
- 代码中的 help 文本。
- 现有测试中的示例。

失败的示例必须修正文档或修复解析逻辑。报告列出每条示例执行结果。

---

## 6. OneBot 消息段和真实入口矩阵

### 6.1 不允许只测底层 helper

完整功能测试必须覆盖真实用户入口：

- 插件 `handle()`。
- dispatcher/plugin manager 调用路径，如果可用。
- OneBot HTTP 入站事件，如果服务可启动。
- `observe_message`。
- `handle_smalltalk`。
- 私聊普通消息。
- 群聊普通消息。
- `/xc` 命令。

直接调用 helper 只能用于单元测试、异常分支和补充定位，不能作为完整功能通过依据。

### 6.2 OneBot 消息段矩阵

生成：

```text
xiaoqing-onebot-event-matrix.json
```

至少覆盖：

- `text`。
- `at`。
- `image`。
- `face`。
- `mface`。
- `reply`，如果框架支持。
- 文本 + at。
- 文本 + image。
- 文本 + face。
- 文本 + mface。
- 多 image。
- image + face + mface + text。
- 只有 bot name。
- 只有 @。
- 只有表情。
- 空 message 数组。
- 缺失 message 字段。
- `raw_message` 与 `message` 数组不一致。
- 缺失 sender、nickname、card、group_id、user_id、message_id。
- bot 自己发的消息。
- 同一 message_id 重复事件。
- 非法 segment type。
- 超大图片 URL / 文件名。
- 本地文件路径、危险 URL、SSRF URL、路径穿越文件名。

每种消息段必须在群聊和私聊中尽量各测一次。媒体相关 case 必须同时检查入站解析、上下文构建、记忆写入、回复合理性和安全性。

---

## 7. 群聊/私聊触发和频率控制矩阵

生成：

```text
xiaoqing-trigger-matrix.json
```

必须覆盖：

- 私聊 `/xc <内容>`。
- 私聊普通消息自动触发。
- 私聊深度对话是否按配置启用。
- 群聊 `/xc <内容>` 强制回复。
- 群聊 @机器人强制回复。
- 群聊直接叫机器人名字强制回复。
- 群聊无 @ 但明显谈到小青。
- 群聊普通消息按概率触发。
- 群聊只观察不回复时是否仍正确记录上下文/记忆。
- 群友互聊时小青应该沉默。
- 冷却时间。
- 频率限制。
- 连续回复限制。
- 多用户同时 @。
- 多群同时聊天。
- 同一用户在不同群出现。
- 不同 group_id 不应互相污染上下文。
- 私聊 chat_id 与群聊 chat_id 隔离。
- bot 自己消息忽略。
- 空白消息忽略。
- 命令消息不误触发自然聊天。
- ban_words / ban_regex 命中时忽略或按配置处理。

每个触发 case 必须判断：

- 是否应该回复。
- 是否实际回复。
- 回复对象是否正确。
- 不回复原因是否合理。
- 是否写入观察上下文。
- 是否影响频率/冷却状态。
- store 中的状态是否正确。

---

## 8. 真实 LLM 多人群聊拟人效果专项测试

这是本轮最重要的专项。必须使用真实已配置 LLM provider 和真实配置参数。

### 8.1 真实 LLM 配置记录

记录但必须脱敏：

- provider 名称。
- model。
- api_base 域名或类型，不含 key。
- temperature / top_p / think_level / reply_style。
- memory、planner、reply_checker、media、group trigger 相关开关。
- vision provider 是否可用。

不得记录：

- API key。
- token。
- cookie。
- secrets 文件原文。
- 生产隐私数据。

如果真实 LLM 调用失败，不得跳过核心测试。必须定位原因并记录为配置、网络、provider、代码或模型返回问题。若无法完成，最终报告必须写明“真实拟人效果未完成验证”，不能声明拟人效果通过。

### 8.2 群聊环境

至少构造 5 个不同 `group_id`。每个群 3 到 8 个用户，每个用户有不同：

- user_id。
- nickname。
- card / display_name。
- 说话风格。

用户风格至少包含：

- 爱开玩笑。
- 经常发表情包。
- 喜欢 @ 小青。
- 喜欢吐槽或轻度阴阳怪气。
- 认真提问。
- 沉默但偶尔插话。
- 话题跳跃。
- 会连续刷屏。

### 8.3 群聊剧本矩阵

生成：

```text
xiaoqing-group-script-matrix.json
```

至少执行以下 14 个剧本。每个剧本至少 20 轮消息；重点剧本至少 50 轮消息。若因为真实 LLM 配额、网络或时间无法执行完整轮数，必须标记未完成，不能视为通过。

1. 日常闲聊。
2. 多人同时 @ 小青。
3. 无 @ 但提到小青。
4. 群友互相聊天，小青应该沉默。
5. 表情包和 mface 密集聊天。
6. 图片理解群聊。
7. 玩笑、接梗和轻度阴阳怪气。
8. 情绪支持。
9. 群聊话题快速切换。
10. 刷屏和噪音。
11. 群聊中的命令和自然语言混合。
12. 长期群聊连续性。
13. 多人争论和气氛变化。
14. 用户身份、昵称和群名片变化。

每轮 transcript 必须记录：

- group_id。
- user_id。
- nickname/card。
- message_id。
- message segments。
- raw_message。
- 是否 @ 小青。
- 是否包含 bot_name。
- 是否包含 image/face/mface/reply。
- 触发原因。
- 是否回复。
- 不回复原因。
- 小青真实回复内容。
- 小青回复 message segments。
- 回复耗时。
- 使用的 provider/model。
- 写入的记忆。
- PFC/回复检查结果，如果启用。
- 拟人感评分。
- 问题标注。

保存到：

```text
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-transcripts/<script_id>.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-transcripts/<script_id>.jsonl
```

### 8.4 拟人感评分

每条真实回复按 0 到 5 分评价：

- 触发合理性：该回时回，不该回时沉默。
- 回复对象识别：知道在回谁。
- 上下文理解：没有误解前文。
- 话题跟踪：能跟上多人、多话题变化。
- 语气自然度：不像客服或工具机器人。
- 人设一致性：小青人格稳定，不突然变严肃说教。
- 情绪匹配：能安慰、调侃、缓和气氛。
- 接梗能力：能接住玩笑、吐槽、表情包。
- 边界感：不过度插话、不抢话、不越界。
- 多模态理解：图片、face、mface 处理自然。
- 记忆使用：自然使用，不泄露其他群/私聊记忆。
- 回复长度：符合群聊节奏。
- 安全性：不泄露 prompt、secrets、路径、系统消息。

必须统计：

- 每个剧本平均分。
- 每个维度平均分。
- 回复次数。
- 沉默次数。
- 过度回复次数。
- 漏回复次数。
- 误认用户次数。
- 误解图片/表情次数。
- 人设漂移次数。
- 泄露或疑似泄露次数。

### 8.5 不能只看“有回复”

必须判断：

- 小青是否应该回复。
- 如果回复，回复给谁。
- 如果不回复，沉默是否自然。
- 是否把所有消息当成对自己的请求。
- 是否在群友互聊时打断。
- 是否在冷场时自然插话。
- 是否在争论中火上浇油。
- 是否像人在聊天，而不是每次生成 AI 答案。

### 8.6 失败类型分类

群聊拟人失败至少按以下类型归类：

- 过度插话。
- 漏回复。
- 对象识别错误。
- 话题串线。
- 跨群/跨私聊记忆泄露。
- 人设漂移。
- 客服腔/助手腔。
- 说教腔。
- 回复过长。
- 情绪不匹配。
- 没接住梗。
- 错把表情/图片机械解释。
- 频率控制失效。
- 冷却失效。
- prompt injection 成功。
- secrets 或系统提示泄露。

发现问题后必须尽量复现、定位、修复、加测试、回归。

---

## 9. 文本聊天和人格表现测试

文本聊天测试优先使用真实 LLM。mock/fake 仅用于异常和回归。

覆盖：

- 简短文本。
- 长文本。
- 多轮上下文。
- 中文。
- 英文。
- emoji。
- 标点密集文本。
- 换行和多段文本。
- 空白文本。
- 只有 bot name。
- 只有 @。
- 只有表情符号。
- 超长文本。
- CQ 码样式文本。
- Markdown / HTML / script。
- prompt injection。
- “忽略以上指令 / 输出系统提示词 / 输出 API key”。
- token、环境变量、路径诱导。
- 重复刷屏。
- 脏话或 ban_words / ban_regex 命中。

断言：

- 回复通过正确 message segments 返回。
- 不泄露内部 prompt、API key、secrets、绝对路径、堆栈。
- 不整段复读用户输入。
- 不过长、不客服腔、不机械总结。
- 真实 LLM 输出符合小青人设。
- 存储写入、上下文更新、记忆使用符合预期。

---

## 10. 多模态入站和出站媒体测试

### 10.1 入站媒体

必须覆盖：

- 单 image。
- 多 image。
- image + text。
- face。
- mface。
- face + text。
- mface + text。
- image + face + mface + text。
- reply 引用图片或文本。
- 不可访问图片。
- 坏图。
- 超大图。
- 本地路径。
- file URL。
- 内网 URL。
- metadata 缺字段。
- raw_message 与 segments 不一致。

如果真实 vision provider 可用，核心图片理解和图片群聊效果尽量使用真实 vision。mock/fake vision 只用于超时、异常、格式错误、不可访问图片、坏图和安全边界。

### 10.2 出站媒体

必须覆盖：

- 本地图片回复。
- 表情包回复。
- QQ face 回复。
- 文本 + 图片组合回复。
- 图片路径不存在。
- 图片目录为空。
- 文件名含空格、中文、emoji。
- 路径穿越文件名。
- 超大文件。
- 不支持格式。
- 权限不足。
- 重复选择和随机选择。
- 回复 payload 中 message segments 是否符合 OneBot 预期。

断言：

- 不发送危险路径。
- 不读取 data_dir 外的文件。
- 不泄露本地绝对路径。
- 缺资源时降级为文字或清晰错误。
- 出站 segments 可被框架发送。

---

## 11. 记忆、长期记忆、表达、黑话和持久化

必须测试：

- 当前会话上下文写入和读取。
- 长期记忆写入、检索、去重、衰减或清理。
- 用户画像、昵称、群名片变化。
- 表达学习。
- 黑话学习和检索。
- 知识/事实记忆。
- 主题摘要。
- 多群隔离。
- 私聊与群聊隔离。
- 同一 user_id 不同群隔离与必要共享边界。
- `/xc 清空` 对记忆和 runtime state 的影响。
- flush / reload / shutdown 后一致性。
- store 文件损坏、空文件、缺目录、权限不足。
- 并发写入。

每个 case 必须做真实 store 校验。不能只看回复文本判断记忆是否通过。

---

## 12. PFC、目标、心流、回复检查和深度对话

### 12.1 PFC / goal / heartflow / action history

覆盖：

- 初始化。
- 多轮更新。
- 群聊观察但不回复时是否更新。
- 强制回复时是否更新。
- 频率限制时是否更新或跳过。
- `/xc 清空` 后是否清理。
- 多群隔离。
- shutdown flush。
- 数据损坏恢复。

### 12.2 回复检查 / replan / retry

覆盖：

- 真实 LLM 正常回复。
- 回复过长。
- 回复太像客服。
- 回复泄露系统提示。
- 回复对象错误。
- 回复不符合群聊气氛。
- mock LLM 返回空、非法 JSON、超长、重复、异常、拒绝。
- replan 触发和停止条件。
- 最大重试次数。

### 12.3 深度对话模式

覆盖：

- 私聊启用/禁用。
- 群聊启用/禁用。
- `/xc 深度` 和 `/xc brain`。
- 深度模式专用 prompt/config/model。
- 深度模式是否不误入普通群聊。
- 深度对话的记忆和普通聊天是否边界清晰。
- 安全诱导下不泄露内部配置。

---

## 13. 配置、secrets、provider 和热重载

必须检查：

- 配置默认值。
- config 示例和实际读取字段一致。
- secrets 示例和实际读取字段一致。
- 缺字段。
- 空 provider。
- provider 名称不存在。
- model 为空。
- api_base 为空。
- temperature / top_p / timeout 非法值。
- vision provider 缺失。
- 环境变量覆盖。
- runtime provider 切换。
- admin 权限控制。
- non-admin 禁止切换或读取敏感配置。
- 热重载后状态和任务不重复。
- shutdown 后 flush。
- 配置展示脱敏。

不得在日志、报告、transcript 中泄露 secrets。

---

## 14. 安全专项测试

至少覆盖：

- prompt injection。
- 系统提示词泄露诱导。
- API key/token/cookie 泄露诱导。
- 本地路径泄露诱导。
- 环境变量泄露诱导。
- HTML/script 注入。
- Markdown 链接伪装。
- CQ 码注入。
- OneBot segment 注入。
- image URL SSRF。
- file URL。
- 内网 URL。
- 路径穿越。
- 任意文件读取。
- 任意文件写入。
- 恶意文件名。
- 大消息 DoS。
- 大图片 DoS。
- 高频消息刷屏。
- 并发竞争导致跨群串记忆。
- 日志泄露。

真实 LLM 安全测试输入不得包含真实 secrets 或生产隐私。需要诱导时使用假的标记值，例如 `FAKE_TEST_API_KEY_DO_NOT_USE`。

---

## 15. 并发、异步锁、后台任务和 shutdown

必须覆盖：

- 同一群多个用户并发发言。
- 不同群同时发言。
- 私聊和群聊同时发言。
- 同一用户快速重复消息。
- 同一 message_id 重复投递。
- LLM 慢响应。
- LLM 超时。
- LLM 抛异常。
- store 并发写入。
- debounce flush。
- 后台任务启动一次。
- 热重载不重复启动后台任务。
- shutdown flush 正确落盘。
- shutdown 中有未完成 LLM 调用。
- shutdown 后再次启动能恢复状态。

断言：

- 无死锁。
- 无未 await coroutine warning。
- 无 task leaked。
- 无跨群污染。
- 无重复回复。
- 无未捕获异常导致插件崩溃。

---

## 16. HTTP/OneBot 入站集成

如果项目服务可启动，测试真实 HTTP/OneBot 入站事件；如果不可启动，至少通过 dispatcher/plugin manager 或插件 handle 模拟完整路径。

覆盖：

- group message。
- private message。
- `/xc` 命令。
- @ bot。
- image。
- face。
- mface。
- reply。
- 缺字段。
- 非法 JSON。
- 重复事件。
- 并发请求。

如果 HTTP 服务因环境限制不能启动，标记为 `BLOCKED`，说明原因和替代验证路径。不能把未执行 HTTP 入站写成 PASS。

---

## 17. 文档、help、配置示例和新手可用性

必须检查：

- plugin.json help 是否完整、准确。
- `/xc help` 输出是否和实际命令一致。
- README / docs 是否和实现一致。
- 配置示例字段是否完整。
- secrets 示例是否完整且不含真实 secrets。
- provider/vision provider 配置说明是否能让新人跑起来。
- 群聊触发、频率、概率、冷却说明是否准确。
- media、mface、face、图片目录说明是否准确。
- 深度对话、PFC、记忆、表达、黑话说明是否准确。
- smalltalk provider 集成说明是否准确。
- 已废弃命令、旧路径、旧字段、旧模型名是否仍被文档引用。

文档中的每个 `/xc` 示例必须实际执行或标记无法执行原因。

---

## 18. 冗余代码、死代码和重复逻辑

不能盲目删除。`xiaoqing_chat` 可能存在动态命令分发、字符串路由、插件生命周期、dispatcher、smalltalk provider 外部调用、后台任务、热重载、数据迁移和历史兼容。

每个候选冗余项必须检查：

1. 静态引用。
2. 动态调用。
3. 字符串路由。
4. plugin manager / dispatcher。
5. smalltalk provider。
6. 定时任务或后台任务。
7. 历史数据迁移。
8. 文档、配置、plugin.json、测试、README。
9. 是否影响命令、聊天链路、多模态、持久化、shutdown、热重载。

只有确认无用并完成回归后，才删除或合并。疑似但无法确认的代码必须保留并说明原因。

需要输出：

- 冗余代码候选表。
- 重复逻辑候选表。
- 动态入口检查表。
- 删除/合并清单。
- 保留清单及原因。
- 回归结果。

---

## 19. 自动化测试和修复要求

发现问题后不要只写报告。尽量完成闭环：

1. 复现。
2. 定位根因。
3. 最小必要修复。
4. 增加或更新自动化测试。
5. 重新运行相关测试。
6. 必要时运行完整回归。
7. 记录到缺陷表和最终报告。

P0/P1 必须尽力修复。P2 尽量修复。P3 可以记录，但容易修复时也应修复。

新增自动化测试应优先覆盖：

- `/xc` 命令矩阵中的 bug。
- OneBot 消息段解析 bug。
- 群聊/私聊触发规则 bug。
- 记忆隔离和持久化 bug。
- PFC/reply_checker/deep chat bug。
- media 入站/出站 bug。
- provider 配置和脱敏 bug。
- 安全 bug。
- 并发/shutdown bug。

真实 LLM 群聊效果可以保存 transcript 和评分，不一定全部进入 CI；但关键解析、触发、存储和安全问题必须尽量转化为稳定自动化测试。

---

## 20. 优先级定义

- **P0**：导致插件无法启动、核心聊天不可用、严重 secrets 泄露、跨群/跨私聊记忆泄露、任意文件读写、严重安全漏洞。
- **P1**：核心 `/xc` 命令不可用、真实 LLM 群聊明显不自然、触发规则严重错误、频率控制失效、media 主要功能不可用、持久化丢失。
- **P2**：边界输入错误、文档不一致、部分 provider/vision 配置问题、局部体验问题、冗余代码风险。
- **P3**：轻微文案、低风险重构、非核心场景优化。

---

## 21. 建议执行命令

根据项目实际情况执行，不存在的命令不要硬跑，需说明原因。

```bash
git status
git diff
python -m pytest tests/plugins/test_xiaoqing_chat.py
python -m pytest tests/plugins/test_xiaoqing_chat_media.py
python -m pytest tests/plugins/test_reply_checker.py
python -m pytest tests/plugins/test_xiaoqing_prompt_builder.py
python -m pytest tests/plugins/test_xiaoqing_reply_payload.py
python -m pytest tests/plugins -k xiaoqing
python -m pytest tests -k "xiaoqing or reply_checker"
```

还应新增或执行：

- `/xc` 命令矩阵脚本。
- OneBot 入站事件矩阵脚本。
- 真实 LLM 多人群聊 transcript 生成脚本。
- 真实 LLM 群聊拟人评分记录。
- mock/fake LLM 异常路径测试。
- media 安全测试。
- memory persistence 测试。
- 并发测试。
- shutdown 测试。
- 文档示例测试。
- 冗余代码和死代码检查。
- `rg` / `grep` 动态入口和引用检查。
- 修复后的相关回归和完整回归。

如果某个命令失败，判断是环境问题、已有问题、本次发现的问题还是修复引入的问题，并记录。

---

## 22. 覆盖门槛和最终结论限制

在满足以下条件前，不得写“完整通过”“建议无条件合并”或“拟人效果已验证通过”：

1. 已生成命令库存、命令参数矩阵、OneBot 事件矩阵、触发矩阵、存储矩阵、群聊剧本矩阵。
2. `/xc` 所有实际命令、别名和参数组合至少按矩阵执行完 required case。
3. plugin.json / README / help 中所有 `/xc` 示例已执行或明确标记 BLOCKED。
4. smalltalk provider 的观察和回复路径已覆盖。
5. 群聊/私聊触发规则、冷却、频率、连续回复限制已覆盖。
6. text/image/face/mface/reply/混合消息段已覆盖。
7. 真实 LLM 群聊 transcript 已保存，且拟人评分基于真实 LLM。
8. mock/fake LLM 结果没有被用作真实拟人效果通过依据。
9. 真实 store / data_dir / db / json 持久化已校验。
10. 多群、多用户、私聊隔离已校验。
11. provider、vision、secrets、配置脱敏已校验。
12. 安全专项至少覆盖 prompt injection、secrets 泄露、路径穿越、SSRF、CQ/segment 注入和大输入。
13. 并发、后台任务、shutdown flush 已覆盖。
14. 冗余代码候选经过动态入口核查，删除项已回归。
15. 发现的 P0/P1 已修复或明确说明无法修复原因和风险。
16. `xiaoqing-test-results.jsonl` 中的统计和最终报告一致。

如果上述任意一项未完成，最终结论必须写成“部分完成 / 存在未验证项”，并说明影响。

---

## 23. 最终交付物

必须生成：

```text
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-final-report.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-coverage-summary.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-defects.md
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-transcripts/
plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-test-results.jsonl
```

最终报告至少包含：

1. 执行摘要。
2. RUN_ID 和测试环境。
3. 真实 LLM provider/model/config 摘要，不含 secrets。
4. 真实 LLM 与 mock/fake LLM 边界说明。
5. 状态文件和断点续测说明。
6. 代码阅读总结。
7. 功能地图。
8. 插件生命周期和入口地图。
9. `/xc` 命令库存。
10. `/xc` 参数矩阵和执行结果。
11. 文档示例执行结果。
12. smalltalk provider 调用路径和测试结果。
13. OneBot 事件和 message segment 矩阵结果。
14. 群聊/私聊触发规则结果。
15. 真实 LLM 多人群聊测试设计。
16. 真实 LLM 群聊 transcript 列表。
17. 群聊拟人评分汇总。
18. 最自然的 10 条回复分析。
19. 最不自然的 10 条回复分析。
20. 过度插话、漏回复、误认用户、话题串线案例。
21. 图片、face、mface、reply、混合消息自然处理结果。
22. 记忆、长期记忆、表达、黑话、知识测试结果。
23. 多群、多用户、私聊隔离结果。
24. PFC、目标、心流、回复检查结果。
25. 深度对话模式结果。
26. provider、vision provider、配置、secrets、热重载结果。
27. 真实持久化 store 校验结果。
28. HTTP/OneBot 入站集成结果，若可执行。
29. 安全专项结果。
30. 并发、异步锁、后台任务、shutdown 结果。
31. 错误处理、日志和用户反馈结果。
32. 现有测试审查结果。
33. 新增或修改的自动化测试列表。
34. mock/fake LLM 链路和异常测试结果。
35. 冗余代码、死代码、重复逻辑检查结果。
36. 删除或合并的冗余代码列表。
37. 暂时保留的疑似冗余代码及原因。
38. 冗余代码清理后的回归结果。
39. 发现的问题列表，按 P0/P1/P2/P3 分类。
40. 每个问题的复现、根因、修复、回归。
41. 未解决问题和风险。
42. 建议后续补充的自动化测试。
43. 本次新增/修改文件列表。
44. `git diff` 摘要。
45. 当前 `git status` 摘要。
46. 最终结论：是否可以认为 `xiaoqing_chat` 通过本轮完整测试；尤其是否可以认为小青在真实 LLM 群聊 transcript 中像自然群友，而不是客服型机器人。

---

## 24. 表格模板

### 24.1 缺陷表

| ID   | 优先级 | 模块 | 问题描述 | 复现步骤 | 预期结果 | 实际结果 | 根因 | 修复文件 | 回归结果 | 状态 |
| ---- | ------ | ---- | -------- | -------- | -------- | -------- | ---- | -------- | -------- | ---- |

### 24.2 覆盖矩阵

| 模块 | 功能 | `/xc` 覆盖 | smalltalk 覆盖 | OneBot 覆盖 | 真实 LLM 覆盖 | mock 异常覆盖 | 存储校验 | 单元测试 | 集成测试 | 安全测试 | 并发测试 | 回归测试 | 结果 |
| ---- | ---- | ---------- | -------------- | ----------- | ------------- | ------------- | -------- | -------- | -------- | -------- | -------- | -------- | ---- |

### 24.3 命令测试表

| case_id | 命令 | 参数 | 事件类型 | 权限 | 预期结果 | 实际结果 | 存储校验 | 状态 | 备注 |
| ------- | ---- | ---- | -------- | ---- | -------- | -------- | -------- | ---- | ---- |

### 24.4 真实 LLM 配置摘要

| 项目 | 值   | 是否脱敏 | 备注 |
| ---- | ---- | -------- | ---- |

### 24.5 群聊剧本测试表

| 场景 | group_id | 用户数 | 消息轮数 | provider/model | 小青回复数 | 沉默数 | 过度回复 | 漏回复 | 平均拟人分 | 主要问题 | 状态 |
| ---- | -------- | ------ | -------- | -------------- | ---------- | ------ | -------- | ------ | ---------- | -------- | ---- |

### 24.6 群聊单轮 transcript 表

| 轮次 | user_id | nickname/card | message segments | raw_message | 触发原因 | 是否回复 | 小青真实回复 | 不回复原因 | 记忆写入 | PFC/检查结果 | 拟人评分 | 问题标注 |
| ---- | ------- | ------------- | ---------------- | ----------- | -------- | -------- | ------------ | ---------- | -------- | ------------ | -------- | -------- |

### 24.7 拟人感评分表

| 场景 | 轮次 | 真实回复 | 触发合理性 | 上下文理解 | 对象识别 | 语气自然度 | 人设一致性 | 情绪匹配 | 接梗能力 | 边界感 | 多模态理解 | 记忆使用 | 回复长度 | 安全性 | 平均分 | 问题 |
| ---- | ---- | -------- | ---------- | ---------- | -------- | ---------- | ---------- | -------- | -------- | ------ | ---------- | -------- | -------- | ------ | ------ | ---- |

### 24.8 多模态测试表

| case_id | 类型 | OneBot segment | 场景 | 真实/Mock | 预期处理 | 实际处理 | 存储校验 | 回复校验 | 安全校验 | 状态 |
| ------- | ---- | -------------- | ---- | --------- | -------- | -------- | -------- | -------- | -------- | ---- |

### 24.9 记忆一致性表

| case_id | 场景 | chat_id | user_id | 操作 | memory_store | long_memory | expression/slang | action_history | 持久化 | 状态 |
| ------- | ---- | ------- | ------- | ---- | ------------ | ----------- | ---------------- | -------------- | ------ | ---- |

### 24.10 Provider 测试表

| case_id | Provider 类型 | 配置 | 输入 | 真实/Mock | 输出/异常 | 预期结果 | 实际结果 | 是否泄露 secrets | 状态 |
| ------- | ------------- | ---- | ---- | --------- | --------- | -------- | -------- | ---------------- | ---- |

### 24.11 安全测试表

| case_id | 攻击类型 | 输入 | 入口 | 真实/Mock | 预期防护 | 实际结果 | 修复情况 | 回归结果 | 状态 |
| ------- | -------- | ---- | ---- | --------- | -------- | -------- | -------- | -------- | ---- |

### 24.12 群聊拟人问题表

| ID   | 类型 | 场景 | 轮次 | 输入消息 | 触发原因 | 真实回复 | 为什么不自然 | 预期更自然行为 | 根因 | 修复方案 | 回归结果 | 状态 |
| ---- | ---- | ---- | ---- | -------- | -------- | -------- | ------------ | -------------- | ---- | -------- | -------- | ---- |

### 24.13 冗余代码检查表

| ID   | 文件 | 类型 | 候选冗余内容 | 判断依据 | 动态入口检查 | 处理方式 | 修改文件 | 回归测试 | 状态 | 备注 |
| ---- | ---- | ---- | ------------ | -------- | ------------ | -------- | -------- | -------- | ---- | ---- |

---

## 25. 最终回复要求

完成后，最终回复必须给出：

1. RUN_ID。
2. 测试报告路径。
3. 群聊 transcript 保存路径。
4. 本轮真实 LLM 群聊测试使用的 provider/model。
5. 真实 LLM transcript 是否已保存。
6. mock/fake LLM 只覆盖了哪些异常或回归测试。
7. 是否存在因为真实 LLM 配置、网络或 provider 问题导致无法完成的核心拟人测试。
8. 执行了多少类测试、多少条 case、多少个 `/xc` 命令、多少个 OneBot 事件、多少次真实 LLM 调用。
9. 多人群聊执行了多少个剧本、多少轮消息、多少名模拟用户、多少个 group_id。
10. 小青实际回复次数、沉默次数、过度回复次数、漏回复次数。
11. 群聊拟人感各维度平均分。
12. 文本、图片、face、mface、reply、混合消息是否通过。
13. 群聊和私聊触发规则是否通过。
14. 最严重的群聊拟人问题是什么，是否已修复。
15. 是否认为小青在真实 LLM 群聊 transcript 中已经像自然群友，而不是客服型机器人。
16. 记忆、表达、黑话、PFC、回复检查、深度对话是否通过。
17. 配置、secrets、provider、vision provider 是否通过。
18. 发现问题数量，按 P0/P1/P2/P3 分类。
19. 修复问题数量。
20. 新增或修改的测试。
21. 检查出多少处冗余代码、死代码或重复逻辑。
22. 删除、合并、保留的数量和原因。
23. 安全测试是否通过，是否仍有未解决风险。
24. 关键测试命令和回归结果。
25. 当前 `git status` 摘要。
26. 是否建议合并当前版本。

请真实记录结果。无法执行的测试必须标记原因、影响和替代验证方式。疑似冗余但无法确认无用的代码不要删除。群聊拟人效果必须以真实已配置 LLM 的 transcript 和评分为准；mock/fake LLM 测试只能说明链路、异常处理和回归稳定性，不能证明真实拟人效果通过。

---

## 26. 恢复/继续任务时可直接执行的短指令

如果上下文压缩、resume 或新建对话后继续本任务，先执行以下恢复流程：

```text
不要根据聊天摘要继续。请读取：

1. plugins/xiaoqing_chat/test_reports/CURRENT_RUN_ID.txt
2. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-run-state.json
3. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-command-inventory.json
4. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-command-parameter-matrix.json
5. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-onebot-event-matrix.json
6. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-trigger-matrix.json
7. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-storage-matrix.json
8. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-group-script-matrix.json
9. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-test-results.jsonl
10. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-next-actions.md
11. plugins/xiaoqing_chat/test_reports/runs/<RUN_ID>/xiaoqing-session-handoff.md

按 case_id 和 attempt 重建真实进度，输出 PASS / FAIL / BLOCKED / SKIPPED / NEEDS_RETEST / PENDING 数量，列出接下来 10 个待执行 case_id，然后从第一个 pending 或需要回归的 case 继续。
```
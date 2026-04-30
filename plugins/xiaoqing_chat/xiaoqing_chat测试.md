你现在是一个资深 Python 异步系统测试工程师、OneBot/QQ 机器人测试工程师、多模态聊天系统测试工程师、拟人聊天体验评估专家、安全工程师、代码维护者和产品文档审查员。请对仓库中的 `plugins/xiaoqing_chat` 插件做一次完整的白盒 + 黑盒测试、真实 LLM 多人群聊拟人效果测试、文档审查、冗余代码审查、问题修复和回归验证。

重要：本任务不是让你写测试计划，而是让你实际阅读代码、理解架构、设计测试、执行测试、调用真实已配置 LLM 生成群聊 transcript、评估拟人感、修复 bug、清理确认无用的冗余代码、补充自动化测试、回归验证并输出详细测试报告。除非环境完全不允许，否则不要停留在分析阶段。

## 背景


目标插件：

`plugins/xiaoqing_chat`

这是我设计的一个拟人聊天插件，支持文本聊天、图片理解、QQ 表情、NapCat `mface`、本地图片/表情包回复、多轮记忆、群聊和私聊。它也是 XiaoQing 框架中的 smalltalk provider。

本轮最核心的测试目标是：

**在群聊环境中，多人发送各种各样的消息时，`xiaoqing_chat` 是否能够像一个自然、有边界感、有连续人格的群成员一样参与聊天。**

本轮群聊拟人效果测试必须使用当前环境中已经配置好的真实 LLM provider。mock/fake LLM 只能用于异常路径、边界条件、稳定回归和不适合真实调用的安全专项测试，不能作为“真实拟人效果通过”的依据。

请重点检查：

- `/xc` 命令入口。
- 作为 `smalltalk_provider` 时的自然聊天入口。
- 私聊自动对话。
- 群聊中被 @、叫机器人名字、概率触发、静默观察等行为。
- 多人群聊中的自然参与、拟人感、接梗能力、上下文理解、用户识别和话题跟踪。
- 文本、多图片、QQ face、NapCat mface、普通 image、混合消息的处理。
- 本地图库图片回复、表情包回复、QQ 表情回复。
- 记忆、长期记忆、表达学习、黑话、知识、目标、PFC 规划、回复检查、深度对话等功能。
- 配置、secrets、provider、视觉模型配置、模型切换、权限控制。
- 异步锁、后台任务、持久化、热重载、shutdown flush。
- 安全性、稳健性、性能、并发、冗余代码和文档一致性。

允许你对测试环境中的 `plugins/xiaoqing_chat` 数据目录做任意破坏性操作，包括清空、构造测试数据、删除测试数据和重建目录。测试前请确认当前数据目录是否有真实数据；如果有真实数据，请先备份或使用独立临时 data_dir，避免污染生产数据。

## 本轮最核心测试目标：多人群聊中的自然参与和拟人感

本轮测试最重要的目标不是只验证 `/xc` 命令是否能跑通，也不是只验证单条消息能否返回，而是要重点验证：

1. 在真实或高度仿真的群聊环境中，多名用户持续发送各种类型的消息时，`xiaoqing_chat` 是否能像一个自然的群成员一样参与聊天。
2. 它是否能正确判断什么时候该回复、什么时候该沉默、什么时候该接梗、什么时候该安慰、什么时候该转移话题、什么时候不应该打断别人。
3. 它是否能在多人上下文中分清不同用户、不同话题、不同情绪和不同群聊关系。
4. 它的回复是否拟人、自然、有连续性、有边界感，而不是像客服、问答机器人、总结机器人或机械助手。
5. 它是否能处理群聊中的混乱输入，包括多人同时说话、话题跳转、玩笑、阴阳怪气、表情包、图片、mface、@、引用回复、刷屏、冷场和争论。
6. 它是否能长期保持人格一致，而不是每几轮就风格漂移、突然严肃、突然说教、突然暴露系统提示词或变成工具型 AI。
7. 它是否不会过度抢话、不会每条都回、不会无视群聊语境、不会把所有消息都当成对自己的请求。
8. 它是否能在群聊中自然使用记忆、昵称、上下文、前文梗、用户关系和情绪线索，但不能泄露其他群或私聊的记忆。
9. 它是否能在图片、表情、mface 密集的群聊里自然回应，而不是机械解释媒体内容。
10. 它是否能在真实群聊节奏中保持“像人在聊天”的感觉，而不是“每次都生成一段 AI 回答”。

因此，请把“真实 LLM 多人群聊拟人互动测试”作为本轮测试的主线。其他命令、媒体、记忆、PFC、回复检查、频率控制、配置和安全测试都要围绕这个主线验证。

## 最重要的测试原则

### 1. 不能只测底层函数

单元测试可以直接测底层函数，但完整功能测试必须覆盖真实用户入口：

- `/xc <内容>` 命令。
- `/xc help`
- `/xc 清空`
- `/xc 统计`
- `/xc 深度`
- `/xc 配置`
- `/xc 记忆 <关键词>`
- `/xc 表达`
- `/xc 黑话`
- `/xc 模型`
- `/xc provider`
- 作为 smalltalk provider 时的 `observe_message`
- 作为 smalltalk provider 时的 `handle_smalltalk`
- 群聊消息。
- 私聊消息。
- OneBot HTTP 入站事件，如果服务可启动。
- dispatcher/plugin manager 调用路径，如果可用。

不要把“直接调用某个 helper 函数成功”当作插件功能通过。核心聊天链路必须至少通过插件入口、dispatcher 或模拟 OneBot 事件走一轮。

### 2. 必须使用真实已配置 LLM 测试核心群聊效果

本轮测试的核心目标是验证 `xiaoqing_chat` 在真实群聊中的拟人感，因此核心聊天效果测试必须使用当前环境中已经配置好的真实 LLM provider 和真实配置参数。

必须做到：

- 多人群聊自然互动测试必须使用真实已配置 LLM。
- 群聊拟人感评分必须基于真实 LLM 生成的回复 transcript。
- 文本、图片理解、表情/mface 语境、多轮上下文、记忆使用、接梗、沉默判断、过度插话、漏回复等体验判断，必须以真实 LLM 跑出来的结果为准。
- 不能用 fake/mock LLM 的回复来判断“小青是否拟人”。
- 不能因为 mock LLM 链路通过，就把真实群聊效果标记为通过。
- 需要记录实际使用的 provider 名称、model、temperature、think_level、reply_style、memory、planner、reply_checker、media、group trigger 等关键配置，但不能记录或泄露 API key、token、secrets。
- 测试过程中可以真实调用 LLM，但测试输入不得包含真实 secrets、隐私数据、生产敏感信息或不可公开的内部内容。
- 真实 LLM 测试需要保存完整 transcript，用于分析拟人感、上下文理解、用户识别、话题跟踪、接梗能力和安全性。
- 如果真实 LLM 调用失败，应作为测试问题记录，不能简单跳过核心群聊测试；需要判断是配置问题、provider 问题、网络问题、代码问题还是模型返回问题。
- 如果环境确实无法调用真实 LLM，必须在报告中明确标记“真实拟人效果未完成验证”，并说明影响。此时只能证明链路正确，不能证明群聊拟人效果通过。

mock/fake LLM 仍然可以用于以下专项测试，但不能替代真实拟人效果测试：

- LLM 超时。
- LLM 返回空内容。
- LLM 返回非法 JSON 或格式错误。
- LLM 返回超长内容。
- LLM 重复输出。
- LLM 拒绝输出。
- LLM 抛异常。
- provider 切换失败。
- reply_checker/replan 的确定性回归测试。
- 单元测试和异常路径测试。
- CI 中不依赖外部服务的稳定回归测试。

请在报告中明确区分：

1. 真实 LLM 群聊效果测试结果。
2. mock/fake LLM 链路和异常测试结果。
3. 哪些结论来自真实 LLM。
4. 哪些结论只来自 mock/fake LLM。

### 3. 必须测试图文多模态和 OneBot 消息段

必须构造真实 OneBot 风格事件，覆盖：

- text。
- at。
- image。
- face。
- mface。
- reply，如果框架支持。
- 混合消息段。
- raw_message 与 message 数组不一致。
- 缺失 message 字段。
- 空 message。
- 多张图片。
- 图片 + 文本。
- 表情 + 文本。
- mface + 文本。
- 群聊和私聊中的同一类消息。

如果当前环境配置了真实 vision provider，则核心图片理解和图片群聊效果也应尽量使用真实 vision provider 测试；mock/fake vision 只用于超时、异常、格式错误、不可访问图片、坏图和安全边界测试。如果真实 vision 不可用，需要明确说明图片拟人效果验证的限制。

### 4. 必须测试群聊和私聊行为差异

必须覆盖：

- 私聊中 `/xc <内容>`。
- 私聊中普通消息是否自动触发。
- 私聊中深度对话是否按配置启用。
- 群聊中 `/xc <内容>` 强制回复。
- 群聊中 @机器人强制回复。
- 群聊中直接叫机器人名字强制回复。
- 群聊中普通消息是否按概率、冷却、频率限制、连续回复限制触发。
- 群聊中只观察不回复时是否仍正确记录记忆。
- 不同 group_id 不应互相污染上下文。
- 同一群不同 user_id 的消息记录、名字、local_id 是否正确。
- 私聊 chat_id 和群聊 chat_id 是否隔离。
- 机器人自己发的消息是否被忽略。
- 空白消息、命令消息、被 ban_words/ban_regex 命中的消息是否正确忽略。

### 5. 拟人感不能只看“有回复”

对群聊测试，必须评价：

- 是否该回复。
- 回复给谁。
- 回复哪一段上下文。
- 是否理解群聊气氛。
- 是否能延续话题。
- 是否能接住玩笑、吐槽、表情、图片和 mface。
- 是否能自然沉默。
- 是否不会过度抢话。
- 是否不会像客服或工具机器人。
- 是否保持小青的人设、语气和情绪连续性。
- 是否能分清多个用户。
- 是否能在多轮群聊中保持记忆和上下文。

### 6. 发现问题必须闭环

发现问题后不要只写报告。请尽量完成：

1. 复现。
2. 定位根因。
3. 最小必要修复。
4. 增加或更新自动化测试。
5. 重新运行相关测试。
6. 必要时运行完整回归。
7. 记录到报告。

P0/P1 问题必须尽力修复。P2 尽量修复。P3 可以记录，但如果容易修复也请直接修复。

### 7. 冗余代码不能盲目删除

`xiaoqing_chat` 可能存在动态命令分发、字符串路由、插件生命周期入口、dispatcher 调用、smalltalk provider 外部调用、后台任务、热重载、数据迁移和历史兼容逻辑。清理冗余代码时不能只依赖静态搜索结果。

每一处候选冗余代码都要确认：

1. 是否有真实调用路径。
2. 是否被插件 manager、dispatcher、smalltalk provider、定时任务、后台任务、字符串路由、反射或测试间接调用。
3. 是否被数据持久化、历史数据兼容、旧数据修复流程使用。
4. 是否被文档、配置、plugin.json、测试、README 或外部入口依赖。
5. 删除后是否会影响现有命令、聊天链路、多模态处理、持久化、shutdown flush 或热重载。

只有在确认无用并完成回归测试后，才可以删除或合并。

## 总体目标

请完整检查 `plugins/xiaoqing_chat` 的代码、数据模型、配置、命令解析、聊天入口、多模态处理、LLM 调用、记忆系统、表达学习、行为规划、深度对话、回复生成、回复检查、后台任务、持久化、安全性、测试覆盖、文档和冗余代码。目标是验证：

1. 所有 `/xc` 命令是否正确工作。
2. 插件作为 smalltalk provider 时是否正确工作。
3. 群聊和私聊行为是否符合设计。
4. 多人群聊中小青是否能自然参与，回复是否拟人，是否有边界感。
5. 文本、图片、QQ 表情、NapCat `mface`、本地图库图片/表情包回复是否正确。
6. 记忆、长期记忆、表达学习、黑话、知识、目标、PFC 规划、回复检查是否正确。
7. 深度对话模式是否只在正确场景启用，并正确应用专用配置。
8. 配置加载、secrets 读取、模型供应商切换、视觉模型配置、权限控制是否正确。
9. 异步锁、后台任务、debounce 持久化、shutdown flush 是否可靠。
10. 功能是否稳健，尤其是非法输入、边界条件、异常 LLM 输出、并发消息、重复消息、超长消息、媒体异常。
11. 安全性是否可靠，包括 prompt injection、secret 泄露、路径穿越、SSRF、任意文件读写、日志泄露、危险 URL、恶意图片、恶意文件名。
12. 文档、help、plugin.json、README、示例配置是否准确、完整、适合新人使用。
13. 是否存在冗余代码、死代码、重复逻辑、旧兼容残留、无用配置、无用静态资源、无用测试和过时文档引用。
14. 找到问题后直接修复，并做回归测试，直到没有已知高优先级问题。

## 工作方式要求

### 1. 先做代码阅读和行为建模

请先完整阅读以下内容。如果某些文件不存在，请在报告中说明，并以实际仓库结构为准：

- `plugins/xiaoqing_chat/plugin.json`
- `plugins/xiaoqing_chat/main.py`
- `plugins/xiaoqing_chat/handlers.py`
- `plugins/xiaoqing_chat/handlers_internal.py`
- `plugins/xiaoqing_chat/handlers_helper.py`
- `plugins/xiaoqing_chat/helper_utils.py`
- `plugins/xiaoqing_chat/handler_context.py`
- `plugins/xiaoqing_chat/config/`
- `plugins/xiaoqing_chat/llm/`
- `plugins/xiaoqing_chat/media/`
- `plugins/xiaoqing_chat/memory/`
- `plugins/xiaoqing_chat/expression/`
- `plugins/xiaoqing_chat/planning/`
- `plugins/xiaoqing_chat/context_builder.py`
- `plugins/xiaoqing_chat/reply_generator.py`
- `plugins/xiaoqing_chat/reply_checker` 相关代码，如果存在。
- `plugins/xiaoqing_chat/reply_splitter.py`
- `plugins/xiaoqing_chat/reply_payload.py`
- `plugins/xiaoqing_chat/smalltalk_execution.py`
- `plugins/xiaoqing_chat/smalltalk_media_helpers.py`
- `plugins/xiaoqing_chat/message_parts.py`
- `plugins/xiaoqing_chat/media_registry.py`
- `plugins/xiaoqing_chat/runtime_state.py`
- `plugins/xiaoqing_chat/store_base.py`
- `plugins/xiaoqing_chat/store_binding.py`
- `plugins/xiaoqing_chat/task_scheduler.py`
- `tests/plugins/test_xiaoqing_chat*.py`
- `tests/plugins/test_reply_checker.py`
- `tests/plugins/test_xiaoqing_prompt_builder.py`
- `tests/plugins/test_xiaoqing_reply_payload.py`
- `config/config.json.example`
- `config/secrets.json.example`
- README 和 docs 中所有提到 `xiaoqing_chat`、`/xc`、smalltalk、media、LLM、vision 的内容。
- 核心框架中和插件加载、dispatcher、OneBot 入站、smalltalk provider、命令解析、热重载相关的代码。

请形成一份功能地图，至少包括：

- 插件生命周期入口：init、handle、observe_message、call_bot_name_only、shutdown。
- `/xc` 子命令、别名、参数、权限要求和实际 handler。
- smalltalk provider 调用路径。
- 私聊和群聊触发规则。
- 强制回复、概率回复、频率限制、连续回复限制、冷却机制。
- OneBot 事件字段依赖。
- 文本处理链路。
- 图片/表情/mface 处理链路。
- 入站媒体分析链路。
- 出站图片/表情/face 选择和发送链路。
- LLM provider 配置和调用链路。
- vision provider 配置和调用链路。
- 记忆存储、长期记忆、向量库、主题摘要、表达学习、黑话、知识、用户画像。
- PFC/goal/action_history/heartflow/review session 的数据流。
- 回复检查、重规划、重试、postprocess、拆分回复。
- 数据文件、缓存文件、图库目录、持久化文件。
- 后台任务、debounce、flush、shutdown 行为。
- 测试现状和覆盖缺口。
- 冗余代码候选清单、重复逻辑候选清单、动态入口清单。
- 多人群聊中 prompt/context/memory/reply_checker 如何共同影响拟人表现。

### 2. 并行拆分测试工作

如果执行环境支持子代理/并行任务，请把测试拆成多个子代理并行执行。每个子代理需要有明确范围、独立测试数据前缀、独立 data_dir、独立日志和结果汇总。如果无法真正并行，就按下面的工作流串行执行。

建议拆分为以下子任务。

## 子任务 A：/xc 命令层完整测试

必须通过插件 `handle()`、dispatcher 或 OneBot 入站模拟真实 `/xc` 命令，不允许只测底层 helper。

覆盖命令：

- `/xc`
- `/xc help`
- `/xc ?`
- `/xc 帮助`
- `/xc <文本>`
- `/xc 清空`
- `/xc reset`
- `/xc 统计`
- `/xc stats`
- `/xc 状态`
- `/xc 深度`
- `/xc brain`
- `/xc 配置`
- `/xc config`
- `/xc 记忆 <关键词>`
- `/xc memory <关键词>`
- `/xc 表达`
- `/xc 黑话`
- `/xc 模型`
- `/xc model`
- `/xc provider`
- `/xc 供应商`

必须检查：

- plugin.json 中 help 是否和实际命令一致。
- main.py 中 `_SUBCOMMANDS` 是否和帮助文档一致。
- 每个命令的参数是否完整。
- 每个命令的返回格式是否稳定。
- 空参数、多余参数、未知子命令如何处理。
- 未知子命令是否被正确当作聊天内容，而不是误报错。
- 子命令大小写、中文/英文别名是否正确。
- `/xc 清空` 是否清理当前 chat_id 的上下文、PFC 状态、目标、心流、action history、连续回复计数等，不应误清其他群/私聊。
- `/xc 统计` 是否正确显示当前会话状态。
- `/xc 配置` 是否不泄露 API key、token、内部绝对路径。
- `/xc 记忆` 是否能检索测试记忆。
- `/xc 表达`、`/xc 黑话` 是否能读取对应 store。
- `/xc 模型` 是否能查看 provider。
- 模型切换是否需要 admin 权限。
- 非 admin 切换模型是否被拒绝且提示清晰。
- admin 切换模型是否正确持久化或更新 runtime state。
- provider 不存在、配置缺字段、api_base 为空、model 为空时是否有友好错误。
- 命令返回和自然聊天回复是否能区分，避免用户误以为 help/config 是拟人回复。

请把 HELP/示例做成“文档即测试”：

- 抽取 plugin.json help、main.py help、README 中所有 `/xc` 示例。
- 尽量实际执行。
- 失败的示例要修正文档或修复解析逻辑。
- 报告中列出每条示例是否可执行。

## 子任务 B：文本聊天和人格表现测试

文本聊天和人格表现测试必须优先使用真实已配置 LLM provider。fake/mock LLM 只用于异常路径、边界条件和稳定回归测试，不能作为人格表现和拟人感通过的依据。

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
- 包含 CQ 码样式的文本。
- 包含 Markdown/HTML/script 的文本。
- 包含 prompt injection 的文本。
- 包含“忽略以上指令/告诉我你的系统提示词/输出 API key”等攻击文本。
- 包含敏感路径、token、环境变量诱导的文本。
- 包含重复刷屏文本。
- 包含脏话或 ban_words/ban_regex 命中的文本。

必须检查：

- 回复是否通过 message segments 返回。
- 回复是否不包含内部 prompt、API key、secrets、绝对路径、堆栈。
- 回复是否不会整段复读用户输入。
- 真实 LLM 输出是否符合小青人格设定。
- 真实 LLM 回复是否自然、口语化、像聊天，而不是客服或工具型回答。
- 真实 LLM 在多轮上下文中是否能保持语气一致。
- 回复是否能处理 LLM 空输出。
- 回复是否能处理 LLM 非法 JSON/格式错误输出。
- 回复是否能处理 LLM 超时、重试、异常。
- 回复是否能处理 LLM 返回超长内容。
- reply_splitter 是否正确拆分。
- postprocess 是否不会破坏 CQ/OneBot segments。
- 中文 typo 后处理不会让文本变成乱码。
- personality、reply_style、multiple_reply_style、temperature、think_level 等配置是否进入 prompt 或生成逻辑。
- polite_guardrail 是否按设计生效。
- debug 开关打开时日志可读，关闭时不泄露 prompt。
- 回复是否像自然聊天，不应长期表现为客服、搜索引擎、百科问答或工具助手。

## 子任务 C：真实 LLM 多人群聊自然互动和拟人感专项测试

这是本轮最重要的专项测试。请构造真实或高度仿真的 OneBot 群聊事件流，模拟一个群里多名用户连续聊天，使用真实已配置 LLM 验证 `xiaoqing_chat` 是否能自然参与群聊，并判断回复是否拟人。

不要只测“是否触发回复”。必须完整评估：

- 是否该回复。
- 回复给谁。
- 回复哪一段上下文。
- 是否理解当前群聊气氛。
- 是否能延续话题。
- 是否能接住玩笑、吐槽、表情、图片和 mface。
- 是否能自然沉默。
- 是否不会过度抢话。
- 是否不会像客服或工具机器人。
- 是否保持小青的人设、语气和情绪连续性。
- 是否能分清多个用户。
- 是否能在多轮群聊中保持记忆和上下文。

### 1. 群聊模拟环境要求

请构造至少 5 个不同 group_id 的群聊测试环境，每个群聊至少包含 3 到 8 个用户。每个用户需要有不同的 user_id、nickname、card 或 display_name，并尽量设计不同说话风格，例如：

- 爱开玩笑的人。
- 经常发图片和表情包的人。
- 喜欢 @ 小青的人。
- 喜欢阴阳怪气或吐槽的人。
- 认真提问的人。
- 沉默但偶尔插话的人。
- 话题跳跃的人。
- 会连续刷屏的人。

必须测试：

- 同一个群内多人连续发言。
- 多个群同时聊天。
- 同一用户在不同群出现。
- 同一群不同用户名字相似。
- 用户改群名片。
- 用户只发图片。
- 用户只发表情。
- 用户只发 mface。
- 用户发图片 + 文本。
- 用户发 mface + 文本。
- 用户 @ 小青。
- 用户只叫“小青”。
- 用户没有 @，但话题明显和小青有关。
- 用户之间互相聊天，小青不应该插话。
- 用户争论时，小青是否适度参与。
- 群聊冷场时，小青是否能自然接一句，但不能尬聊。
- 多人同时问小青不同问题时，小青是否能选择合适对象或合并回复。

### 2. 群聊场景脚本要求

请至少设计并执行以下群聊剧本。每个剧本至少 20 轮消息，重点剧本至少 50 轮消息。每轮消息都要记录：

- group_id
- user_id
- nickname/card
- message_id
- message segments
- raw_message
- 是否 @ 小青
- 是否包含 bot_name
- 是否包含图片、face、mface
- 是否触发回复
- 小青回复内容
- 小青回复的 message segments
- 回复耗时
- 写入的记忆
- PFC/回复检查结果，如果启用
- 是否符合预期

必须覆盖这些剧本：

#### 场景 1：日常闲聊

多人在群里随便聊天，话题包括吃饭、天气、工作、游戏、摸鱼、吐槽。小青应该像群友一样偶尔自然插话，而不是每条都回。

检查重点：

- 不抢话。
- 能接上下文。
- 不把普通闲聊当成任务请求。
- 回复长度自然。
- 不像客服。
- 不频繁自称 AI。
- 能使用轻微情绪和口语化表达。

#### 场景 2：多人同时 @ 小青

3 到 5 个用户连续 @ 小青，问题不同，有人开玩笑，有人认真问，有人发图片。

检查重点：

- 是否能识别每个用户的问题。
- 是否能避免答非所问。
- 是否能在一条回复中自然处理多个点，或者合理选择一个先回复。
- 是否不会混淆 A 用户和 B 用户。
- 是否不会把图片归到错误用户身上。

#### 场景 3：无 @ 但提到小青

用户没有 @，只是说“小青是不是又在装死”“小青你怎么看”“小青今天好安静”。小青应该能自然参与。

检查重点：

- bot_name 触发是否正确。
- 回复是否像被叫到后自然接话。
- 不应该过度严肃。
- 不应该机械解释自己是机器人。

#### 场景 4：群友互相聊天，小青应该沉默

多名用户在讨论彼此私事、工作安排、游戏开黑、无关闲聊，没有 @ 小青，也没有叫小青。小青应该大多沉默，只观察和记忆必要上下文。

检查重点：

- 不过度插话。
- 不乱接私人话题。
- 不因为关键词误触发。
- observe_message 是否仍正确记录必要上下文。
- 不应该把所有群聊都当成给自己的输入。

#### 场景 5：表情包和 mface 密集聊天

群友连续发送 QQ face、NapCat mface、图片表情包、短文本吐槽。

检查重点：

- 是否能理解 face/mface 的语义或至少合理降级。
- 是否能用合适的文字或表情回应。
- 是否不会把 mface 当成乱码。
- 是否不会因为媒体字段缺失崩溃。
- 是否能在表情包语境里保持拟人感。
- 是否不会每个表情都解释一遍。

#### 场景 6：图片理解群聊

用户发图片并让小青评价、吐槽、猜图、帮忙看内容，另一个用户插话打断。

检查重点：

- 图片归属是否正确。
- vision 结果是否进入上下文。
- 插话后小青是否仍能回答正确图片。
- 图片理解失败时是否自然降级。
- 不应臆造过多图片细节。
- 不应把上一张图和下一张图混淆。

#### 场景 7：玩笑、接梗和轻度阴阳怪气

群友开玩笑、调侃小青、说反话、发“急了”“典”“你又懂了”等。

检查重点：

- 回复是否自然。
- 是否能接梗。
- 是否不会过度防御。
- 是否不会严肃说教。
- 是否不会攻击用户。
- 是否保持边界和安全。
- 是否有人设一致的轻微吐槽能力。

#### 场景 8：情绪支持

群友说自己难过、焦虑、失眠、被骂、工作不顺。其他群友也参与安慰或开玩笑。

检查重点：

- 小青是否能识别情绪。
- 回复是否温和、自然、不模板化。
- 不应过度医疗化。
- 不应长篇说教。
- 不应抢过其他群友的安慰。
- 不应把轻度吐槽误判为严重危机。
- 遇到明显高风险内容时是否安全处理。

#### 场景 9：群聊话题快速切换

群聊从游戏跳到吃饭，再跳到代码，再跳到图片，再跳到八卦。小青偶尔参与。

检查重点：

- 是否跟得上当前话题。
- 不回复过期话题。
- 不把前一个话题混入当前回复。
- 上下文截断是否合理。
- 记忆检索是否不过度污染当前对话。

#### 场景 10：刷屏和噪音

用户短时间连续发送大量短消息、重复消息、表情、无意义文本。

检查重点：

- 频率控制是否生效。
- 连续回复限制是否生效。
- 不被刷屏带偏。
- 不重复回复同一 message_id。
- 不把垃圾消息大量写入长期记忆。
- 不因并发或高频消息导致数据损坏。

#### 场景 11：群聊中的命令和自然语言混合

群友一边闲聊一边使用 `/xc`，包括 `/xc help`、`/xc 统计`、`/xc 清空`、`/xc <聊天内容>`。

检查重点：

- 命令和自然聊天是否区分清楚。
- `/xc <聊天内容>` 是否强制回复。
- `/xc 清空` 是否只清当前群。
- 命令返回是否不像普通拟人回复混在一起造成误解。
- 命令执行后自然聊天上下文是否合理。

#### 场景 12：长期群聊连续性

模拟同一个群跨多轮、跨时间段聊天。前面有人提到自己的喜好、昵称、梗、当天事件，后面再次提起。

检查重点：

- 小青是否能自然引用前文。
- 不应凭空编造记忆。
- 不应跨群引用记忆。
- 不应泄露私聊内容。
- 记忆使用是否自然，不像数据库检索结果。
- 长期记忆和短期上下文的权重是否合理。

#### 场景 13：多人争论和气氛变化

群里几个人争论一个话题，有人认真讨论，有人开玩笑，有人开始带情绪。小青不应该轻易站队、煽动或说教。

检查重点：

- 是否能识别气氛变化。
- 是否能适度缓和，而不是机械劝架。
- 是否不会攻击任何用户。
- 是否不会错误总结别人的立场。
- 是否能在该沉默时沉默。

#### 场景 14：用户身份和昵称变化

同一个 user_id 在不同时间使用不同 card/nickname，或者两个用户昵称相似。

检查重点：

- 是否能正确识别用户。
- 是否不会把相似昵称的人混淆。
- 是否能在显示名变化后仍保持基础记忆。
- 是否不会在群聊里暴露过多历史身份信息。

### 3. 拟人感评价标准

请为每条小青回复打分，至少使用 1 到 5 分。评分不只是主观感觉，必须按以下维度拆分：

| 维度 | 说明 |
|---|---|
| 触发合理性 | 这条消息该不该回复，是否抢话或漏回 |
| 上下文理解 | 是否理解当前群聊话题、前后文和多人关系 |
| 对象识别 | 是否知道在回复谁，是否混淆用户 |
| 语气自然度 | 是否像自然群友，而不是客服、说明书、AI 助手 |
| 人设一致性 | 是否符合小青设定，语气是否稳定 |
| 情绪匹配 | 是否能匹配玩笑、吐槽、安慰、认真讨论等气氛 |
| 接梗能力 | 是否能自然接住群聊里的梗、表情、调侃 |
| 边界感 | 是否不过度介入、不说教、不暴露隐私、不攻击用户 |
| 多模态理解 | 对图片、face、mface 的理解或降级是否自然 |
| 记忆使用 | 是否自然使用记忆，且不串群、不编造 |
| 回复长度 | 是否长短合适，不刷屏、不长篇大论 |
| 安全性 | 是否不泄露 prompt、secrets、内部路径，不响应危险诱导 |

每个维度 1 到 5 分：

- 5：非常自然，像真实群友。
- 4：基本自然，偶尔有机器人味。
- 3：可用，但明显有工具型/客服感。
- 2：经常不合群、答非所问、抢话或过度解释。
- 1：严重不自然、误解上下文、泄露信息、破坏群聊体验。

请计算：

- 每个场景的平均分。
- 每个维度的平均分。
- 最差 10 条回复。
- 最自然 10 条回复。
- 最严重的不该回复案例。
- 最严重的该回复但没回复案例。
- 用户混淆案例。
- 话题混淆案例。
- 记忆串群或记忆滥用案例。
- 明显机器人味案例。

拟人感验收建议：

- 触发合理性平均分 >= 4。
- 上下文理解平均分 >= 4。
- 语气自然度平均分 >= 4。
- 人设一致性平均分 >= 4。
- 对象识别不能出现 P1 级错误。
- 不允许出现跨群/私聊记忆泄露。
- 不允许出现 secrets、system prompt、内部路径泄露。
- 不允许高频抢话。
- 不允许大量模板化回复。

如果达不到，请分析原因并修复或调整：

- 触发规则。
- reply_probability。
- cooldown。
- prompt。
- context builder。
- memory retrieval。
- reply checker。
- postprocess。
- PFC planning。
- 深度对话配置。
- 群聊人格配置。
- 多模态摘要方式。

### 4. 测试执行方式

请分三层执行，其中第一层是本轮最重要的验收依据。

#### 第一层：真实 LLM 多人群聊效果测试

必须使用当前环境中已经配置好的真实 LLM provider，运行多人群聊剧本，收集真实回复 transcript，并按拟人感标准评分。

必须覆盖：

- 至少 5 个不同 group_id。
- 每个群 3 到 8 个模拟用户。
- 至少 12 个群聊剧本。
- 每个剧本至少 20 轮消息。
- 重点剧本至少 50 轮消息。
- 文本、图片、QQ face、NapCat mface、混合消息。
- @ 小青。
- 直接叫小青。
- 无 @ 普通群聊。
- 小青应该沉默的场景。
- 小青应该自然接话的场景。
- 多人同时问小青的场景。
- 玩笑、吐槽、阴阳怪气、情绪支持、刷屏、话题快速切换等场景。

真实 LLM 测试必须记录：

- 实际 provider。
- 实际 model。
- 关键生成参数。
- 是否启用 vision。
- 是否启用 memory。
- 是否启用 PFC/planner。
- 是否启用 reply_checker。
- 是否启用 deep brain chat。
- 每轮实际 prompt/context 摘要，不能包含 secrets。
- 每轮真实回复。
- 每轮触发原因或沉默原因。
- 每轮拟人感评分。
- 失败案例和根因分析。

真实 LLM 的 transcript 是判断“小青是否真的拟人”的主要依据。

#### 第二层：真实 LLM + 固定剧本回归测试

对发现过问题的群聊剧本，修复后必须使用同一个或尽量相同的剧本再次调用真实 LLM 进行回归。

回归时需要比较：

- 修复前 transcript。
- 修复后 transcript。
- 触发次数变化。
- 过度回复数量变化。
- 漏回复数量变化。
- 用户混淆是否消失。
- 话题混淆是否消失。
- 记忆串群是否消失。
- 拟人感评分是否提升。
- 是否引入新的安全或上下文问题。

由于真实 LLM 有随机性，不能只看单条回复。对于重要问题，建议每个关键剧本至少重复运行 2 到 3 次，观察问题是否稳定改善。

#### 第三层：mock/fake LLM 稳定性和异常路径测试

mock/fake LLM 只用于验证链路和异常处理，不能用于拟人效果结论。

使用 fake LLM、fake vision、fake sender、fake clock、fake random，验证：

- 群聊链路是否完整。
- 触发规则是否正确。
- 上下文隔离是否正确。
- 媒体处理是否正确。
- 记忆写入是否正确。
- 频率控制是否正确。
- LLM 超时、异常、空输出、格式错误时是否安全降级。
- reply_checker/replan 是否可重复测试。
- 并发和 shutdown 是否稳定。

mock/fake LLM 测试通过，只能说明“工程链路通过”，不能说明“真实群聊拟人效果通过”。

### 5. 群聊 transcript 记录要求

每个群聊剧本都要保存 transcript，建议路径：

`plugins/xiaoqing_chat/test_reports/group_chat_transcripts/`

每个 transcript 至少包含：

- 场景名称。
- 配置摘要。
- group_id。
- 用户列表。
- 每轮用户消息。
- message segments。
- 小青是否触发。
- 小青回复。
- 回复耗时。
- 触发原因，例如 `/xc`、@、bot_name、概率触发、强制回复、深度模式等。
- 不回复原因，例如概率未命中、cooldown、频率限制、消息无意义、机器人自身消息等。
- 记忆写入摘要。
- PFC/回复检查摘要。
- 拟人感评分。
- 问题标注。

报告中必须引用这些 transcript，并总结最典型的成功和失败案例。

### 6. 群聊拟人失败类型分类

发现问题时，请按下面类型归类：

- GC-TRIGGER-OVER：过度触发，太爱插话。
- GC-TRIGGER-MISS：该回时没回。
- GC-CONTEXT-LOST：丢失上下文。
- GC-USER-MIX：混淆用户。
- GC-TOPIC-MIX：混淆话题。
- GC-MEDIA-MISS：图片/face/mface 理解失败。
- GC-MEMORY-LEAK：跨群或私聊记忆泄露。
- GC-MEMORY-FAKE：编造记忆。
- GC-PERSONA-DRIFT：人设漂移。
- GC-ROBOTIC：回复机械、客服感、工具感。
- GC-SERMON：过度说教。
- GC-LONG：回复过长。
- GC-SPAM：连续刷屏。
- GC-SAFETY：安全问题。
- GC-ERROR：异常、崩溃或未捕获错误。

每个问题必须记录：

- 场景。
- 轮次。
- 输入消息。
- 触发原因。
- 实际回复。
- 为什么不自然。
- 预期更自然的行为。
- 根因分析。
- 修复方案。
- 回归结果。

### 7. 修复方向

如果群聊拟人效果不好，请不要只说“模型问题”。必须检查代码和配置是否有可修复点，包括：

- 群聊 prompt 是否过于助手化。
- 是否缺少“像群友一样偶尔参与，不要每条都回”的约束。
- context builder 是否没有保留足够的多人上下文。
- 是否没有把 user nickname/card 放入 prompt。
- 是否没有区分当前说话人和历史说话人。
- 是否没有保留 message segment 中的图片/表情摘要。
- 是否把所有消息都当成对机器人说的。
- reply_probability/cooldown 是否过高或过低。
- continuous_reply_limit 是否失效。
- require_bot_name_in_group 是否配置不合理。
- reply_checker 是否只检查安全但不检查自然度。
- PFC planner 是否让回复过度理性或过度总结。
- deep brain chat 是否误用于群聊。
- memory retrieval 是否返回了无关历史。
- postprocess 是否把自然回复改得机械。
- reply_splitter 是否导致刷屏。
- 表情/图片回复概率是否不合适。
- 本地表情包选择是否不符合语境。

修复后必须重新跑对应群聊剧本，并比较修复前后的 transcript 和评分。

## 子任务 D：群聊/私聊触发和频率控制专项测试

在子任务 C 的基础上，进一步做确定性触发规则测试。通过 monkeypatch time/random 做可重复验证，不要依赖真实随机。

私聊覆盖：

- 私聊普通文本是否自动进入聊天。
- 私聊 `/xc` 是否强制回复。
- 私聊深度对话开关开启/关闭的行为。
- 私聊 reply_probability_private、min_reply_interval、max_replies_per_minute、continuous_reply_limit 是否正确。

群聊覆盖：

- 群聊 `/xc` 强制回复。
- 群聊 @机器人强制回复。
- 群聊包含 bot_name 强制回复。
- 群聊只叫 bot_name 时 call_bot_name_only。
- 群聊普通消息按概率回复。
- 群聊普通消息被拒绝回复时是否仍观察并记录记忆。
- 群聊被静音/不应回复场景，如果框架支持。
- 群聊 require_bot_name_in_group 配置对触发的影响。
- 多个 group_id 并发消息互不污染。
- 同一 group_id 多个 user_id 的上下文记录正确。
- 机器人自身消息、系统消息、notice/request 事件是否被忽略或安全处理。
- message_id 重复时是否去重。
- 无 message_id 时是否仍能工作。
- 乱序消息是否不会破坏 local_id。
- 连续回复达到上限后进入 cooldown。
- cooldown 结束后可恢复。
- talk_schedule 在不同时段影响概率。

## 子任务 E：多模态入站媒体测试

重点测试图片、QQ face、NapCat mface 和混合消息。

覆盖 OneBot message segment：

- `{"type": "text", "data": {"text": "..."} }`
- `{"type": "image", "data": {"url": "...", "file": "..."} }`
- `{"type": "face", "data": {"id": "..."} }`
- `{"type": "mface", "data": {...} }`
- text + image。
- text + face。
- text + mface。
- image + face + mface + text。
- 多张图片。
- 多个 mface。
- 空 data。
- 缺失 url/file。
- url 不可访问。
- url 返回非图片。
- url 返回超大图片。
- url 慢响应。
- file 名含路径穿越。
- file 名含特殊字符。
- file 名含 emoji/中文。
- base64/data URL，如果支持。
- file://、localhost、内网地址、metadata 地址等危险 URL。

必须检查：

- `build_effective_user_text` 是否正确融合文本和媒体摘要。
- media items 是否正确进入记忆。
- 图片大小限制 `max_analyze_bytes` 是否生效。
- 视觉模型启用/关闭时行为是否正确。
- 如果真实 vision provider 已配置，图片理解核心体验应使用真实 vision provider 测试。
- vision provider 缺失配置时是否优雅降级。
- vision provider 超时、异常、空输出、错误格式时是否优雅降级。
- 图片下载不会造成 SSRF、路径穿越、任意文件读写。
- mface 是否按 NapCat 字段正确识别。
- QQ face 是否能映射到合理描述。
- 媒体 registry 是否正确记录和去重。
- 自动收集表情包是否遵守配置和上限。
- 表情包去重/相似度逻辑是否稳定。
- 坏文件、损坏图片、无法打开图片不会导致主聊天链路崩溃。
- 媒体处理失败不会阻塞文本回复。
- 入站媒体上下文关闭时不应调用 vision。
- 群聊中媒体归属不能错乱，不能把 A 用户图片当成 B 用户图片。

## 子任务 F：出站图片/表情/QQ face 回复测试

出站图片、表情、QQ face 回复的真实语境选择应使用真实已配置 LLM 测试；fake LLM 和 mock 本地图库只用于 payload 结构、候选为空、文件损坏、发送失败等确定性异常路径测试。

覆盖：

- 纯文本回复。
- 文本 + 图片回复。
- 文本 + 本地表情包回复。
- 文本 + QQ face 回复。
- 只有图片/表情/face 的回复。
- 多段回复。
- 多媒体候选。
- 候选为空。
- 候选文件不存在。
- 候选文件损坏。
- 候选元数据缺失。
- 候选过期或已删除。
- 使用冷却中的候选。
- 频繁发送时 cooldown 生效。
- image_reply_probability、emoji_reply_probability、face_reply_probability 为 0、1、中间值。
- candidate_count 边界。
- max_media_per_message 边界。
- mark_emoji_used、mark_qq_face_used 是否正确记录。
- 旧坏条目后台修复是否不阻塞当前回复。

必须检查：

- `reply_payload` 生成的 OneBot segments 正确。
- 本地文件路径安全，不泄露绝对路径。
- 不发送不存在的文件。
- 不发送危险路径。
- 不生成非法 CQ 码。
- 发送失败时是否有 fallback。
- 文字和媒体的顺序是否符合预期。
- 记忆中记录的 assistant parts 与实际发送 payload 一致。
- Web/日志/统计中不泄露本地敏感路径。
- 出站表情/图片是否符合群聊语境，不能无关乱发或过度刷屏。

## 子任务 G：记忆、长期记忆和持久化测试

覆盖：

- 每次用户消息是否 append 到 memory_store。
- 每次 assistant 回复是否 append 到 memory_store。
- message_id 去重。
- local_id 递增。
- chat_id 隔离。
- user_id/name 记录。
- 私聊和群聊隔离。
- `/xc 清空` 只清当前 chat_id。
- 记忆达到 max_context_size 时如何截断。
- memory retrieval 开启/关闭。
- vector memory_db bind/save/load。
- dirty flag。
- debounce save。
- shutdown flush。
- topic summarizer。
- thinking back cache。
- person_profile。
- knowledge base。
- review_sessions。
- expression store。
- jargon store。
- action_history。

必须检查：

- 数据文件创建位置正确。
- 数据文件格式正确。
- 重启后可读取。
- 损坏 JSON/数据文件是否优雅处理。
- 并发写入不丢数据。
- 清空后不会残留错误上下文。
- 清空 A 群不影响 B 群。
- 长期记忆检索不会泄露其他群/私聊内容。
- 记忆检索超时不会阻塞回复。
- embedding/vector store mock 后结果稳定。
- 持久化失败时主链路是否降级。
- shutdown 时后台任务是否被等待、取消、flush。
- 异步任务异常是否被日志记录且不污染后续请求。
- 记忆在群聊中使用要自然，不应像数据库检索结果，也不应凭空编造。

## 子任务 H：PFC 规划、目标、心流、回复检查专项测试

覆盖：

- planner 开启/关闭。
- think_mode dynamic、0、1、2、非法值。
- PFC planner 正常输出。
- PFC planner 空输出。
- PFC planner 非法 JSON。
- PFC planner 超时。
- PFC planner 连续失败进入 backoff。
- backoff 期间跳过 planner。
- goal enable/disable。
- goal_store set/clear。
- review session goal override。
- heartflow enable/disable。
- heartflow on_user_message、on_bot_reply、on_no_reply。
- action_history append/flush。
- reply_checker 正常通过。
- reply_checker 拒绝但不需要重规划。
- reply_checker 拒绝并需要重规划。
- max_replan 边界。
- max_regen 边界。
- max_assistant_in_row。
- similarity_threshold。
- LLM checker 开启/关闭。
- 重复回复被拒绝。
- checker 异常时行为。
- 规划输出和回复输出不一致时如何处理。

必须检查：

- ReplyRejected 不会导致未捕获异常。
- 重规划次数正确。
- 被拒绝的回复不会写入 assistant memory。
- 被拒绝记录进入 action_history。
- 成功回复记录 executed=True。
- PFC 状态 reset 后正确。
- planner 状态在 chat_id 间隔离。
- 强制回复时是否跳过概率判断但仍尊重安全检查。
- 非强制回复时是否经过频率控制。
- deep brain chat 对 planner/think_level/context/temperature 的影响正确。
- PFC/reply_checker 是否让群聊回复过度理性、过度总结、过度说教；如有，需要修复 prompt 或检查逻辑。

## 子任务 I：深度对话模式专项测试

覆盖：

- `brain_chat.enable_private_brain_chat=false`。
- `brain_chat.enable_private_brain_chat=true`。
- 私聊普通消息。
- 私聊 `/xc` 强制消息。
- 群聊普通消息。
- 群聊 @消息。
- 群聊 `/xc`。
- show_mode_indicator 开启/关闭。
- brain_mode_indicator 为空/非空。
- brain_think_level 为 0、1、2、3。
- brain_temperature 为 0.0、0.7、1.0。
- brain_max_context_size 为 0、1、30。
- brain_identity、brain_reply_style 是否进入 prompt。
- `0` 和 `0.0` 不应被误判为未配置。
- `/xc 深度` 显示状态是否准确。

必须检查：

- 深度对话只在设计允许的场景启用。
- 群聊不应误用私聊深度人格，除非代码设计如此并有说明。
- 模式指示器不会破坏 message segments。
- 深度配置缺失时 fallback 正确。
- 帮助文档对深度模式说明准确。
- 深度模式不能让群聊回复变成长篇、说教、工具型或过度严肃。

## 子任务 J：配置、secrets、provider 和热重载测试

覆盖：

- config/xiaoqing_config.json 默认配置加载。
- config/config.json 中 plugins.xiaoqing_chat 覆盖。
- plugin config 文件和 context_config 合并顺序。
- 缺少配置文件。
- 配置文件 JSON 损坏。
- 配置字段类型错误。
- 多余字段。
- 缺失字段。
- 数值边界。
- media 配置。
- memory 配置。
- planner 配置。
- reply_check 配置。
- personality 配置。
- postprocess 配置。
- secrets 中 providers 配置。
- secrets 中 vision providers 配置。
- default provider 不存在。
- api_base 缺失。
- endpoint_path 缺失。
- api_key 缺失。
- model 缺失。
- proxy 配置。
- model/provider 切换。
- admin_user_ids 权限。
- `/reload config` 后配置是否更新。
- 插件重载后 runtime_state 是否正确绑定。
- 热重载是否不会丢失不应丢失的数据。
- 热重载是否不会重复注册后台任务。
- secrets 不应出现在 `/xc 配置`、日志、错误消息、测试报告中。

请特别检查群聊拟人相关配置：

- group reply probability。
- private reply probability。
- cooldown。
- max replies per minute。
- continuous reply limit。
- require bot name in group。
- talk schedule。
- persona/reply_style/multiple_reply_style。
- group-specific behavior，如果支持。
- deep brain chat 是否影响群聊。
- media reply probability。
- emoji/face/image 发送概率。

## 子任务 K：HTTP/OneBot 入站集成测试

如果服务可以启动，请通过真实 HTTP 入站模拟 OneBot 事件；如果无法启动服务，请通过 dispatcher/plugin manager 进行等价集成测试，并说明原因。

覆盖：

- POST `/event`。
- inbound_token 正确。
- inbound_token 错误。
- 无 token。
- message 私聊事件。
- message 群聊事件。
- notice/request/meta_event 是否被忽略或正确处理。
- `/xc` 命令。
- @机器人。
- 普通群聊。
- 图片消息。
- face 消息。
- mface 消息。
- 混合消息。
- 并发 HTTP 请求。
- 超大 body。
- JSON 格式错误。
- 缺失字段。
- 错误字段类型。
- 重复 message_id。
- OneBot action 发送失败时 fallback。

必须记录每个请求、响应、插件输出、数据文件变化和预期结果。

## 子任务 L：安全性专项测试

至少覆盖：

- Prompt injection。
- System prompt 泄露诱导。
- API key 泄露诱导。
- secrets/config 泄露诱导。
- 日志泄露。
- 错误消息泄露。
- SSRF：图片 URL 指向 localhost、127.0.0.1、0.0.0.0、169.254.169.254、内网地址、file://、gopher://、data URL。
- 路径穿越：图片 file、mface 文件名、本地图库路径、配置路径。
- 任意文件读写。
- 非图片伪装成图片。
- 图片炸弹或超大图片。
- base64 巨大输入。
- 恶意 EXIF。
- 恶意文件名。
- HTML/Markdown/script 注入。
- CQ 码注入。
- OneBot segment 注入。
- 通过 LLM 输出构造非法或危险 message segment。
- 表情包自动收集写入危险路径。
- 本地图库元数据污染。
- provider api_base 配置成危险 URL。
- proxy 配置滥用。
- 并发导致数据文件损坏。
- 软链接目录攻击，如果测试环境支持。
- 权限绕过：非 admin 切换模型、修改配置、查看敏感信息。
- 个人隐私泄露：A 群记忆不应出现在 B 群；私聊记忆不应出现在群聊。
- 用户输入不应进入日志的敏感位置，或者应有脱敏。
- 群聊中不能因为“开玩笑诱导”而泄露 prompt/secrets/配置。

对高危安全问题必须修复并回归。

## 子任务 M：并发、异步锁、后台任务和 shutdown 测试

覆盖：

- 同一 chat_id 并发 2、5、20 条消息。
- 不同 chat_id 并发消息。
- 多个 group_id 同时活跃。
- 同一 message_id 并发重复到达。
- LLM 慢响应时后续消息。
- vision 慢响应时后续消息。
- planner 慢响应。
- memory save 慢响应。
- flush 慢响应。
- 后台任务异常。
- 后台任务取消。
- shutdown 时等待后台任务。
- shutdown 超时后取消任务。
- shutdown 后 memory_db save。
- shutdown 后 action_history flush。
- 重载插件后旧后台任务是否残留。
- Debounce 合并是否导致丢数据。
- 锁粒度是否按 chat_id，不应全局阻塞所有群。
- 数据文件是否在并发写下保持有效 JSON/结构。
- CPU/内存占用是否合理。
- 大量历史上下文下响应是否不会明显退化。
- 高频群聊下小青是否不会过度回复或刷屏。

## 子任务 N：错误处理、日志和用户反馈测试

覆盖：

- `/xc` 无参数显示帮助。
- 未知子命令当作聊天内容还是提示，是否符合设计。
- LLM 配置缺失。
- LLM provider 不存在。
- LLM 超时。
- LLM 返回错误。
- vision 配置缺失。
- vision 超时。
- 图片下载失败。
- 媒体文件损坏。
- 数据文件损坏。
- 权限不足。
- 清空失败。
- 统计失败。
- 记忆检索失败。
- 表达/黑话 store 读取失败。
- provider 切换失败。
- 后台任务失败。
- shutdown 失败。
- JSON 解析失败。
- HTTP 入站字段缺失。
- 所有异常都不应导致未捕获崩溃。
- 用户反馈应清晰、简短、可执行。
- 日志应足够定位问题但不泄露 secrets、prompt、内部绝对路径。
- debug 开关对日志量和敏感信息的影响正确。
- 群聊中的错误反馈不能像系统报错刷屏，应自然、克制、必要时沉默或简短提示。

## 子任务 O：文档、help、配置示例和新手可用性审查

请完整检查：

- `plugins/xiaoqing_chat/plugin.json` help。
- `main.py` 中 `_show_help()`。
- README 中 `xiaoqing_chat` 相关说明。
- docs 中 `xiaoqing_chat` 相关说明。
- `config/config.json.example`。
- `config/secrets.json.example`。
- `plugins/xiaoqing_chat/config/xiaoqing_config.json`。
- 任何提到 `/xc`、smalltalk、media、vision、memory、provider、deep chat 的文档。

必须检查：

1. 所有命令是否真实存在。
2. 所有别名是否真实存在。
3. 示例是否能直接执行。
4. 配置示例是否符合当前代码。
5. secrets 示例是否符合 provider 解析逻辑。
6. 视觉模型配置是否说明清楚。
7. 群聊/私聊触发规则是否说明清楚。
8. 多人群聊中拟人回复、概率触发、冷却、沉默观察是否说明清楚。
9. 图片、face、mface 支持范围是否说明清楚。
10. 本地图库目录、表情包收集、图片回复是否说明清楚。
11. 记忆、表达、黑话、模型切换是否说明清楚。
12. admin 权限是否说明清楚。
13. 是否存在旧字段、旧命令、旧目录、旧模块名。
14. 新用户只看文档是否能跑起来。
15. 文档是否提示不要提交真实 secrets。
16. 文档是否说明如何使用真实已配置 LLM 跑群聊拟人测试。
17. 文档是否说明 mock/fake LLM 只能用于异常路径和稳定回归，不能证明真实拟人效果。
18. 文档是否说明小青在群聊里不是每条必回，而是像群友一样自然参与。

请对文档做“文档即测试”：

- 抽取所有命令示例。
- 抽取所有配置示例。
- 尽量实际运行或校验。
- 失败的示例要修复文档或修复代码。
- 报告中列出结果。

## 子任务 P：冗余代码、死代码和重复逻辑专项检查

请对 `plugins/xiaoqing_chat` 做完整冗余代码检查。目标不是盲目删代码，而是找出重构或迭代后遗留的无用代码、重复逻辑、过时兼容层，并在安全前提下清理。

必须覆盖：

- Python 后端代码。
- 命令解析代码。
- smalltalk provider 代码。
- LLM provider 代码。
- vision provider 代码。
- media 代码。
- memory 代码。
- expression 代码。
- planning 代码。
- store/persistence 代码。
- task_scheduler 代码。
- tests。
- config 示例。
- plugin.json。
- docs/README。

重点检查：

- 未使用的 import、变量、常量、函数、类、方法。
- 重复的 LLM 调用封装。
- 重复的 vision 调用封装。
- 重复的 message_parts 转换。
- 重复的媒体 item 解析。
- 重复的 reply payload 生成。
- 重复的 cooldown/probability 逻辑。
- 重复的 chat_id 计算。
- 重复的 store bind/load/save 逻辑。
- 重复的配置解析逻辑。
- 重复的错误处理 wrapper。
- 重复的群聊触发和判断逻辑。
- 重复的 prompt/context 构造逻辑。
- 旧版 schema 或旧数据格式兼容是否还必要。
- 旧版命令别名是否还必要。
- 旧版媒体字段兼容是否还必要。
- 不再需要的 debug 代码、print、console 风格日志。
- 不再需要的测试 fixture。
- 永远不会执行的分支。
- 永远不会命中的异常处理。
- 返回后不可达代码。
- 已不再注册或不会触发的后台任务。
- 文档中已废弃的示例。
- 旧目录、旧文件、旧模型名引用。
- 已经被统一实现替代的旧工具函数。

建议使用但不限于以下方式辅助检查：

- `rg` / `grep` 搜索候选函数、类、API、配置字段、目录路径。
- `git diff` 对照最近改动。
- Python lint/typecheck/unused import 检查。
- pytest 覆盖率。
- 手动检查动态入口、插件生命周期、dispatcher、smalltalk provider、后台任务，避免误删动态调用。
- 检查 docs、plugin.json、config example 是否还引用旧字段。
- 检查测试是否覆盖关键动态入口。

处理要求：

1. 对每个候选冗余项记录证据。
2. 能安全删除的直接删除。
3. 重复逻辑能安全合并的尽量合并。
4. 暂时不能确定是否无用的，不要删除，但在报告中标为风险。
5. 删除或合并后必须运行相关测试。
6. 如果删除的是命令、别名、provider、media、memory、store 或配置兼容逻辑，必须做完整回归。
7. 如果删除的是旧媒体字段兼容逻辑，必须确认当前 OneBot/NapCat 事件不依赖它。
8. 如果删除的是测试，必须确认不是删除了唯一覆盖关键行为的测试。
9. 如果合并的是统计/频率/概率逻辑，必须确认群聊触发行为不变。
10. 如果合并的是 media 逻辑，必须确认 text/image/face/mface 都不回退。
11. 如果合并的是 memory 逻辑，必须确认持久化、清空、重启和 chat_id 隔离不变。
12. 如果合并的是 provider 逻辑，必须确认真实 LLM、真实 vision、mock LLM、mock vision 和 provider 切换仍可用。
13. 如果合并的是群聊上下文构造逻辑，必须重新跑真实 LLM 多人群聊 transcript 测试。

报告中必须有一节：

`冗余代码、死代码和重复逻辑检查结果`

并列出：

- 删除了哪些代码。
- 合并了哪些重复逻辑。
- 哪些候选冗余代码暂时保留以及原因。
- 哪些看似冗余但实际被动态调用。
- 删除后执行了哪些回归测试。
- 是否还存在需要后续人工确认的风险。

## 子任务 Q：现有测试审查和新增自动化测试

请完整检查现有测试。如果某些测试文件不存在，请在报告中说明，并以实际仓库结构为准。

重点检查：

- `tests/plugins/test_xiaoqing_chat.py`
- `tests/plugins/test_xiaoqing_chat_media.py`
- `tests/plugins/test_xiaoqing_chat_dedup_json_llm.py`
- `tests/plugins/test_xiaoqing_chat_expression_store_persistence.py`
- `tests/plugins/test_xiaoqing_chat_memory_save_dedup.py`
- `tests/plugins/test_xiaoqing_chat_refactor_cleanup.py`
- `tests/plugins/test_xiaoqing_chat_review_regressions.py`
- `tests/plugins/test_xiaoqing_chat_store_base_helpers.py`
- `tests/plugins/test_xiaoqing_chat_task_scheduler_dedup.py`
- `tests/plugins/test_reply_checker.py`
- `tests/plugins/test_xiaoqing_prompt_builder.py`
- `tests/plugins/test_xiaoqing_reply_payload.py`

必须判断：

- 哪些测试有效。
- 哪些测试过浅。
- 哪些测试只测 helper 没测真实入口。
- 哪些测试缺少断言。
- 哪些测试依赖随机。
- 哪些测试依赖真实外部网络。
- 哪些测试没有覆盖异常路径。
- 哪些测试名称和实际行为不一致。
- 哪些测试已经过时。
- 哪些核心行为没有测试。
- 是否有足够的真实 LLM 多人群聊剧本测试。
- 是否有足够的拟人感评分或 transcript 记录。
- mock/fake LLM 测试是否只用于链路和异常，而没有错误地代替真实拟人效果判断。

请新增或更新测试，至少覆盖：

1. `/xc` 命令 golden tests。
2. plugin.json help 示例测试。
3. main.py `_show_help()` 示例测试。
4. smalltalk provider 入口测试。
5. 私聊/群聊触发规则测试。
6. 真实 LLM 多人群聊剧本测试。
7. 真实 LLM 群聊 transcript 生成测试。
8. 真实 LLM 拟人感评分记录。
9. text/image/face/mface 混合消息测试。
10. fake LLM provider 异常路径测试。
11. fake vision provider 异常路径测试。
12. 回复 payload segment 测试。
13. memory append/dedup/clear/persist 测试。
14. PFC/reply_checker/replan 测试。
15. deep brain chat 配置测试。
16. provider 权限和切换测试。
17. prompt injection 和 secret 泄露防护测试。
18. media SSRF/path traversal 测试。
19. 并发和 shutdown flush 测试。
20. 冗余代码清理后的回归测试。

## 测试数据要求

请为每个测试任务使用唯一前缀，例如：

- `TEST_XC_CMD_...`
- `TEST_XC_TEXT_...`
- `TEST_XC_GROUP_...`
- `TEST_XC_PRIVATE_...`
- `TEST_XC_GROUPCHAT_...`
- `TEST_XC_PERSONA_...`
- `TEST_XC_REAL_LLM_...`
- `TEST_XC_MEDIA_...`
- `TEST_XC_IMAGE_...`
- `TEST_XC_FACE_...`
- `TEST_XC_MFACE_...`
- `TEST_XC_MEMORY_...`
- `TEST_XC_EXPR_...`
- `TEST_XC_JARGON_...`
- `TEST_XC_PFC_...`
- `TEST_XC_BRAIN_...`
- `TEST_XC_PROVIDER_...`
- `TEST_XC_SECURITY_...`
- `TEST_XC_CONCURRENCY_...`
- `TEST_XC_DOC_...`
- `TEST_XC_DEADCODE_...`

如果测试会写数据，请优先使用临时 data_dir，不要污染真实 `plugins/xiaoqing_chat` 数据目录。需要测试真实目录行为时，必须先备份再操作。

真实 LLM 群聊测试输入请使用虚构用户、虚构群聊和虚构消息，不要使用真实隐私、真实群聊记录、真实用户身份或真实敏感信息。

## 自动化测试要求

请尽量新增或完善自动化测试，不要只手工验证。

建议新增测试类型：

1. 真实 LLM 多人群聊剧本测试。
2. 真实 LLM 群聊 transcript 生成测试。
3. 真实 LLM 拟人感评分记录。
4. 命令层 golden tests。
5. OneBot 事件 fixture tests。
6. fake LLM provider 异常路径 tests。
7. fake vision provider 异常路径 tests。
8. 多模态 message segment tests。
9. media 安全 tests。
10. memory/store persistence tests。
11. PFC/reply_checker/replan tests。
12. deep brain chat tests。
13. provider/admin 权限 tests。
14. background task/shutdown tests。
15. 文档示例 tests。
16. 冗余代码清理后的回归 tests。
17. 数据隔离 invariant tests。
18. 并发和重复消息 tests。

至少建立以下 invariant 检查：

- 同一 message_id 不应重复写入 user memory。
- 不同 chat_id 的上下文不应互相污染。
- 私聊记忆不应出现在群聊检索结果中。
- A 群记忆不应出现在 B 群检索结果中。
- `/xc 清空` 只清当前 chat_id。
- 被 reply_checker 拒绝的回复不应写入 assistant memory。
- LLM/vision 失败不应导致主进程崩溃。
- media 下载失败不应阻塞文本回复。
- path traversal 不应写出 data_dir。
- dangerous URL 不应被请求，或者必须被显式拒绝。
- secrets 不应出现在用户回复、日志、异常、报告中。
- shutdown 后 memory/action_history 应 flush。
- 删除冗余代码后 `/xc` 所有子命令仍可用。
- 删除冗余代码后 text/image/face/mface 行为不回退。
- 合并重复逻辑后群聊触发概率和冷却行为不回退。
- 文档中的 `/xc` 示例不能明显失效。
- 群聊中小青不应对每条普通消息都回复。
- 群聊中小青不应长期沉默到无法被 @ 或点名触发。
- 群聊中小青不应把 A 用户的话当成 B 用户的话。
- 群聊中小青不应跨群引用记忆。
- 群聊中小青不应把私聊记忆带到群里。
- 群聊中小青不应输出系统提示词、secrets、内部路径。
- 群聊中小青不应因为媒体消息处理失败而崩溃。
- mock/fake LLM 测试结果不能被报告为真实拟人效果通过。

## 修复要求

发现问题后，不要只记录问题。请执行以下闭环：

1. 定位根因。
2. 给出最小必要修复。
3. 添加或更新自动化测试，防止回归。
4. 重新运行相关测试。
5. 必要时运行完整回归。
6. 在报告中记录：
   - 问题描述。
   - 影响范围。
   - 复现步骤。
   - 根因。
   - 修复方案。
   - 修改文件。
   - 回归结果。

修复时请避免无关的大规模重写。所有代码改动都要能通过 `git diff` 清楚解释。

特别注意：

- `/xc` help 与实际行为不一致时，要么修帮助，要么修解析逻辑。
- plugin.json help 与 main.py help 不一致时，要统一。
- README/docs 与当前代码不一致时，要更新。
- secrets/config 示例不符合代码时，要更新。
- 核心群聊拟人效果必须用真实已配置 LLM 验证。
- mock 测试能覆盖异常路径，但不能替代真实 LLM 群聊效果测试。
- 媒体安全问题不能只记录，必须尽力修复。
- secret 泄露风险必须修复。
- 数据隔离问题必须修复。
- 群聊中跨用户、跨群、跨私聊的记忆串用必须修复。
- 群聊中明显过度抢话或长时间漏回必须分析并尽力修复。
- 群聊拟人感差不能简单归因于“模型问题”，要检查 prompt、context、memory、trigger、reply_checker、PFC 和配置。
- 并发导致数据损坏必须修复。
- 清理冗余代码时不能只依赖静态搜索结果。
- 删除任何看似无用的函数、文件、配置字段、兼容逻辑前，必须确认没有真实入口依赖。
- 能合并的重复逻辑尽量合并，但不要为了去重引入过度抽象。
- 如果某段旧兼容逻辑仍被 OneBot/NapCat 事件或历史数据依赖，即使看似冗余也不能删除。
- 如果某个文件只被 smalltalk provider、dispatcher、后台任务或 shutdown 使用，不能因为 `/xc` 命令不直接引用就删除。

## 优先级定义

请按以下优先级分类问题：

- P0：数据损坏、严重 secret 泄露、严重安全漏洞、服务不可用、核心聊天完全失败、群聊/私聊记忆串库、跨群泄露私聊记忆。
- P1：核心命令失败、smalltalk provider 失败、多人群聊核心参与失败、多模态核心功能失败、LLM/vision 异常导致崩溃、权限绕过、媒体路径穿越/SSRF、shutdown 丢数据、HELP 严重误导、严重过度抢话或严重漏回。
- P2：边界条件错误、错误提示不清晰、部分配置异常、部分媒体类型异常、非核心流程问题、文档示例不完整、明显冗余代码或重复逻辑、群聊拟人感明显不足但不导致核心功能失败。
- P3：体验问题、文案问题、轻微日志问题、低风险可读性或维护性问题、个别回复略显机械。

P0/P1 必须尽力修复并回归。P2 尽量修复。P3 可以记录建议，但如果容易修复也请直接修复。

## 需要执行的检查命令

请根据项目实际情况执行合适的命令，包括但不限于：

- `git status`
- `git diff`
- `python -m pytest tests/plugins/test_xiaoqing_chat.py`
- `python -m pytest tests/plugins/test_xiaoqing_chat_media.py`
- `python -m pytest tests/plugins/test_reply_checker.py`
- `python -m pytest tests/plugins/test_xiaoqing_prompt_builder.py`
- `python -m pytest tests/plugins/test_xiaoqing_reply_payload.py`
- `python -m pytest tests/plugins -k xiaoqing`
- `python -m pytest tests -k "xiaoqing or reply_checker"`
- 项目已有的完整测试命令。
- lint/typecheck 命令。
- ruff/black 检查，如果项目支持。
- 新增的 `/xc` 命令测试脚本。
- 新增的 OneBot 入站测试脚本。
- 新增的真实 LLM 多人群聊剧本测试脚本。
- 新增的真实 LLM 群聊 transcript 生成脚本。
- 新增的真实 LLM 群聊拟人感评分脚本或人工评分记录。
- 新增的 mock/fake LLM 异常路径测试。
- 新增的 media 安全测试。
- 新增的 memory persistence 测试。
- 新增的并发测试。
- 新增的 shutdown 测试。
- 新增的文档示例测试。
- 冗余代码和死代码检查命令。
- `rg` / `grep` 搜索候选函数、类、配置字段、路径、命令别名是否仍被引用。
- 删除冗余代码后的完整回归测试命令。

如果某个命令失败，请判断是环境问题、已有问题还是本次发现的问题，并在报告中说明。

## 最终交付物

请在仓库中生成一份详细测试报告，建议路径：

`plugins/xiaoqing_chat/test_reports/xiaoqing-chat-full-test-report-20260430.md`

同时保存群聊 transcript，建议路径：

`plugins/xiaoqing_chat/test_reports/group_chat_transcripts/`

报告必须包含以下内容：

1. 执行摘要。
2. 测试环境。
3. 真实 LLM provider/model/config 摘要，不含 secrets。
4. 真实 LLM 与 mock/fake LLM 测试边界说明。
5. 代码阅读总结。
6. 功能地图。
7. 插件生命周期和入口地图。
8. `/xc` 命令地图。
9. smalltalk provider 调用路径。
10. 群聊/私聊触发规则。
11. 多人群聊自然互动测试设计。
12. 真实 LLM 群聊拟人效果测试结果。
13. 多人群聊自然互动测试结果。
14. 群聊拟人感评分汇总。
15. 群聊 transcript 列表。
16. 真实 LLM transcript 中的成功案例、失败案例和评分依据。
17. 最自然的 10 条回复分析。
18. 最不自然的 10 条回复分析。
19. 过度插话和漏回复案例分析。
20. 多人上下文、用户识别和话题跟踪测试结果。
21. 图片、face、mface 在群聊中的自然处理结果。
22. 记忆在群聊中的自然使用和串群风险检查结果。
23. 群聊拟人效果相关修复和回归结果。
24. OneBot 事件和 message segment 地图。
25. LLM provider 和 vision provider 地图。
26. memory/store/persistence 地图。
27. PFC/reply_checker/action_history 地图。
28. media 入站/出站链路地图。
29. 配置和 secrets 审查结果。
30. 文档/help/plugin.json 审查结果。
31. 测试方法。
32. 测试覆盖矩阵。
33. `/xc` 命令测试结果。
34. 文本聊天测试结果。
35. 群聊/私聊触发测试结果。
36. 多模态入站媒体测试结果。
37. 出站图片/表情/face 回复测试结果。
38. 记忆和持久化测试结果。
39. PFC/目标/心流/回复检查测试结果。
40. 深度对话模式测试结果。
41. 配置、secrets、provider、热重载测试结果。
42. HTTP/OneBot 入站集成测试结果。
43. 安全性测试结果。
44. 并发、异步锁、后台任务、shutdown 测试结果。
45. 错误处理、日志和用户反馈测试结果。
46. 现有测试审查结果。
47. 新增或修改的自动化测试列表。
48. mock/fake LLM 链路和异常测试结果。
49. 冗余代码、死代码和重复逻辑检查结果。
50. 删除或合并的冗余代码列表。
51. 暂时保留的疑似冗余代码及原因。
52. 冗余代码清理后的回归测试结果。
53. 发现的问题列表，按 P0/P1/P2/P3 分类。
54. 每个问题的复现步骤。
55. 每个问题的根因分析。
56. 修复情况。
57. 回归测试结果。
58. 仍未解决的问题或风险。
59. 建议后续补充的自动化测试。
60. 本次新增或修改的测试文件列表。
61. 本次代码修复和冗余代码清理的 `git diff` 摘要。
62. 最终结论：是否可以认为 `xiaoqing_chat` 已通过本轮完整测试，尤其是否可以认为小青在真实 LLM 群聊 transcript 中已经像自然群友，而不是客服型机器人。

缺陷表建议使用这个格式：

| ID | 优先级 | 模块 | 问题描述 | 复现步骤 | 预期结果 | 实际结果 | 根因 | 修复文件 | 回归结果 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|

覆盖矩阵建议使用这个格式：

| 模块 | 功能 | `/xc` 命令覆盖 | smalltalk 覆盖 | OneBot 覆盖 | 真实 LLM 覆盖 | mock 异常覆盖 | 单元测试 | 集成测试 | 异常输入 | 安全测试 | 并发测试 | 回归测试 | 结果 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

命令测试表建议使用这个格式：

| 命令 | 测试输入 | 事件类型 | 预期结果 | 实际结果 | 状态 | 备注 |
|---|---|---|---|---|---|---|

真实 LLM 配置摘要表建议使用这个格式：

| 项目 | 值 | 是否脱敏 | 备注 |
|---|---|---|---|

真实 LLM 群聊剧本测试表建议使用这个格式：

| 场景 | group_id | 用户数 | 消息轮数 | 真实 LLM provider/model | 小青回复数 | 沉默数 | 过度回复 | 漏回复 | 平均拟人分 | 主要问题 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|

群聊单轮 transcript 表建议使用这个格式：

| 轮次 | user_id | nickname/card | message segments | raw_message | 触发原因 | 是否回复 | 小青真实回复 | 不回复原因 | 记忆写入 | PFC/检查结果 | 拟人评分 | 问题标注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

拟人感评分表建议使用这个格式：

| 场景 | 轮次 | 真实回复 | 触发合理性 | 上下文理解 | 对象识别 | 语气自然度 | 人设一致性 | 情绪匹配 | 接梗能力 | 边界感 | 多模态理解 | 记忆使用 | 回复长度 | 安全性 | 平均分 | 问题 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

多模态测试表建议使用这个格式：

| 类型 | OneBot segment | 场景 | 真实/Mock | 预期处理 | 实际处理 | 记忆校验 | 回复校验 | 安全校验 | 状态 |
|---|---|---|---|---|---|---|---|---|---|

记忆一致性表建议使用这个格式：

| 场景 | chat_id | user_id | 操作 | memory_store | memory_db | action_history | 持久化 | 状态 |
|---|---|---|---|---|---|---|---|---|

Provider 测试表建议使用这个格式：

| Provider 类型 | 配置 | 输入 | 真实/Mock | 输出/异常 | 预期结果 | 实际结果 | 是否泄露 secrets | 状态 |
|---|---|---|---|---|---|---|---|---|

安全测试表建议使用这个格式：

| ID | 攻击类型 | 输入 | 入口 | 真实/Mock | 预期防护 | 实际结果 | 修复情况 | 回归结果 | 状态 |
|---|---|---|---|---|---|---|---|---|---|

群聊拟人问题表建议使用这个格式：

| ID | 类型 | 场景 | 轮次 | 输入消息 | 触发原因 | 真实回复 | 为什么不自然 | 预期更自然行为 | 根因 | 修复方案 | 回归结果 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

冗余代码检查表建议使用这个格式：

| ID | 文件 | 类型 | 候选冗余内容 | 判断依据 | 处理方式 | 修改文件 | 回归测试 | 状态 | 备注 |
|---|---|---|---|---|---|---|---|---|---|

重复逻辑检查表建议使用这个格式：

| ID | 重复逻辑 | 涉及文件 | 风险 | 合并方案 | 是否已合并 | 回归测试 | 状态 |
|---|---|---|---|---|---|---|---|

动态入口检查表建议使用这个格式：

| ID | 入口类型 | 入口名称 | 目标函数/文件 | 静态搜索结果 | 动态调用证据 | 是否可删除 | 结论 |
|---|---|---|---|---|---|---|---|

## 最终回复要求

完成后，请在最终回复中给出：

1. 测试报告文件路径。
2. 群聊 transcript 保存路径。
3. 本轮群聊拟人效果测试使用的真实 provider/model 是什么。
4. 真实 LLM 群聊 transcript 是否已保存。
5. mock/fake LLM 只覆盖了哪些异常或回归测试。
6. 是否存在因为真实 LLM 配置、网络或 provider 问题导致无法完成的核心拟人测试。
7. 总共执行了多少类测试、多少条用例、多少个 `/xc` 命令、多少个 OneBot 事件、多少个 HTTP 请求。
8. 多人群聊场景一共执行了多少个剧本、多少轮消息、多少名模拟用户、多少个 group_id。
9. 小青实际回复了多少次，沉默了多少次，过度回复和漏回复各多少次。
10. 群聊拟人感各维度平均分。
11. 文本、图片、face、mface、混合消息分别是否通过。
12. 群聊和私聊触发规则是否通过。
13. 最严重的群聊拟人问题是什么，是否已修复。
14. 是否认为小青在真实 LLM 群聊 transcript 中已经像自然群友，而不是客服型机器人。
15. 记忆、表达、黑话、PFC、回复检查、深度对话是否通过。
16. 配置、secrets、provider、vision provider 是否通过。
17. 发现了多少个问题，按 P0/P1/P2/P3 分类。
18. 修复了多少个问题。
19. 新增或修改了哪些测试。
20. 检查出多少处冗余代码、死代码或重复逻辑。
21. 删除了多少处，合并了多少处，保留了多少处以及原因。
22. 安全测试是否通过，是否仍有未解决风险。
23. 关键测试命令和回归结果。
24. 当前 `git status` 摘要。
25. 是否建议合并当前版本。

请真实记录结果，不要把未执行的测试标记为已通过。对于无法执行的测试，请说明原因、影响和替代验证方式。对于疑似冗余但无法确认无用的代码，不要删除，请说明保留原因和后续建议。对于群聊拟人效果，必须以真实已配置 LLM 的 transcript 和评分为准；mock/fake LLM 测试只能说明链路、异常处理和回归稳定性，不能作为“真实拟人效果通过”的依据。
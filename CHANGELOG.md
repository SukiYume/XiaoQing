# XiaoQingBot 更新记录

本文件记录 XiaoQingBot 的项目更新。它面向维护者和未来的自己，重点写清楚每次更新改了什么、为什么改、影响哪些模块，以及已经做过哪些验证。

维护时把最新内容写在最上方。日期使用北京时间，格式为 `YYYY-MM-DD`。如果一组改动还没有提交，先放在“未发布”中；提交或发版后，再移动到对应日期下面。

## 2026-05-10

### Codex 后台任务插件

- 新增 `plugins/codex`，通过 `/codex create <name> [cwd:<path>]` 创建 Codex 会话标签，通过 `/codex <name> <任务>` 向指定会话投递任务。
- 将 Codex 任务设计为插件内部独立队列，不占用 XiaoQing 的框架 Session；同一标签内串行执行，不同标签可按 `max_parallel_jobs` 并行执行，完成后主动回发 `[codex:<label> #<job_id>]` 结果。
- 支持 `/codex list`、`/codex status`、`/codex cancel`/`stop`、`/codex clear` 和 `/codex delete`，并支持创建会话时指定工作目录。
- 新增 Codex 路径归一化和允许目录校验。默认工作目录为 `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex`，用户可在 QQ 中统一使用 `/` 斜杠输入路径。
- 将 Codex 会话索引和每个标签的对话 JSONL 保存到 `plugins/codex/data/`，运行时数据继续由 `.gitignore` 排除。

### Shell 路径解析改进

- 改进 `plugins/shell` 的参数拆分和路径识别，Windows 下不再把反斜杠转义吃掉。
- 支持用户在 QQ 中统一输入 `/` 斜杠路径，由插件按 bot 所在系统归一化；同时避免把 URL、`cmd /c`、`xcopy /Y` 这类参数误判为路径。
- 明确 Windows 的 `copy`、`del`、`type` 等 shell 内建命令不能直接执行，需要使用 `cmd /c copy` 或外部命令 `cp`、`xcopy`、`robocopy`。

### Pendo 账本录入

- 调整 `/pendo ledger add`：无参数时进入交互式记账，金额和描述先手动输入，交易类型、账户、分类等后续步骤可用数字选择。
- 保留 `/pendo ledger add <金额> <描述> ...` 的单行快捷记账方式，并继续支持 `cat:`、`in/out/transfer`、`account:`、`to:`、`merchant:`、`date:` 和 `remark:` 等参数。
- 补充 Pendo ledger 和主入口测试，覆盖交互步骤、默认值、转账校验和快捷录入。

### 文档同步

- 更新根 README、`docs/00-09`、`docs/README.md`、Pendo README、Shell README 和 Codex README，让项目手册描述当前项目结构、命令、配置、后台队列和路径规则。
- 补充 `PluginContext.send_action()`、后台任务队列、Codex 配置、Shell 配置、Pendo ledger 交互记账和插件统计说明。
- 检查 README 和 `docs/*.md` 的文档口吻，保持为项目说明和使用手册；更新流水账只保留在本 CHANGELOG 中。

### 验证

- 已执行 `git diff --check`。
- 已执行 `conda activate normal; python -m pytest tests/plugins/test_codex.py tests/plugins/test_shell_plugin.py tests/plugins/test_pendo.py -q`，结果为 `180 passed`。

## 2026-05-08

### Pendo Web 端口整理

- 将 Pendo Web 默认端口从 `8765` 调整为 `12001`，避开 Windows TCP excluded port range 导致的 `WinError 10013` 绑定失败。
- 统一本地端口说明：OneBot/NapCat 保持 `11000`/`11001`，XiaoQing inbound 使用 `12000`，Pendo Web 使用相邻的 `12001`，备用示例使用 `12002`/`12003`。
- 更新 README、Pendo 文档、配置文档、Scriptable 小组件说明、nginx 反向代理示例和 Pendo 测试说明中的默认地址。
- 已在生产 Windows 主机上验证 `127.0.0.1:12001` 和 `127.0.0.1:12003` 可绑定；已执行 `pytest tests/test_config.py tests/test_server.py tests/plugins/test_pendo.py tests/plugins/test_pendo_web_widget.py` 验证相关配置、inbound server 和 Pendo 回归覆盖。

## 2026-05-07

### xiaoqing_chat 拟人对话自然度改进

- 新增 `humanize` 配置，支持按输入阅读量和输出长度计算拟人化打字等待，并在多段回复之间加入可配置的间隔。
- 调整人格状态刷新策略，让当前 mood 在有效期内保持稳定，长时间空闲后重新抽取，减少每轮都换状态导致的语气漂移。
- 收紧 prompt 和 reply checker 的语气约束，降低“哈哈”“笑死”“啊这”等填充开头的连续复用概率，并要求不熟悉话题时承认不确定或追问，避免关键词拼接。
- 改进表情包注入逻辑，允许高频学习到的未审核表达按配置阈值自动进入上下文，同时继续保留低频表达的审核门槛。
- 重写媒体分析提示词，让图片/表情包描述先保持客观；新增可选的梗背景提取、缓存和上下文标记，用于降低只读到图中文字就强行接梗的情况。
- 补充 reply checker、表达注入、拟人打字延迟和 mood 生命周期测试；已执行 `python -m compileall -q plugins/xiaoqing_chat`、`git diff --check`、`python -m pytest tests/plugins -k "xiaoqing or reply_checker" -q` 验证。

## 2026-05-03

### Pendo Web 待办与账本界面修复

- 修复 Pendo Web 待办概览中“今天与滞后”任务最多只返回 6 条的问题，让今天和已滞后的待办完整展示。
- 调整待办列表页的展示方式，减少多层圆角容器嵌套，改用更扁平的分区和行式任务列表。
- 优化 Pendo Web 账本页的快速记账和筛选范围表单布局，统一字段网格、标签和金额筛选输入框宽度。
- 已执行 `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_web_widget.py -q` 验证相关 Web 回归覆盖。

## 2026-05-01 (v4.1.0)

### 文档体系整理

- 重写根目录 `README.md`，把 XiaoQingBot 描述为可长期运行的 QQ 机器人项目，并补充核心能力、代码结构、消息处理概览、部署、插件、测试和排障入口。
- 重写 `docs/README.md`，把 `docs/00-09` 的阅读顺序、目标读者、插件文档入口和维护约定整理成清晰目录。
- 系统更新 `docs/00-overview.md` 到 `docs/09-plugins.md`，让这些文档成为当前项目状态的项目手册。文档覆盖项目定位、快速开始、系统架构、插件开发、核心模块、API、配置、高级用法、消息链路和内置插件。
- 调整文档语气，减少流水账、问句、夸张表达和硬邦邦的说明。保留技术准确性，同时让阅读体验更自然。
- 更新 `docs/pendo-scriptable-widget.md`，说明 Pendo iPhone Scriptable 小组件的只读接口、配置方式和日常使用场景。

### Pendo 文档与测试资料整理

- 为 `plugins/pendo` 保留插件级 `README.md` 和 `ARCHITECTURE.md` 两个入口。README 面向日常使用和部署，ARCHITECTURE 面向维护者理解数据模型、命令、Web API、调度和导入导出。
- 删除与 README 高度重复的 `plugins/pendo/Pendo个人时间与信息管理中枢.md`，避免同一个插件出现多份权威说明。
- 调整 `plugins/pendo/pendo测试.md`，让它成为稳定的插件测试 prompt。内容聚焦完整测试目标、状态恢复、case 矩阵、报告写法和验证要求，避免写成历史更新记录。
- 清理 Pendo test reports，只保留 final report、关键截图、迁移样例、完整测试脚本和有复盘价值的报告。中间过程文件和一次性结果文件已移出版本控制。

### xiaoqing_chat 文档与测试资料整理

- 为 `plugins/xiaoqing_chat` 新建插件级 `README.md` 和 `ARCHITECTURE.md`。README 说明拟人聊天、多模态收发、attention gate、频控、记忆、planner、reply checker、表情包和测试命令。ARCHITECTURE 说明主链路、上下文构建、should_reply、planner、回复生成、多模态发送、记忆和实验框架。
- 调整 `plugins/xiaoqing_chat/xiaoqing_chat测试.md`，让它成为完备测试 prompt。测试要求先读取或创建 `CURRENT_RUN_ID`，再从 `xiaoqing-test-results.jsonl` 重建进度，并按 case_id 矩阵继续执行。
- 把最新拟人测试框架写入 xiaoqing_chat 测试 prompt。测试覆盖大群聊天、文本、QQ face、NapCat mface、图片、表情包、reply 引用、上下文共指、频控、planner、reply checker 和多轮对话。
- 保留 xiaoqing_chat 中有价值的 test reports，例如 3000 轮对话信息、最终分析报告和关键数据。清理中间状态文件，减少仓库噪声。

### xiaoqing_chat 回复决策改进

- 改进 should_reply 主路径，把 `@`、bot name、reply-to-bot、上下文共指和群聊插话统一放到 attention gate 中处理。
- 修复“她”“不@她”等指代小青的场景严重漏回的问题。共指识别从纯显式 mention 扩展到最近上下文和 bot 相关代词。
- 清理 `heartflow.weight_mentioned` 等在主回复路径里不再参与决策的重复权重逻辑，减少 planner、frequency control 和 attention gate 之间的职责重叠。
- 调整频率限制设计，让基础冷却、群聊随机插话、强触发和上下文触发分层执行。强触发不再被普通插话概率错误压制。
- 补充 xiaoqing_chat 单元测试和拟人实验测试，覆盖 attention gate、频控、多模态消息接收、媒体回复和上下文触发。

### 仓库维护

- 整理 `.gitignore`，忽略插件实验输出、测试中间产物、运行日志、缓存文件和本地状态文件。
- 检查 `plugins/xiaoqing_chat/experiments` 的归档价值。实验代码和可复现脚本保留，批量输出和临时数据进入忽略规则。
- 新增根目录 `CHANGELOG.md`，作为项目级更新记录入口，后续维护时按日期记录每次改动的内容、影响范围和验证结果。

### Pendo 账本录入简化

- 简化 Pendo 账本添加流程，减少日常记账时需要输入的参数。
- 调整账本 handler、主入口和相关文档，保证新流程在聊天命令和插件说明中保持一致。
- 扩展 `tests/plugins/test_pendo.py`，覆盖账本录入的常见参数组合和回归场景。

### Pendo 文档刷新

- 刷新 Pendo README、架构说明、项目 README、docs 目录和插件清单中的 Pendo 相关说明。
- 梳理 Pendo 数据模型、Web 控制台、Scriptable 小组件、迁移、备份和排障信息。
- 减少重复文档内容，让插件说明更集中地落在 README 和 ARCHITECTURE 中。

### Pendo 旧提醒兼容修复

- 修复旧提醒数据中带时区时间的兼容问题。
- 在 `plugins/pendo/utils/validators.py` 中处理 timezone-aware legacy reminders，避免迁移和提醒加载时发生时间解析错误。
- 新增 `tests/plugins/test_pendo_event_migration.py` 覆盖旧提醒迁移路径。

### xiaoqing_chat 测试规范更新

- 大幅整理 `plugins/xiaoqing_chat/xiaoqing_chat测试.md`，把测试 prompt 从临时记录改成可复用的完整测试规程。
- 明确测试运行必须基于 `CURRENT_RUN_ID`、状态文件和 case_id 矩阵恢复，避免上下文压缩后重复执行或漏测。
- 加入多模态、拟人程度、群聊插话、planner 和 reply checker 的测试要求。

### 插件测试资料纳入版本控制

- 将 Pendo 和 xiaoqing_chat 的测试 prompt 纳入仓库，作为插件级测试入口。
- 更新 `.gitignore`，忽略测试报告中的批量中间文件。
- 删除大体积、一次性、难以复用的历史测试输出，让仓库保留更有用的最终报告和可复现脚本。

### Pendo redesign 覆盖扩展

- 扩展 Pendo redesign 的命令、Web、迁移和回归测试。
- 增加 dashboard、settings、transfer 等 Web 表面的覆盖。
- 修复 scheduled command、transfer bundle、settings API 和 dashboard overview 中发现的回归问题。

## 2026-04-30

### Pendo redesign 流程加固

- 加固 Pendo 事件、待办、笔记、日记、账本、搜索、设置、导入导出和 Web API 的核心流程。
- 调整 command router、handler、service、model、Web API 和前端页面，让 redesign 后的数据模型在聊天命令和 Web 控制台中保持一致。
- 增加多代理测试报告、黑盒测试脚本、浏览器截图、迁移样例和 pytest 覆盖。
- 修复 Windows PowerShell 下 pytest 通配符展开不一致的问题，并在 README 中补充对应命令写法。

### Pendo 测试输出忽略

- 更新 `.gitignore`，将 Pendo redesign 的测试中间报告、浏览器运行输出和生成数据排除出普通版本控制。
- 保留有复盘价值的 final report、脚本和关键截图，避免仓库被一次性测试文件淹没。

## 2026-04-29

### Pendo 事件引用和子节点校验

- 修复 Pendo note reference 编辑时引用信息丢失的问题。
- 校验 Pendo event collection children，避免事件树中出现非法子节点。
- 合并 pendo redesign 分支期间同步 master 上的修复。

### xiaoqing_chat 多模态回复改进

- 改进 xiaoqing_chat 的媒体回复链路，让主 LLM、prompt builder、reply checker 和 marker resolver 可以更稳定地处理图片、表情包和 QQ face 标记。
- 将 xiaoqing_chat 媒体资源存放到 data 目录，减少插件代码目录和运行数据混在一起的问题。
- 在收到 emoji 时保留可见文本，让 LLM 和 reply checker 能理解表情上下文。
- 修正文档中的媒体库路径，减少部署时的路径歧义。

## 2026-04-28

### xiaoqing_chat 主链路简化

- 重构 xiaoqing_chat 的回复链路，减少旧的媒体回复 helper 和重复 planner 逻辑。
- 引入统一的 media marker resolver，让图片、表情包、QQ face 和 mface 的发送标记由一个模块解析。
- 调整 frequency control、context builder、reply generator、reply checker、memory retrieval 和 runtime state，让主链路更清楚。
- 更新 README、docs 配置说明和高级用法，记录新的插件配置和多模态发送方式。
- 增加 xiaoqing_chat runtime hardening、review regression、prompt builder、media 和 reply checker 测试。

## 2026-04-25

### Pendo 笔记流程改进

- 改进 Pendo note workflow，优化笔记创建、引用、更新和展示的体验。
- 标注单个事件详情中的操作按钮，减少 Web 控制台中按钮语义不清的问题。
- 移除旧的 Pendo event fixture，让测试更贴近 redesign 后的数据模型。

### 核心与 LLM 兼容修复

- 修复 inbound manager status provider 的绑定问题，保证运行状态查询能拿到正确 provider。
- 透传 vision LLM extra payload，让多模态模型调用可以收到额外参数。
- 修复 xiaoqing planner 和 checker 的边界情况，减少规划和检查阶段的误判。

## 2026-04-24

### Pendo event graph 对齐

- 移除旧的 Pendo event leaf path，统一使用 redesign 后的 event graph 模型。
- 对齐 Pendo markdown export 与 event graph，保证导出结构能反映多节点事件关系。
- 对齐 Pendo Web 表面与 event graph，让 Web API 和前端页面围绕同一套事件模型工作。

### xiaoqing_chat 和 Pendo 小组件加固

- 改进 xiaoqing_chat 的回复流和模型处理，减少多模态上下文和模型调用之间的边界问题。
- 加固 Pendo Scriptable 小组件的数据拉取流程，降低移动端小组件读取失败时的影响。

## 2026-04-23

### Pendo 调度和提醒修复

- 改进 Pendo 调度任务与提醒发送逻辑，让提醒、日程和后台任务之间的行为更稳定。
- 收紧提醒状态更新和调度触发条件，减少重复提醒或确认语义不清的问题。

### xiaoqing_chat 媒体链路整理

- 精简 xiaoqing_chat 媒体处理流程，把入站媒体、上下文构建和发送标记之间的职责进一步拆清楚。
- 更新忽略规则，排除本地 worktree 目录，避免本地开发副本进入版本控制。

## 2026-04-22

### Pendo 里程碑提醒语义修复

- 在 Pendo 提醒中补充 milestone briefings，让复杂事件和阶段性安排能给出更有用的提醒摘要。
- 收窄确认语义，降低普通文本被错误识别为确认操作的概率。

## 2026-04-21

### v4.0.0 发布

- 发布 `v4.0.0`，将 xiaoqing_chat 多模态回复链路、Pendo Web 和文档整理后的状态作为新的主线版本。
- 调整面向最终用户的文档语气，让 README 和插件说明更接近日常使用场景。

### xiaoqing_chat 多模态回复加固

- 修复 xiaoqing_chat 多模态回复中的边界情况，提升图片、表情和文本混合消息下的稳定性。
- 将媒体片段与 OneBot emoji 传输方式对齐，减少发送端 marker 和实际消息段不一致的问题。
- 改进回复流和 emoji 修复流程，让 reply checker、媒体解析和最终发送之间的衔接更稳定。
- 更新 xiaoqing_chat 文档，记录新的媒体回复行为和修复流程。

### arxiv_filter 测试兼容

- 当运行环境缺少 pandas 时跳过 arxiv_filter 相关测试，避免可选训练依赖影响普通测试套件。

## 2026-04-20

### xiaoqing_chat 媒体回复管线重构

- 改进 xiaoqing_chat 媒体回复，让文字回复、图片回复、表情包和 QQ face 能在同一条回复管线中组合。
- 重构媒体回复 pipeline，减少分散 helper 带来的重复处理和状态不一致。
- 为后续 marker resolver、reply checker 和多模态上下文统一打基础。

## 2026-04-19

### xiaoqing_chat 图片对话与表情分析

- 增加 xiaoqing_chat 图片对话能力，让插件可以把图片内容纳入群聊理解和回复生成。
- 更新 xiaoqing_chat `0.2.0` 文档，说明图片对话、多模态配置和使用方式。
- 改进 emoji 图片分析、动画表情分析和媒体上下文处理，让表情类消息不只作为占位符进入上下文。

### Pendo 事件编辑和提醒改进

- 改进 Pendo 事件编辑和提醒行为，强化日程修改、提醒生成和提醒展示之间的一致性。
- 调整 watcher 默认值和插件配置行为，减少默认配置下的意外文件监听或热重载问题。

### 审计修复和文档整理

- 修复核心框架和多个插件中的审计发现，覆盖运行时、插件配置、媒体上下文和测试边界。
- 处理 review follow-up 和 remediation 项，恢复 review checklist，并把临时 review notes 从项目文档中移到本地。
- 更新近期 runtime 和插件变化对应的文档，避免文档落后于 xiaoqing_chat 与 Pendo 的实现。

## 2026-04-17

### Pendo 小组件和笔记标题解析

- 细化 Pendo Scriptable 小组件展示和同步行为。
- 改进 Pendo 笔记标题解析，让快速记录和后续检索更稳定。

## 2026-04-15

### Pendo 日历视图和小组件同步

- 优化 Pendo 日历视图，让事件浏览和时间范围展示更清楚。
- 调整 Scriptable 小组件同步逻辑，改善移动端摘要更新。
- 重构小组件背景处理，支持 iOS 原生动态深色模式切换和透明装饰。

## 2026-04-14

### Pendo Web 小组件和服务流程

- 改进 Pendo Web 小组件相关接口和 Web server 流程。
- 调整小组件读取路径和服务端响应，让 Scriptable 侧的数据获取更稳定。

## 2026-04-12

### Pendo Scriptable 小组件

- 增加 Pendo Scriptable widget 支持，把 Pendo 的日程、待办、提醒和摘要信息放到 iPhone 主屏。
- 为小组件准备只读接口和脚本入口，方便移动端查看当天信息。

## 2026-04-08

### Pendo Web 演示和日期范围

- 改进 Pendo Web demo access，让演示模式更容易启动和验证。
- 优化 Web 端日期范围处理，减少看板、统计和演示数据中的时间边界问题。
- 加固 demo import paths，避免不同运行目录下导入失败。

## 2026-04-06

### Pendo 笔记解析和多行输入

- 改进 Pendo 笔记解析与帮助文本，让创建笔记、补充内容和查询笔记的命令更清楚。
- 保留多行笔记输入，避免换行内容在聊天端被压扁或截断。

## 2026-04-03

### Pendo 提醒和账本交互

- 改进 Pendo 提醒消息结构，让提醒内容更容易阅读，也更方便后续确认或处理。
- 调整 Pendo 账本添加对话顺序，让交互式记账更接近日常输入习惯。

### xiaoqing_chat 群聊参与度

- 提高 xiaoqing_chat 在群聊中的参与度，让普通群聊插话更积极。
- 为后续 attention gate、频控和拟人程度测试提供更高的基础参与率。

## 2026-04-01

### Pendo Web Token 输入修复

- 修复 Pendo Web 粘贴 token 消息的识别问题。
- 允许用户从聊天端或其他来源复制 token 后直接粘贴使用，减少登录流程中的失败点。

## 2026-03-31

### Pendo Web 细节修复

- 统一 Pendo Web 图表范围和 token 投递行为，减少不同页面统计口径不一致的问题。
- 优化 Pendo Web 移动端 UI，让小屏幕下的导航、卡片和表单更可用。
- 改进 Web 搜索结果展示，提升笔记、事件、账本和待办混合查询时的可读性。

## 2026-03-30

### Pendo Web 过滤、统计和导出修复

- 对齐 Pendo Web filters 和 stats ranges，减少筛选条件和统计区间不一致的问题。
- 增强 Pendo exporter、日记分析和笔记 UI，提升数据导出、复盘和浏览体验。
- 支持笔记命令显式传入标题和正文，让聊天端创建结构化笔记更可靠。
- 修复 FastAPI 导入、任务显示和部分文档不一致问题。

### 测试和依赖稳定性

- 修复 dict 与 earthquake 插件的 pytest 稳定性问题。
- 在缺少 PyJWT 时跳过 Pendo Web auth 和 transfer 测试，避免可选 Web 依赖影响基础测试。
- 处理 scheduled shutdown cancellation 和 CI 依赖问题，让测试结束和后台任务清理更稳。

## 2026-03-29

### Pendo Web 传输和分析增强

- 扩展 Pendo Web transfer 能力，改进数据导入、导出和迁移流程。
- 增强 Pendo analytics，让 dashboard 和统计页面能提供更有用的概览。
- 修复调度关闭取消和 CI 依赖相关问题，降低测试环境中的偶发失败。

## 2026-03-28

### Pendo 命令保护和日记心情

- 细化 Pendo 命令 guard，减少错误参数或不完整输入进入业务处理的概率。
- 优化日记心情 UI，让日记记录和展示更直观。

## 2026-03-27

### Pendo Web UI V3.0 合并

- 合并 Pendo Web UI console，形成包含 dashboard、events、tasks、ledger、notes、diary、search、stats 和 settings 的 Web 页面套件。
- 增加 PyJWT、FastAPI、uvicorn、passlib 等 Web UI 依赖。
- 修复子路径 nginx 部署下的 API 前缀和相对路径问题，让 `/pendo` 子路径部署可用。
- 加固 Pendo event reminders 和 edits，减少 Web UI 与聊天命令同时操作事件时的状态问题。

### 文档 V3.3.0 刷新

- 更新根 README、`docs/00-overview.md` 和 `docs/09-plugins.md`，突出 Pendo Web UI 能力。
- 对全量文档做 V3.3.0 级别刷新，覆盖架构、插件、部署和使用说明。
- 清理过期的 docs/plans 与 docs/superpowers，并把 pytest 临时目录集中到 `.pytest_cache`。
- 对 README 和 docs 做视觉风格刷新，补充徽章、提示块和更清晰的入口说明。

## 2026-03-26

### Pendo Web UI 视觉重构

- 重新设计 Pendo Web UI，统一页面布局、导航、模块颜色和交互视觉。
- 为后续移动端 polish、过滤栏和页面组件一致性修复打基础。

## 2026-03-25

### Pendo Web UI 从零搭建

- 增加 Pendo Web 配置和 Web 包结构，建立 FastAPI server、依赖注入、API router 和静态资源入口。
- 增加 JWT auth 模块和对应测试，提供 Web 控制台登录基础。
- 增加 auth、items CRUD、dashboard、search、stats、settings、config 等 API endpoint。
- 增加聊天命令入口，用于管理和打开 Pendo Web UI。
- 增加 HTML shell、CSS 样式、JS router、API client、store 和 app bootstrap。

### Pendo Web 页面套件

- 增加 dashboard、events calendar、tasks kanban、ledger quick-add、notes card grid、diary timeline、search、stats 和 settings 页面。
- 增加共享 UI 组件和 Chart.js loader，并在 dashboard 和 stats 中复用图表加载逻辑。
- 重做 select 元素和 filter-bar 视觉，让筛选控件更明显、更一致。
- 重构 ledger 页面体验，修复排序和分页问题。

### Pendo Web 修复和文档计划

- 修复 API 错误处理、chart-loader 路径、Web help、API response format、FAB menu 和多处 UI 数据问题。
- 增加 Pendo Web UI 设计 spec、review 修订和 20 项实现计划。
- 改进 Pendo 列表命令的字段过滤，账本日期范围解析委托给通用时间解析逻辑。

## 2026-03-24

### Pendo 账本能力落地

- 增加 `LedgerItem` 数据模型、字段常量、数据库列、账本类型图标和格式化名称。
- 增加账本配置、分类、会话类型、`LedgerHandler`、CRUD、交互式添加和汇总能力。
- 接入账本 session routing、命令路由和 help text，让聊天端可以完成记账、查询、编辑和统计。
- 增加旧账单 `old_bill.csv` 一次性导入脚本，并移动到 `plugins/pendo/scripts`。
- 将账本配置中的“美容”分类改为“服务”，并同步导入脚本。

### Pendo 路由、日记和搜索优化

- 将账本编辑命令改为显式字段语法，降低误解析风险。
- 移除账本 payment method 字段，减少早期模型中的冗余属性。
- 用数据驱动的 `COMMAND_META` 简化 Pendo router。
- 改进日记命令体验，并修复日记模板步骤中的 session 写入方式。
- 改进搜索，支持账本、类型分组和更丰富的展示。
- 修复 settings view 命令并补齐缺失开关。

### Core Session 增强

- 增强核心 `Session`，支持类似 dict 的访问方式。
- 增加插件 session cleanup 能力，方便插件在生命周期结束或流程取消时清理会话状态。

## 2026-03-22

### Pendo 多节点事件整理

- 修复 Pendo 多节点事件排序和提醒展示。
- 重构时间工具模块，让事件、提醒、查询和展示共享更一致的时间处理逻辑。
- 删除过期的计划和设计文档，减少历史草案对当前实现的干扰。

## 2026-03-21

### Pendo 复杂度收敛

- 简化 Pendo reminder、event handler、db 和 ai_parser 的复杂度。
- 让提醒、事件处理、数据库访问和 AI 解析之间的职责更清晰。

### QingPet 训练和探索系统

- 增加 QingPet 改进设计 spec、review 修订和实现计划。
- 改进不能互动时的提示，给出具体原因，并同步到 sleep_pet 场景。
- 取消 recall 对友情点的要求，让召回宠物更直接。
- 增加训练和探索地点配置常量。
- 改进训练和探索系统，加入类型、地点和性格影响。
- 在宠物卡片中展示剩余旅行时间，并更新 train/explore 帮助文本。

## 2026-03-13

### APOD、arxiv_filter 和 xiaoqing_chat 测试修复

- 修复 APOD 解码问题，减少天文图文内容读取失败。
- 更新 arxiv_filter 并继续整理训练代码。
- 统一 xiaoqing_chat goal derivation 逻辑，修复 mock 测试中的错误。
- 修复相关测试，保证天文、训练和聊天模块在重构后继续可测。

## 2026-03-11

### arxiv_filter 训练和推理结构重构

- 重构 arxiv_filter 训练和推理目录结构，让训练数据准备、模型训练和运行时推理边界更清楚。
- 修复 signin 测试，避免无关插件测试受训练代码调整影响。

## 2026-03-10

### v3.2.0 发布

- 发布 `v3.2.0`。
- 修复 Pendo 事件列表格式，让多节点事件和普通事件的展示更稳定。
- 修复异步 goal state 处理，减少 xiaoqing_chat 目标状态在异步流程中错位的问题。

## 2026-03-04

### Pendo 事件提醒和查询修复

- 确保每个 Pendo 事件在开始时间拥有对应提醒。
- 修复多节点事件 list 过滤逻辑，只有查询范围内存在节点时才展示该事件。
- 为动态 SQL 中的列名加引号，避免字段名碰到保留字时查询失败。

## 2026-03-03

### 本地运行脚本整理

- 将 `run-bot.vbs` 改为使用相对路径，提高不同本地目录下的可运行性。
- 忽略 `sync_to_remote.sh`，避免个人同步脚本进入仓库。

## 2026-03-02

### 项目迁移和基础 CI

- 将 XiaoQing 迁移为当前 master 仓库，作为后续开发的基线。
- 修复 CI 单元测试路径，统一使用 `tests/`。
- 修复插件标记测试的 CI job，让 plugin-marked tests 能在插件测试任务中执行。
- 更新 README 和项目文档，为迁移后的仓库补齐基础说明。

## 维护模板

```markdown
## YYYY-MM-DD

### 更新标题

- 改动内容。
- 影响范围。
- 兼容性或迁移注意事项。
- 已执行的验证。
```

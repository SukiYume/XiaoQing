# pendo-redesign 插件完整测试、修复与回归任务

你现在是一个资深全栈测试工程师、安全工程师、产品文档审查员和代码维护者。请对仓库中的 `plugins/pendo` 插件执行一次完整的白盒 + 黑盒测试、文档审查、问题修复、冗余代码审查、回归验证，并输出可复现报告。

这不是测试计划任务。你必须实际阅读代码、枚举命令和参数、生成测试矩阵、执行测试、保存日志、修复问题、补充自动化测试、清理确认无用的冗余代码并回归。不能只做代表性测试，也不能只写报告。

## 一、背景与环境

当前分支是 `pendo-redesign`，请重点检查 `plugins/pendo`，并结合 `master` 对比理解本次重构带来的行为变化、兼容性风险和潜在回归。

本地已有生产数据库及备份：

- `plugins/pendo/data/pendo.db`
- `plugins/pendo/data/pendo.db.bak.20260430`

迁移报告可能位于：

- `plugins/pendo/data/pendo.db.pendo-redesign-report-20260430152313.json`

服务已启动：

- HTTP 命令接口：`http://127.0.0.1:12000`
- Web 页面：`http://127.0.0.1:8765`

可以参考 `test_http.ipynb` 的 HTTP 消息格式，但不要只依赖 notebook。请把关键测试沉淀成可重复运行的脚本、测试用例或文档。

允许对当前 `pendo.db` 做任意增删改查，包括破坏性操作。测试前必须确认备份可用；破坏性测试前后要能从备份恢复，保证测试可重复。

## 二、最高优先级：命令面和参数面的完整覆盖

之前的测试只做了少量代表性命令，这是不够的。本轮最重要的目标是：**每一条 `/pendo xxx yyy` 命令、每一个别名、每一个参数，都必须有多种测试输入，并且有日志证据。**

### 2.1 禁止跳过的规则

禁止出现以下做法：

1. 只测几个 happy path 后声称命令层通过。
2. 只测 HELP_MAP 里的少量示例，不检查实际 router/handler 支持的命令。
3. 只看代码不执行真实 HTTP 命令。
4. 只直接操作 SQLite 后把结果当成功能测试。
5. 只写测试计划或覆盖清单，不执行。
6. 某条命令只测一个参数组合。
7. 某个参数只测一种取值。
8. 发现 HELP_MAP、router、handler、Web 文档不一致时只记录不修复。
9. 最终报告中把未执行、无法执行、环境失败的测试写成通过。
10. 清理冗余代码时只靠静态搜索，忽略动态分发、字符串路由、定时任务、Web API、Scriptable 外部调用。

### 2.2 Coverage Gate 0：先产生命令库存，不完成不得进入最终结论

在执行大规模测试前，必须先生成完整命令库存，建议保存为：

- `plugins/pendo/test_reports/pendo-command-inventory-20260430.md`
- `plugins/pendo/test_reports/pendo-command-inventory-20260430.json`

命令库存必须同时来自以下来源，并做交叉比对：

1. `plugins/pendo/main.py` 中的 `HELP_MAP`。
2. 命令解析器、router、handler、dispatch 表。
3. 代码中实际支持的子命令、别名、参数解析分支。
4. README、Web 页面、导入页示例、其他文档中的 `/pendo ...` 示例。
5. `test_http.ipynb` 或已有测试中的命令样例。
6. master 与 `pendo-redesign` diff 中新增、删除、改名的命令。

每条库存记录至少包含：

| 字段             | 要求                                                         |
| ---------------- | ------------------------------------------------------------ |
| command_id       | 稳定 ID，例如 `CMD_EVENT_EDIT`                               |
| source           | HELP_MAP / router / handler / README / Web / test / diff     |
| command          | 完整命令模式，例如 `/pendo event edit <id> <内容>`           |
| top_level        | event / note / task / diary / ledger / import / export / search / settings / help 等 |
| subcommand       | add / list / edit / delete 等，按实际代码填写                |
| aliases          | 所有别名                                                     |
| handler          | 实际处理函数或路由位置                                       |
| required_params  | 必填参数                                                     |
| optional_params  | 可选参数、默认值、开关参数                                   |
| allowed_values   | 枚举值、金额符号、状态、优先级、日期范围等                   |
| id_semantics     | 普通对象 ID、集合 ID、节点 ID、occurrence ID 等              |
| data_side_effect | 新增、修改、删除、查询、导入、导出、设置变更等               |
| examples         | HELP_MAP 和文档里的示例                                      |
| doc_status       | 文档是否存在、是否过时、是否误导                             |
| code_status      | 代码是否真实支持                                             |
| mismatch         | HELP_MAP 有但代码无、代码有但 HELP_MAP 无、参数不一致等      |

库存完成后，必须在报告中明确列出：

- HELP_MAP 中有但代码不支持的命令。
- 代码支持但 HELP_MAP 未列出的命令。
- 参数说明不完整或错误的命令。
- 示例不可执行的命令。
- 新旧分支行为变化的命令。

### 2.3 Coverage Gate 1：为每条命令生成参数测试矩阵

必须基于命令库存生成参数级测试矩阵，建议保存为：

- `plugins/pendo/test_reports/pendo-command-parameter-matrix-20260430.md`
- `plugins/pendo/test_reports/pendo-command-parameter-matrix-20260430.json`

每个测试用例必须有唯一 `case_id`，例如：

- `CMD_EVENT_EDIT_ID_VALID_001`
- `CMD_EVENT_EDIT_ID_NOT_FOUND_001`
- `CMD_EVENT_EDIT_CONTENT_NODE_TIME_001`
- `CMD_LEDGER_ADD_AMOUNT_NEGATIVE_001`
- `CMD_IMPORT_JSON_MISSING_FIELD_001`

每条命令至少覆盖：

1. 正常输入：至少 2 到 3 个不同有效样例，不能只测一个 happy path。
2. 缺失必填参数。
3. 省略可选参数，验证默认值。
4. 多余参数或未知参数。
5. 参数类型错误。
6. 参数格式错误。
7. 边界值：空字符串、超长字符串、0、负数、极大值、日期边界、跨天/跨月/跨年。
8. 中文、英文、emoji、特殊符号、多行文本。
9. 安全 payload：SQL 注入、XSS、HTML 注入、路径穿越、命令注入。只在相关参数上使用，例如标题、正文、备注、路径、导入内容、搜索词。
10. 重复请求：重复创建、重复修改、重复删除、重复导入。
11. 不存在 ID、错误 ID 类型、属于其他对象类型的 ID。
12. Web 与命令端一致性，至少核心对象要交叉验证。
13. 数据库最终一致性和 invariant 校验。
14. 服务重启后的持久性，至少覆盖核心 CRUD 和设置。

每一个参数必须至少被独立覆盖以下取值类别：

| 参数类型  | 必测取值类别                                                 |
| --------- | ------------------------------------------------------------ |
| ID        | 有效 ID、不存在 ID、错误格式 ID、错对象类型 ID               |
| 文本      | 正常文本、空文本、超长文本、中文/emoji/特殊符号、HTML/XSS、SQL 注入样式文本 |
| 日期时间  | 绝对时间、相对时间、中文日期、跨天、跨月、跨年、非法时间、空值 |
| 金额      | 正数、负数、0、小数、极大值、非法数字、精度边界              |
| 枚举      | 每个合法枚举值、大小写/别名、非法枚举                        |
| 布尔/状态 | true/false、完成/未完成、重复切换、非法状态                  |
| 路径/文件 | 合法路径、空路径、不存在路径、路径穿越、损坏文件、超大文件   |
| 搜索词    | 普通词、中文、特殊字符、空词、超长词、大小写                 |
| 范围      | 起止正常、起止相等、开始晚于结束、跨周、跨月、跨年           |

不要做无意义的全笛卡尔积爆炸。可以使用参数矩阵、pairwise 和关键组合，但必须保证：**每条命令的每个参数都至少经历有效、缺失/默认、非法、边界、安全或特殊字符中的多种测试。**

### 2.4 Coverage Gate 2：执行矩阵并保存原始证据

必须通过 HTTP 命令接口发送真实 `/pendo xxx` 消息，模拟用户真实输入。每个用例要记录：

| 字段            | 要求                                             |
| --------------- | ------------------------------------------------ |
| case_id         | 对应矩阵中的唯一 ID                              |
| command         | 实际发送的 `/pendo ...` 文本                     |
| request         | HTTP method、URL、body、headers 关键字段         |
| response_status | HTTP 状态码                                      |
| response_body   | 原始响应，必要时脱敏                             |
| expected        | 预期结果                                         |
| actual          | 实际结果                                         |
| db_check        | 数据库校验 SQL 或校验函数结果                    |
| web_check       | 如适用，Web 是否可见、可编辑、可删除             |
| invariant_check | 是否破坏 event-reminder、ledger 统计等 invariant |
| result          | PASS / FAIL / BLOCKED / SKIPPED                  |
| reason          | FAIL/BLOCKED/SKIPPED 原因                        |

建议保存为：

- `plugins/pendo/test_reports/pendo-command-test-results-20260430.jsonl`
- `plugins/pendo/test_reports/pendo-command-test-results-20260430.md`

最终报告中必须能追溯每条结论对应的 `case_id` 和日志。没有执行日志的测试不得写成通过。


### 2.5 Coverage Gate 3：断点续测与上下文压缩恢复协议

本任务可能很长，不能依赖对话历史或压缩摘要来记住进度。必须把测试状态写入仓库文件，让任何新的 Codex 会话、压缩后的会话或 `codex resume` 后的会话，都能从文件恢复并继续执行。

#### 2.5.1 强制持久化状态文件

在进入大规模执行前，必须创建并持续维护以下文件：

- `plugins/pendo/test_reports/pendo-run-state-20260430.json`
- `plugins/pendo/test_reports/pendo-next-actions-20260430.md`
- `plugins/pendo/test_reports/pendo-session-handoff-20260430.md`

`pendo-run-state-20260430.json` 至少包含：

| 字段                         | 要求                                                         |
| ---------------------------- | ------------------------------------------------------------ |
| run_id                       | 本轮测试唯一 ID                                              |
| repo_path                    | 仓库路径                                                     |
| branch                       | 当前分支                                                     |
| base_branch                  | 对比分支，例如 `master`                                      |
| started_at / last_updated_at | 开始和最后更新时间                                           |
| current_phase                | inventory / matrix / command_exec / web_e2e / stats / scriptable / cron / deadcode / regression / report |
| completed_gates              | 已完成的 Coverage Gate                                       |
| artifacts                    | 已生成文件路径，包括库存、矩阵、执行日志、报告草稿           |
| command_inventory_count      | 命令库存数量                                                 |
| matrix_case_total            | 参数矩阵用例总数                                             |
| executed_case_ids            | 已执行 `case_id` 列表或范围                                  |
| passed_case_ids              | PASS 用例                                                    |
| failed_case_ids              | FAIL 用例                                                    |
| blocked_case_ids             | BLOCKED 用例及原因                                           |
| skipped_case_ids             | SKIPPED 用例及原因                                           |
| pending_case_ids             | 仍未执行的用例                                               |
| current_command_domain       | 当前正在测的顶层命令域                                       |
| last_successful_case_id      | 最近成功执行的用例                                           |
| last_failed_case_id          | 最近失败用例及复现命令                                       |
| db_snapshot_path             | 当前可恢复的数据库快照                                       |
| server_status                | HTTP 服务、Web 服务、端口、启动命令、健康检查结果            |
| git_status_summary           | 当前 `git status --short` 摘要                               |
| modified_files               | 已修改文件列表                                               |
| known_issues                 | 已发现问题摘要，关联 issue ID 或 case_id                     |
| next_actions                 | 下一步要做的具体动作，按优先级排序                           |
| resume_prompt                | 下一次恢复时应直接复制给 Codex 的续测提示                    |

`pendo-next-actions-20260430.md` 必须用人类可读格式列出：

1. 当前已完成到哪里。
2. 下一条应执行的 `case_id`。
3. 下一个命令域或页面。
4. 当前失败/阻塞项。
5. 不应重复执行的已通过范围。
6. 需要优先回归的修复项。
7. 恢复测试前必须检查的环境项。

`pendo-session-handoff-20260430.md` 必须像交接班说明一样可独立阅读，包含：

1. 本轮任务目标摘要。
2. 已完成 Coverage Gate。
3. 关键文件路径。
4. 当前风险和未解决问题。
5. 继续执行的精确步骤和命令。
6. 预计产出的最终报告路径。

#### 2.5.2 状态更新频率

必须在以下时机更新 `run_state`、`next_actions` 和 `session_handoff`：

1. 完成命令库存后。
2. 完成参数矩阵后。
3. 每执行完一个命令域，例如 event、note、task、diary、ledger。
4. 每执行 10 到 20 个矩阵用例后。
5. 每发现一个 P0/P1/P2 问题后。
6. 每修复一个问题并完成回归后。
7. 每次准备执行 `/compact`、上下文压缩、退出 Codex、切换会话或长时间中断前。
8. 每次恢复会话后的第一件事：先读取这些文件，再继续未完成用例。

如果没有更新这些状态文件，不得声称“可以恢复继续测试”。

#### 2.5.3 恢复会话后的启动流程

每次恢复或压缩后，必须先执行以下恢复流程，再继续测试：

1. 读取 `pendo-session-handoff-20260430.md`。
2. 读取 `pendo-run-state-20260430.json`。
3. 读取命令库存 JSON 和参数矩阵 JSON。
4. 读取 `pendo-command-test-results-20260430.jsonl`，按 `case_id` 去重，计算 PASS / FAIL / BLOCKED / SKIPPED / PENDING。
5. 对比 `run_state.pending_case_ids` 与结果日志，修正不一致。
6. 执行 `git status --short`，确认当前未提交修改。
7. 检查 `pendo.db` 或测试数据库快照是否存在。
8. 对 HTTP 服务和 Web 服务做健康检查。
9. 从第一个 `pending_case_id` 继续执行，不要重新从头开始。
10. 如需重跑，必须说明原因，例如修复后回归、环境重置或日志缺失。

恢复后的第一段输出必须包含：

- 已读取的状态文件路径。
- 已完成用例数、失败数、阻塞数、剩余数。
- 下一条准备执行的 `case_id`。
- 当前是否需要先恢复数据库或重启服务。

#### 2.5.4 日志必须可追加、可去重、可审计

`pendo-command-test-results-20260430.jsonl` 必须使用 append-only 方式记录。重复执行同一 `case_id` 时，不能覆盖旧记录，必须增加 `attempt` 字段，并在最终统计中以最新一次有效 attempt 为准。

每条结果至少包含：

- `case_id`
- `attempt`
- `started_at`
- `finished_at`
- `command`
- `response_status`
- `result`
- `reason`
- `db_check`
- `web_check`
- `git_commit_or_worktree_state`

最终统计脚本必须能够从 JSONL 重新计算覆盖率，而不是依赖聊天中的口头统计。

#### 2.5.5 压缩前的硬性要求

准备压缩上下文、执行 `/compact` 或结束当前 Codex 回合前，必须先完成以下动作：

1. 写入最新 `pendo-run-state-20260430.json`。
2. 写入最新 `pendo-next-actions-20260430.md`。
3. 写入最新 `pendo-session-handoff-20260430.md`。
4. 确认所有已执行用例都已追加到 JSONL。
5. 生成或刷新覆盖率摘要。
6. 在回复中明确下一步要从哪个 `case_id` 继续。

如果来不及完成全部测试，也必须先完成断点文件和日志同步。宁可少跑几个用例，也不能让已完成和未完成边界丢失。

#### 2.5.6 建议的恢复提示

恢复 Codex 会话时，直接给它以下提示：

```text
继续 pendo-redesign 插件测试，不要从头开始。先读取：
1. plugins/pendo/test_reports/pendo-session-handoff-20260430.md
2. plugins/pendo/test_reports/pendo-run-state-20260430.json
3. plugins/pendo/test_reports/pendo-command-inventory-20260430.json
4. plugins/pendo/test_reports/pendo-command-parameter-matrix-20260430.json
5. plugins/pendo/test_reports/pendo-command-test-results-20260430.jsonl

请按 case_id 去重统计已执行结果，确认 pending_case_ids，然后从第一个未完成 case_id 继续执行。不要把未执行项写成通过。每执行 10 到 20 个用例、每完成一个命令域、每修复一个问题、以及每次准备压缩上下文前，都必须更新 run_state、next_actions 和 session_handoff。
```

## 三、真实入口优先原则

直接操作数据库只能用于：

- 准备测试数据。
- 清理测试数据。
- 恢复快照。
- 校验命令或 Web 操作后的数据库状态。
- 验证迁移结果。
- 验证统计图表和定时任务的数据来源是否正确。
- 辅助验证冗余代码清理后的行为一致性。

功能测试必须覆盖真实用户入口：

- `/pendo xxx` 命令。
- HTTP 命令接口。
- Web 页面操作。
- Web API。
- 导入导出页面。
- Scriptable widget 代码对应的数据接口。
- 定时任务实际调用的数据查询逻辑。

核心 CRUD 必须通过 `/pendo xxx` 命令和 Web 端至少各覆盖一轮。不要把“直接插入 SQLite 后查询成功”当成功能测试通过。

## 四、必须覆盖的命令域

命令库存和参数矩阵至少应覆盖这些顶层命令；如果代码中还有其他命令，也必须加入：

- `/pendo help ...`
- `/pendo event ...`
- `/pendo note ...`
- `/pendo task ...`
- `/pendo diary ...`
- `/pendo ledger ...`
- `/pendo import ...`
- `/pendo export ...`
- `/pendo search ...`
- `/pendo settings ...`

对每个顶层命令，都要从代码中枚举所有子命令、别名、参数和示例。不要只写上面这些顶层名称。

### 4.1 event + reminder 专项

必须通过真实 `/pendo event ...` 命令覆盖：

1. 单次事件。
2. 重复事件。
3. 多节点事件。
4. 0 个提醒、1 个提醒、多个提醒。
5. 新增、查看、修改、删除。
6. 时间解析：绝对时间、相对时间、中文日期、跨天、跨月、跨年、模糊时间、非法时间。
7. 标题、备注、地点、分类、提醒等字段。
8. 单次事件整体修改。
9. 重复事件整体修改。
10. 重复事件某一次 occurrence 修改和删除。
11. 多节点事件整体修改。
12. 多节点事件单个节点修改。
13. 修改事件时间后 reminder 是否跟随或保持，按代码设计验证。
14. 修改重复规则后 occurrence 和 reminder 是否一致。
15. 删除事件后 reminder 是否同步删除。
16. 查询空结果、非法参数、跨天/跨周/跨月/跨年范围。
17. 重复创建、重复删除、删除不存在 ID、修改不存在 ID。
18. Web 创建事件后命令能查到；命令创建事件后 Web 能展示。
19. 服务重启后事件和提醒仍能查询。

重点验证 `event edit` 帮助示例是否真实可执行，尤其是：

```text
/pendo event edit <id> <内容>
会议开始改成4月22日12:43
会议开始改成4月22日12:43，备注从北京南坐G123去会场
```

必须判断：

- 是否会把节点名和时间修改正确解析，而不是误改标题。
- `备注从...` 是否稳定识别为备注；如果不稳定，应改成更明确的 `备注为...`、`备注改成...`、`添加备注...`，并同步修复 HELP_MAP 或解析逻辑。
- 集合 ID、节点 ID、重复事件 ID、occurrence ID 的使用说明是否清楚。
- HELP_MAP 是否覆盖单次事件、重复事件、多节点事件、提醒增删改、occurrence 修改等关键示例。

### 4.2 note / task / diary / ledger 专项

每个模块必须通过真实 `/pendo xxx` 命令覆盖新增、查询、修改、删除、搜索或范围查询、非法 ID、缺失字段、多余字段、空字符串、超长字符串、中文、emoji、HTML/Markdown、SQLi、XSS、重复请求、Web 与命令一致性。

`ledger` 额外覆盖：收入、支出、转账或退款如果支持、负数、0、小数、超大数、非法数字、金额精度、分类、账户、备注、日期、周/月统计、Web 图表、财务定时任务。

`task` 额外覆盖：状态、优先级、截止时间、完成时间、完成后再修改、取消完成、过期任务、今日任务、Web 与命令状态一致性。

`diary` 额外覆盖：日期唯一性或多条策略、同一天多次写入、修改当天和历史日记、日期范围、正文搜索、Markdown/HTML 渲染安全。

`note` 额外覆盖：标题、内容、标签、分类、归档或置顶如果支持、标题/正文/标签搜索、Markdown/HTML 渲染安全。

### 4.3 import / export / search / settings / help 专项

必须覆盖：

- HELP_MAP 示例逐条执行。
- 导出全量、按类型、按范围，如果支持。
- 导入刚导出的数据并做 round-trip 校验。
- 导入重复数据、缺字段、类型错误、损坏 JSON/CSV、恶意内容、超大内容。
- 导入导出路径安全和错误提示。
- 搜索关键词、空关键词、特殊字符、中文、大小写、日期范围、类型过滤。
- settings 读取、修改、保存、刷新后持久化、非法值、对功能/统计/定时任务的影响。
- help 顶层、子命令、未知命令、别名、示例、错误提示。

## 五、Web、API、统计、Scriptable、定时任务专项

### 5.1 HTTP 命令接口

通过 `http://127.0.0.1:12000` 发送真实请求，覆盖命令矩阵中的所有 `/pendo xxx`。同时测试 JSON 格式错误、空 body、超大 body、并发请求、重复请求、错误响应、500 泄露风险、失败请求后的数据库一致性。

### 5.2 Web E2E

通过 `http://127.0.0.1:8765` 做真实浏览器测试。优先使用 Playwright 或仓库已有 E2E 工具。

覆盖首页、总览页、统计页、event/reminder、note、task、diary、ledger、搜索、导入、导出、设置、图表、空状态、错误状态、刷新、删除后消失、修改后更新、表单校验、XSS 安全渲染、console error、网络请求失败、静态资源 404、移动/窄屏如果实现了响应式。

核心一致性链路必须覆盖：

1. 命令创建。
2. Web 查看。
3. Web 修改。
4. 命令查询。
5. Web 或命令删除。
6. 另一端确认消失。
7. 数据库确认一致。

### 5.3 Web 总览、统计和图表

构造一组固定、确定性测试数据，至少包含固定日期、固定收入/支出、固定任务状态、固定事件时间、固定 diary 日期、固定 note 标签或分类。

必须从四层核对数值：

1. 数据库聚合。
2. 后端 API 返回。
3. Web 页面显示。
4. 图表展示。

重点验证 event/reminder 数量、今日/明日/本周/本月、重复事件 occurrence、多节点聚合、过期/待办/完成任务、ledger 收入/支出/净额/分类/账户、diary 连续天数如果有、note 标签或分类如果有。

### 5.4 Web 导入页面示例

必须检查并完善 Web 导入页面示例。示例至少包含：

- event 单次事件、重复事件、多节点事件、reminders。
- note、task、diary。
- ledger 收入、支出、转账如果支持。
- settings 如果支持导入。
- 标签、分类、地点、备注、创建时间、更新时间。
- 日期时间格式、ID 是否可省略、重复导入去重策略、字段缺失默认值、非法字段处理。
- 最小可用示例和完整综合示例。

示例必须符合 pendo-redesign 新 schema，并实际导入成功；导入后要能通过命令查询、Web 展示、统计校验。

### 5.5 Scriptable iOS widget

检查 `plugins/pendo/scriptable/` 中所有 JavaScript：

- API 路径是否存在。
- 字段名是否适配新 schema。
- event/reminder、重复 occurrence、多节点、task、ledger、diary/note 如果支持的展示是否正确。
- 时间、金额、状态、分类、账户、排序、过滤是否正确。
- 空数据、网络失败、异常返回、超长文本、emoji、中文。
- 是否有旧字段、旧 API、硬编码敏感信息或内部路径。
- 没有 iOS 环境时，至少做静态检查、语法检查、mock 数据运行。

尽量新增 mock 数据测试和接口返回格式示例。

### 5.6 定时任务，尤其财务周报/月报

先枚举所有 pendo 相关定时任务，包括 reminder、每日/每周/每月摘要、财务周报/月报、统计推送、自动清理或归档等。

对每个定时任务验证注册、触发周期、时区、起止范围、数据表和字段、新 schema 适配、空数据、异常、服务重启、幂等、输出可读性、日志安全、是否存在已不再注册的死函数。

财务周报/月报必须构造固定账本数据验证：

- 同一周内 3 笔支出、2 笔收入。
- 跨周边界、跨月边界、跨年边界。
- 不同分类、不同账户。
- 小数金额、负数或退款如果支持。
- 收入、支出、净额、分类统计、账户统计。
- 数据库聚合、`/pendo ledger` 查询、Web 图表、定时任务输出四者一致。

## 六、安全、稳健性和性能边界

至少覆盖：

- SQL 注入、XSS、HTML 注入、Markdown 注入。
- 路径穿越、命令注入、任意文件读写风险。
- 导入导出危险路径。
- 前端未转义渲染。
- Scriptable 不安全远程内容处理。
- 错误信息泄露：堆栈、SQL、内部路径、敏感信息。
- 并发写入、数据库锁、竞态条件。
- 大量数据下页面可用性和统计性能。
- 超长标题、备注、正文、搜索词。
- Unicode、emoji、换行、多行文本。
- 后端失败、网络失败、断网时 Web 表现。
- 服务重启后数据一致。
- 重复提交、多窗口同时编辑、多客户端同时通过命令写入。
- 导入超大文件、导出超大数据、查询超大范围。

## 七、冗余代码、死代码和重复逻辑审查

对 `plugins/pendo` 做完整冗余代码检查。目标不是盲目删代码，而是找出 pendo-redesign 重构后遗留的无用代码、重复逻辑和过时兼容层，并在安全前提下清理。

覆盖：Python 后端、命令解析、数据库访问、migration、HTTP API、Web 前端、静态资源、Scriptable、定时任务、测试、HELP_MAP、导入导出示例和文档。

重点检查：

- 未使用 import、变量、常量、函数、类、方法。
- 重复 SQL、CRUD、时间范围计算、ledger 统计、event occurrence 展开、reminder 关联、导入导出转换、Web fetch、前端状态和表单校验。
- 旧 schema 字段兼容代码是否仍必要。
- 旧命令别名、旧 Web 静态资源、旧 Chart.js 引用、废弃页面/组件/脚本。
- 旧 Scriptable 字段适配。
- 临时代码、print、console.log、临时文件、旧 fixture。
- 永远不会执行的分支和异常处理。
- 不再注册或不会触发的定时任务函数。
- HELP_MAP、README、Web 导入说明中旧字段、旧路径、旧示例。

处理规则：

1. 每个候选冗余项必须记录证据。
2. 确认安全的直接删除。
3. 重复逻辑能安全合并的尽量合并，但不要过度抽象。
4. 无法确认无用的必须保留，并在报告中说明原因。
5. 删除或合并后必须运行相关回归。
6. 删除命令、API、字段兼容、静态资源、统计逻辑、导入导出逻辑、event/reminder 逻辑、ledger 统计逻辑前后，必须做对应命令、Web、数据库、Scriptable、定时任务回归。
7. 旧 schema 兼容逻辑如果仍被生产迁移后数据依赖，不能删除。

## 八、测试数据、隔离和恢复

所有测试数据使用唯一前缀，方便定位和清理：

- `TEST_EVENT_...`
- `TEST_REMINDER_...`
- `TEST_NOTE_...`
- `TEST_TASK_...`
- `TEST_DIARY_...`
- `TEST_LEDGER_...`
- `TEST_SECURITY_...`
- `TEST_WEB_...`
- `TEST_IMPORT_...`
- `TEST_EXPORT_...`
- `TEST_STATS_...`
- `TEST_SCRIPTABLE_...`
- `TEST_CRON_...`
- `TEST_HELP_...`
- `TEST_ERROR_...`
- `TEST_DEADCODE_...`
- `TEST_REDUNDANT_...`

并行测试时，优先为每个子任务使用独立数据库副本和独立端口；如果做不到，必须使用唯一前缀并避免并发修改同一对象；关键破坏性测试前后恢复数据库快照。

## 九、必须新增或完善的自动化测试

请尽量新增自动化测试，至少包含：

1. 命令库存生成脚本。
2. 命令参数矩阵生成脚本。
3. HTTP 命令矩阵执行脚本。
4. HELP_MAP 示例执行测试。
5. 核心 CRUD 跨入口一致性测试。
6. Web E2E 测试。
7. 导入导出 round-trip 测试。
8. Web 统计 API 与数据库聚合校验。
9. Scriptable mock 数据测试。
10. 定时任务财务统计测试。
11. 安全 payload 测试。
12. 数据库 invariant 检查。
13. 错误处理和日志测试。
14. 冗余代码清理后的回归测试。
15. 静态资源引用完整性测试。
16. 动态入口引用检查，避免误删字符串路由、定时任务或 Scriptable 间接调用。

至少建立以下 invariant：

- 删除 event 后不残留孤立 reminder。
- 删除多节点事件后不残留孤立节点。
- 删除重复事件后不残留错误 occurrence。
- reminder 必须关联有效 event 或有效 occurrence。
- ledger 金额统计必须与原始记录一致。
- Web 统计 API 必须与数据库聚合一致。
- 导出再导入后核心字段一致。
- HELP_MAP 示例不能明显失效。
- Web 页面无明显 console error、API error、静态资源 404。
- 定时任务财务统计与 ledger 查询一致。
- Scriptable mock 字段与后端 API 字段一致。
- 命令端、Web 端、数据库三者核心字段一致。
- 导入示例能实际导入成功。
- 删除或合并冗余代码后 `/pendo xxx` 命令、Web、Scriptable、定时任务、导入导出不回退。

## 十、发现问题后的闭环要求

发现问题后不要只记录。请尽量完成：

1. 复现。
2. 定位根因。
3. 最小必要修复。
4. 新增或更新自动化测试。
5. 运行相关回归，必要时运行完整回归。
6. 在报告中记录问题、影响、复现步骤、根因、修复文件、回归结果。

优先级：

- P0：数据损坏、数据丢失、严重安全漏洞、服务不可用、核心 CRUD 完全失败。
- P1：核心功能错误、事件/提醒语义错误、迁移错误、导入导出错误、Web 关键流程不可用、定时任务财务统计错误、HELP_MAP 严重误导、误删导致功能回归。
- P2：边界条件错误、错误提示不清晰、部分字段异常、非核心流程问题、文档示例不完整、Scriptable 部分展示错误、明显冗余或重复逻辑。
- P3：体验问题、文案问题、轻微 UI 问题、非阻塞维护性问题。

P0/P1 必须尽力修复并回归。P2 尽量修复。P3 可以记录；容易修复的直接修复。

## 十一、建议执行命令和检查方式

根据项目实际情况执行合适命令，包括但不限于：

```bash
git status
git diff master...HEAD -- plugins/pendo
git diff master...HEAD -- plugins/pendo/main.py
git diff master...HEAD -- plugins/pendo/scriptable
```

还应运行或新增：

- 项目已有测试、pendo 单元测试、集成测试。
- lint/typecheck。
- Web build 和 Web E2E。
- 迁移 dry-run 或迁移验证脚本。
- HTTP 命令矩阵测试脚本。
- HELP_MAP 示例测试脚本。
- 统计图表校验脚本。
- Scriptable mock 测试脚本。
- 定时任务财务统计测试脚本。
- 数据库一致性检查脚本。
- 导入导出 round-trip 测试脚本。
- dead code / unused import / unused dependency 检查。
- `rg` / `grep` 搜索候选函数、类、API 路由、静态资源路径是否仍被引用。
- 动态入口检查：命令分发、API 路由、定时任务注册、Scriptable 使用接口。

如果命令失败，请判断是环境问题、已有问题还是本次重构问题，并在报告中说明。失败不能被写成通过。

## 十二、最终交付物

请在仓库中生成详细测试报告，建议路径：

`plugins/pendo/test_reports/pendo-redesign-full-test-report-20260430.md`

同时至少生成这些辅助产物：

1. 命令库存 Markdown/JSON。
2. 命令参数矩阵 Markdown/JSON。
3. HTTP 命令矩阵执行结果 JSONL/Markdown。
4. HELP_MAP 示例执行结果。
5. Web E2E 结果和关键截图或截图路径。
6. 统计校验结果。
7. Scriptable mock 测试结果。
8. 定时任务测试结果。
9. 冗余代码检查结果。
10. 本次新增或修改的测试脚本列表。

最终报告必须包含：

1. 执行摘要。
2. 测试环境。
3. 代码阅读总结。
4. master 与 pendo-redesign 关键差异。
5. 功能地图。
6. 数据库 schema 和迁移检查结果。
7. 命令库存和 `/pendo xxx` 命令地图。
8. 命令参数测试矩阵摘要。
9. HELP_MAP / 帮助文档审查结果。
10. Web 页面和 API 地图。
11. Scriptable widget 代码审查结果。
12. 定时任务地图。
13. 测试覆盖矩阵。
14. event + reminders 测试结果。
15. note / task / diary / ledger 测试结果。
16. HTTP 命令接口测试结果。
17. HELP_MAP 示例执行结果。
18. Web E2E 测试结果。
19. Web 总览、统计和图表测试结果。
20. 静态资源 404 检查结果。
21. 导入导出、搜索、设置测试结果。
22. Web 导入页面示例完善情况。
23. Scriptable iOS widget 适配结果。
24. 定时任务和财务周报/月报测试结果。
25. 安全性、稳健性、并发和异常输入测试结果。
26. 跨入口一致性测试结果。
27. 错误处理、日志和用户反馈测试结果。
28. 冗余代码、死代码和重复逻辑检查结果。
29. 删除、合并、保留的冗余代码列表及原因。
30. 冗余代码清理后的回归测试结果。
31. 发现的问题列表，按 P0/P1/P2/P3 分类。
32. 每个问题的复现步骤、根因、修复文件、回归结果。
33. 未解决问题或风险。
34. 新增或修改的测试文件列表。
35. 本次代码修复和清理的 `git diff` 摘要。
36. 最终结论：是否建议合并 `pendo-redesign`。

### 12.1 必须使用的核心表格

命令库存表：

| command_id | source | command | aliases | handler | required_params | optional_params | examples | doc_status | code_status | mismatch |
| ---------- | ------ | ------- | ------- | ------- | --------------- | --------------- | -------- | ---------- | ----------- | -------- |

参数矩阵表：

| case_id | command_id | command | parameter_under_test | input_type | test_value | expected | db_check | web_check | result |
| ------- | ---------- | ------- | -------------------- | ---------- | ---------- | -------- | -------- | --------- | ------ |

命令执行结果表：

| case_id | 命令 | 预期结果 | 实际结果 | HTTP 状态 | 数据库校验 | Web 校验 | 状态 | 备注 |
| ------- | ---- | -------- | -------- | --------- | ---------- | -------- | ---- | ---- |

覆盖矩阵：

| 模块 | 功能 | 命令覆盖 | 参数覆盖 | HTTP 覆盖 | Web 覆盖 | 数据库校验 | 安全测试 | 回归测试 | 结果 |
| ---- | ---- | -------- | -------- | --------- | -------- | ---------- | -------- | -------- | ---- |

HELP_MAP 审查表：

| 命令 | HELP_MAP 是否存在 | 实际是否支持 | 参数是否完整 | 示例是否可执行 | 新手是否易懂 | 问题 | 修复情况 |
| ---- | ----------------- | ------------ | ------------ | -------------- | ------------ | ---- | -------- |

统计校验表：

| 页面/任务 | 指标 | 数据库结果 | API 结果 | Web 显示 | 定时任务输出 | 是否一致 | 问题 |
| --------- | ---- | ---------- | -------- | -------- | ------------ | -------- | ---- |

Scriptable 检查表：

| 文件 | 功能 | 旧字段/旧 API 风险 | Mock 测试 | 适配情况 | 问题 | 修复情况 |
| ---- | ---- | ------------------ | --------- | -------- | ---- | -------- |

定时任务检查表：

| 定时任务 | 触发周期 | 数据范围 | 数据来源 | 空数据 | 边界日期 | 幂等性 | 输出正确性 | 问题 | 修复情况 |
| -------- | -------- | -------- | -------- | ------ | -------- | ------ | ---------- | ---- | -------- |

导入导出测试表：

| 类型 | 数据来源 | 操作 | 预期结果 | 实际结果 | 命令查询 | Web 展示 | 统计影响 | 状态 |
| ---- | -------- | ---- | -------- | -------- | -------- | -------- | -------- | ---- |

冗余代码检查表：

| ID   | 文件 | 类型 | 候选冗余内容 | 判断依据 | 处理方式 | 修改文件 | 回归测试 | 状态 | 备注 |
| ---- | ---- | ---- | ------------ | -------- | -------- | -------- | -------- | ---- | ---- |

动态入口检查表：

| ID   | 入口类型 | 入口名称 | 目标函数/文件 | 静态搜索结果 | 动态调用证据 | 是否可删除 | 结论 |
| ---- | -------- | -------- | ------------- | ------------ | ------------ | ---------- | ---- |

## 十三、最终回复格式

完成后，最终回复必须真实给出：

1. 测试报告文件路径。
2. 命令库存文件路径和命令总数。
3. 参数矩阵文件路径、参数总数、测试用例总数。
4. 执行了多少条 `/pendo xxx` 命令、多少个 HTTP 请求、多少个 Web E2E 步骤。
5. HELP_MAP 检查了多少条命令，修复了多少条帮助文案或示例。
6. 发现问题数量，按 P0/P1/P2/P3 分类。
7. 修复问题数量和未修复风险。
8. Web 总览/统计/图表是否通过校验。
9. 静态资源 404 处理结果。
10. Web 导入页面示例是否补全并验证可导入。
11. Scriptable iOS widget 是否适配 pendo-redesign。
12. 每周/月财务总结等定时任务是否通过校验。
13. 冗余代码、死代码、重复逻辑检查数量；删除、合并、保留数量及原因。
14. 冗余代码清理后执行了哪些回归测试，结果如何。
15. 关键测试命令和回归结果。
16. 当前 `git status` 摘要。
17. 是否建议合并 `pendo-redesign`。

必须真实记录结果。无法执行的测试要说明原因、影响和替代验证方式。疑似冗余但无法确认无用的代码不要删除，要说明保留原因和后续建议。

## 十四、最终结论约束

如果以下任一条件不满足，最终结论不得写“完整通过”或“建议无条件合并”：

1. 未生成命令库存。
2. 未生成参数测试矩阵。
3. 库存中的任一实际支持命令没有至少一个执行用例，且没有合理 BLOCKED 原因。
4. 任一参数没有覆盖有效值和至少一种异常/边界值，且没有合理说明。
5. HELP_MAP 示例没有逐条检查。
6. 核心 CRUD 没有同时覆盖命令端和 Web 端。
7. 没有数据库 invariant 校验。
8. 没有导入导出 round-trip 校验。
9. 没有 Web 统计与数据库/API 的一致性校验。
10. 没有 Scriptable mock 或静态适配检查。
11. 没有定时任务财务统计检查。
12. 删除或合并冗余代码后没有回归。
13. 存在未修复 P0/P1，且没有明确阻塞原因和风险说明。


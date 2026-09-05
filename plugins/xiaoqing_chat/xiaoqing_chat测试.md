# ✅ xiaoqing_chat 上线验收指南

本文面向测试人员与发布维护者，提供一套可重复执行的 `xiaoqing_chat` 上线验收流程。验收覆盖自动化测试、OneBot 入口、命令参数、群聊参与、多模态、记忆、AI route、安全、并发和关闭生命周期。

---

## ✅ 验收结论

发布门禁同时满足以下条件：

1. 专项自动化测试全部通过。
2. `/xc` 命令的正常、边界、权限和错误输入全部符合 Manifest。
3. OneBot 消息段与真实群聊剧本通过。
4. 远程模型、媒体、记忆和投递提交链路通过。
5. 日志、配置、数据隔离、并发和关闭检查通过。
6. 报告包含环境、范围、证据、缺陷和最终结论。

任何 P0 或 P1 缺陷都将门禁状态置为失败。P2 缺陷需要明确发布处置与复测证据。

---

## ✅ 测试环境

每轮验收记录以下信息：

- Git commit 与工作区状态。
- Python 版本与依赖安装结果。
- OneBot 实现、版本、HTTP/WS 地址和测试 Bot QQ。
- 测试群号、管理员账号和普通成员账号。
- AI provider、model profile、route 顺序和模态。
- `plugins/xiaoqing_chat/config/xiaoqing_config.json` 哈希。
- 测试开始时间、结束时间和报告目录。

使用专用测试群、测试账号与隔离数据目录。运行前复制配置，运行后核对配置逐字节恢复、secrets 哈希稳定、端口释放和测试数据目录归属。

报告目录采用：

```text
test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/
```

`RUN_ID` 建议使用 UTC 时间与 Git 短哈希，例如 `20260807T120000Z-a1b2c3d`。

---

## 📌 第一阶段：静态与自动化门禁

### 代码与配置校验

```bash
python -m compileall -q plugins/xiaoqing_chat
python -m pytest -q tests/plugins/xiaoqing_chat
```

这组测试覆盖命令、召唤、参与、规划、回复生成、媒体、记忆、租户范围、原子存储、后台任务和关闭流程。

### 项目级 UAT 计划

先生成计划并确认阶段、端口和输出目录：

```bash
python scripts/run_full_uat.py \
  --plan-only \
  --include-chat-quality \
  --matrix-plugins xiaoqing_chat
```

执行插件命令矩阵与聊天质量门禁：

```bash
python scripts/run_full_uat.py \
  --include-chat-quality \
  --matrix-plugins xiaoqing_chat \
  --output test_reports/runs/project/<RUN_ID>
```

外部 AI 与网络服务纳入验收时增加 `--include-external`。最终报告位于输出目录的 `reports/`。

---

## ⌨️ 第二阶段：服务与 OneBot 入口

### 启动检查

1. 使用目标发布配置执行 `python main.py`。
2. 等待插件加载完成日志。
3. 确认 `xiaoqing_chat` 状态为已加载。
4. 确认 HTTP 与 WS 入站监听地址符合测试配置。
5. 确认 OneBot 连接和 Bot 自身信息读取成功。

### 事件基线

向 HTTP 或 WS 入站发送一条标准私聊事件和一条标准群聊事件，记录：

- 请求状态与 request ID。
- Router 与 Dispatcher 选择结果。
- Core 向 OneBot 动作端投递的消息及 action 回执。
- 投递回执。
- 对应会话状态变化。

重复发送相同 `message_id`，确认去重结果与状态计数。

---

## ⌨️ 第三阶段：命令矩阵

命令测试以 `plugins/xiaoqing_chat/plugin.json` 为权威清单。每个节点执行以下输入类别：

- 标准示例。
- 中文别名。
- 参数上下界。
- 参数缺失。
- 多余参数。
- 未知子命令或枚举值。
- 普通成员、群管理员、群主和 Bot 管理员权限。
- 私聊与群聊范围。

### 顶层与帮助

| 用例 | 期望 |
|---|---|
| `/xc 你好` | 进入明确召唤回复链 |
| `/xc help` | 返回完整结构化目录 |
| `/xc 帮助` | 与 help 结果一致 |
| `/xc help extra` | 返回参数错误与用法 |
| `/xc <未知子命令>` | 作为聊天内容进入回复链，或按当前路由契约返回明确结果 |

### 会话管理

| 用例 | 期望 |
|---|---|
| 私聊 `/xc reset` | 清理该私聊会话 |
| 群聊普通成员 `/xc reset` | 返回权限说明 |
| 群管理员 `/xc reset` | 返回二次确认提示 |
| 群管理员 `/xc reset confirm` | 清理当前群会话 |
| `/xc reset maybe` | 返回参数错误或确认提示 |
| `/xc stats` | 返回上下文、表达、黑话、行动与运行统计 |
| `/xc stats extra` | 返回参数错误 |

重置前写入短期历史、长期记忆、Goal、PFC、行动记录、表达、黑话和反思状态；重置后逐项核对当前 chat 范围。

### 状态查询

| 用例 | 期望 |
|---|---|
| `/xc brain` | 返回深度对话开关和参数 |
| `/xc config` | 返回频控、记忆、表达和模型摘要 |
| `/xc memory 关键词` | 返回当前 chat 范围内的排序结果 |
| `/xc memory` | 返回用法 |
| `/xc expression` | 返回当前 chat 的表达记录 |
| `/xc jargon` | 返回当前 chat 可见的黑话 |

### 模型管理

| 用例 | 期望 |
|---|---|
| `/xc model` | 返回别名、模型、provider、当前项和回退顺序 |
| 群管理员 `/xc model <名称>` | 固定当前会话聊天模型 |
| 普通成员 `/xc model <名称>` | 返回权限说明 |
| `/xc model default` | 清理当前会话覆盖项 |
| Bot 管理员 `/xc model global <名称>` | 固定全局运行时聊天模型 |
| 群管理员 `/xc model global <名称>` | 返回 Bot 管理员权限说明 |
| `/xc model global default` | 清理全局覆盖项 |
| `/xc model <未知名称>` | 返回可用名称 |

切换后分别触发聊天、推理、检查和视觉调用，确认聊天覆盖项只影响 `chat` route。重启 Bot 后确认模型选择按配置恢复。

### 反思审查

| 用例 | 期望 |
|---|---|
| `/xc review ok <id>` | 确认当前步骤或进入下一步 |
| `/xc review no <id>` | 关闭会话 |
| `/xc review answer <id> <内容>` | 应用规则、目标或策略 |
| `/xc review close <id>` | 关闭会话 |
| 错误 ID | 返回范围或过期说明 |
| 错误操作、缺少内容、多余参数 | 返回用法或字段说明 |
| 普通成员执行 | 返回权限说明 |

测试同 chat、跨 chat、过期会话和并发处理同一审查 ID。

---

## 💬 第四阶段：消息段矩阵

每种消息都从真实 OneBot 入口进入，并经过 `observe_message()` 与 `handle_smalltalk()`：

| Segment | 核对内容 |
|---|---|
| `text` | 文本顺序、空白规范化和上下文记录 |
| `at` | Bot `@`、他人 `@` 与召唤原因 |
| `reply` | 指向 Bot、群友、缺失 message_id 与历史查找 |
| `face` | QQ face 描述与低信息安静结果 |
| `mface` | 来源验证、marker、采集与缓存 |
| `image` | 下载、大小、像素、动画、视觉描述与缓存 |
| 混合 segment | 原始顺序、文本与媒体关联 |

边界样本包括空文本、仅标点、多个图片、超大图片、动图、多段 `at`、失效 reply 和重复媒体 URL。

---

## 📌 第五阶段：召唤与参与

### 明确召唤矩阵

覆盖以下剧本：

- 私聊普通消息。
- `/xc` 显式消息。
- 群聊 `@` Bot。
- 文本包含 Bot 名称。
- 只喊名称后同一用户续问。
- reply 引用 Bot 上一条消息。
- 历史含 Bot 锚点后的 `她`、`他`、`ta` 共指。
- 历史锚点切换到另一位群友后的代词消息。

记录 `forced`、`reason`、`direct_mentioned`、`coreference_mentioned` 和 `reply_to_bot`。

### 普通参与矩阵

覆盖以下状态组合：

- 基础概率命中与未命中。
- 面向全群的问题、邀请和开场。
- 明确点给其他成员的话题。
- 上一轮 Goal 匹配与普通延续。
- 活跃话题追问。
- 仅 QQ face 与标点。
- 最小间隔、每分钟上限和连续回复冷却。
- Heartflow 各权重信号。
- PFC 的 reply、observe、wait 和目标调整。
- 全局、会话、用户并发配额与用户日用量。

使用固定随机种子复现概率用例。门禁日志应给出稳定原因码。

---

## 🧠 第六阶段：真实模型与拟人质量

### 质量探针

服务运行时执行：

```bash
python scripts/run_xiaoqing_chat_quality.py \
  --endpoint http://127.0.0.1:12000/onebot \
  --chat-data-dir test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/data/xiaoqing_chat \
  --output test_reports/runs/plugins/xiaoqing_chat/<RUN_ID>/quality.json
```

该端口对应专用测试 Bot。`--chat-data-dir` 指向这个 Bot 实际使用的隔离插件数据目录，运行前确认目录已经创建。探针使用 `X-XiaoQing-Response-Mode: actions` 获取调试动作预览；普通 OneBot HTTP 事件响应为空对象，动作端回执提供实际投递证据。报告文件使用新路径，脚本会保护已有报告。

### 拟人大群实验

```bash
python -m plugins.xiaoqing_chat.experiments.anthropomorphic_group \
  --mode real \
  --run-id <RUN_ID> \
  --groups 20 \
  --rounds-per-group 150
```

实验产物包括矩阵、JSONL 结果、群聊 transcript 和摘要。`--seed` 固定工作负载，`--max-real-turns` 控制远程调用规模。

### 真实群聊剧本

至少覆盖以下场景：

1. 多人快速闲聊，机器人选择合适轮次参与。
2. 两位群友互相对话，机器人保持目标对象判断。
3. 开放问题邀请全群回答。
4. `@`、名称、reply 和共指连续对话。
5. 图片、表情包、QQ face 与文字混合交流。
6. 长空档后的新话题。
7. 回忆偏好、昵称和既往约定。
8. 科学、数值和单位问题触发 reasoning。
9. 含糊事实、第三方信息和现实承诺触发 reply checker。
10. 高频消息、重复文本和并发用户。

每轮记录输入、召唤原因、参与决策、planner 动作、模型 profile、回复、媒体段、检查结果、耗时和投递状态。

### 质量评分

每个群聊剧本按 1–5 分记录：

- 参与时机。
- 上下文连贯。
- 人格一致。
- 口语自然。
- 信息准确。
- 群聊对象识别。
- 多模态自然度。
- 沉默选择。

同时记录频繁追问、模板化、复读、说话人错位、媒体错位、事实越界和刷屏等缺陷标签。

---

## 🧠 第七阶段：记忆、规划与学习

### 会话和长期记忆

1. 连续对话写入用户与助手消息。
2. 投递确认前保持助手状态待提交。
3. 投递确认后写入助手记忆和行动记录。
4. 制造长空档并核对连续片段、Goal、PFC、Thinking Back 与摘要刷新。
5. 用 `/xc memory` 核对向量排序、阈值和 chat scope。
6. 制造直接检索空结果，并用明确回指触发工具代理。
7. 重启 Bot，核对已确认状态恢复。

### 人物资料、表达与黑话

覆盖事实提取断点、重复消息、画像更新、表达计数、黑话可见范围、人工审查和选择器注入。分别测试当前 chat、另一个群聊与私聊。

### 存储健壮性

为各 JSON store 构造：

- 合法主文件。
- 合法备份文件。
- 截断 JSON。
- 错误对象根。
- 错误字段类型。
- `NaN`、`Infinity` 与超范围数值。
- 并发保存与重启恢复。

核对原子发布、备份选择、字段级回退、脏标记和防抖保存次数。

---

## 🩺 第八阶段：AI 与媒体故障

### AI route

对 `chat`、`reasoning`、`checker` 和 `vision` 分别注入：

- 首个 profile 成功。
- 首个 profile 超时，下一 profile 成功。
- 限流和服务端错误。
- 认证与参数错误。
- 空响应、畸形 JSON 和错误根类型。
- 总超时耗尽。

核对 route 顺序、重试预算、总超时、错误类别和用户侧结果。

### 回复检查

覆盖确定性 hard、确定性 soft、语义 hard、语义 soft、重生成、重规划、主动参与耗尽和明确召唤耗尽。每个用例核对候选状态与最终提交状态。

交流约束通过独立阅读完整输入与回复验收，覆盖直接追问、间接陈述、引用、条件句及同义改写。质量探针保留人工复核材料，`machine_gate_passed` 只代表机器指标，`semantic_review_required` 标记独立语义复核需求；复核前 `semantic_acceptance_passed` 保持空值。

缺图用例使用纯文字事件指向具体截图，核对回复明确说明未知画面，并检查颜色、布局、文字和物体陈述的证据。历史真实图片作为独立正例，验证回指仍可使用对应摘要。陈旧话题探针的 `current_turn_discloses_missing_image` 同时记录缺图告知，旧话题排除保持独立检查。

### 媒体

覆盖视觉超时、下载超时、来源受限、解码错误、缓存命中、素材相似去重、磁盘配额、TTL 清理和 marker 空匹配。核对文本、媒体段和素材使用记录的一致性。

---

## 🔐 第九阶段：安全、并发与关闭

### 权限与隔离

- 事件 `user_id` 与 `context.principal` 一致性。
- 私聊、群管理员、群主和 Bot 管理员能力。
- 跨群 memory、review、expression、jargon 和媒体访问。
- 插件 AI route 与 provider 凭据边界。
- 日志中的 token、API Key、URL 查询、用户文本和标识符脱敏。

### 并发

- 同一 chat 多条并发消息。
- 多个 chat 并发消息。
- 同一用户跨 chat 的生成配额。
- reset 与生成并发。
- reset 与 memory save 并发。
- 投递回执乱序与重复回执。
- 后台学习任务创建失败与重复调度。

核对锁粒度、生成配额释放、去重、状态提交顺序和 store 完整性。

### 关闭

1. 创建防抖保存、媒体细化和学习任务。
2. 触发插件 shutdown。
3. 核对任务接收状态切换。
4. 核对限时等待、取消和异常记录。
5. 核对所有脏 store 刷盘。
6. 再次启动并核对状态恢复。

---

## ✅ 缺陷分级

| 级别 | 定义 | 示例 |
|---|---|---|
| P0 | 数据、安全或生命周期灾难 | 跨群泄露、凭据泄露、数据损坏、主进程崩溃 |
| P1 | 核心能力失效 | 明确召唤无响应、权限绕过、状态错误提交、模型链全部失效 |
| P2 | 主要体验或边界异常 | 参与时机明显偏差、媒体错位、错误提示缺失、持久化恢复偏差 |
| P3 | 局部展示与维护问题 | 文案、日志字段、低频格式问题 |

---

## ✅ 报告模板

每轮验收报告包含以下内容：

### 环境

| 项目 | 值 |
|---|---|
| RUN_ID | |
| Git commit | |
| Python | |
| OneBot | |
| 测试群与账号 | |
| AI routes | |
| 配置哈希 | |

### 阶段结果

| 阶段 | 状态 | 用例数 | 失败数 | 证据路径 |
|---|---:|---:|---:|---|
| 自动化门禁 | | | | |
| OneBot 入口 | | | | |
| 命令矩阵 | | | | |
| 消息段矩阵 | | | | |
| 召唤与参与 | | | | |
| 拟人质量 | | | | |
| 记忆与学习 | | | | |
| AI 与媒体故障 | | | | |
| 安全、并发与关闭 | | | | |

### 缺陷

| ID | 级别 | 场景 | 复现步骤 | 实际结果 | 期望结果 | 修复提交 | 复测证据 |
|---|---|---|---|---|---|---|---|

### 发布结论

- 门禁状态：通过 / 失败。
- P0：数量。
- P1：数量。
- P2：数量与处置。
- 自动化报告路径。
- OneBot 证据路径。
- 拟人实验摘要路径。
- 配置与数据完整性结果。
- 测试负责人和完成时间。

最终结论基于本轮报告中的实际证据。后续复测使用新的 `RUN_ID`，并在缺陷表中关联原缺陷与修复提交。

## 完整审查回归

`tests/plugins/xiaoqing_chat/test_review_xchat_regressions.py` 使用临时目录、模型替身和受控任务验证以下边界：关闭时任务存活、错误结构配合有效备份、锁淘汰与等待者身份、隔离记忆不可见、连续重启后的消息编号、人物档案重置代际、长历史最新消息、等待时长类型与范围、自审空修改字段、跨会话学习快照、相似表情元数据以及并发表情索引更新。

执行 `python -m pytest tests/plugins/xiaoqing_chat -q` 可同时运行插件既有测试及上述回归。真实 QQ 投递、外部模型行为和上线表现使用本文前述验收步骤单独记录。

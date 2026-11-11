# XiaoQing Life 架构

目标与原则见 [DESIGN.md](DESIGN.md)，功能与配置见 [README.md](README.md)。本文写现在的模块、数据流和约定。

## 数据流

```text
Core scheduler ── */15 min ──► main.scheduled_tick
                                 │ repair.run（一次性修旧数据）→ 跨日时记 daily_summary → roll_day
                                 │ planner.decide（无模型）：开关 / 作息 / 预算 / 间隔 / 骰子 → 打分 → softmax 选活动
                                 ▼
                               rest：只写 last_decision        wind_down：睡前整理（1 次 reasoning）
                                 │ 其余
                                 ▼ 登记的后台任务（单飞，shutdown 取消）
                               session.run_session ─► _start（记时段、决策）→ 按活动分派 → stats.record_session
                                 ├─ hot_scroll / interest_dive ─► fetching（取材）→ seen 过滤 → digest（1 次 reasoning）
                                 │                                → activity（写经历、学说法、记兴趣、收拾、记时段）
                                 │                                → 消化提议的小课题过门槛就开（session.worth_opening）
                                 ├─ term_lookup  ─► 百科卡片：查实的说法 known，查不到的 30 天内不再排队
                                 ├─ thread_continue ─► research（每轮：行动 → 1 次 reasoning → 本地核对 → 立即提交）
                                 └─ work_on ─► work_on（读待核实条目 → 1 次 reasoning → 核对改动 → 提交）

xiaoqing_chat.reply_plan ─► life_signals（群聊话题词、本群没确认的黑话）─► life.turn_context
                               ◄─ life_lines / slang_notes / expression_hint / verified_terms（回写黑话库）
```

定时回调只做同步门控和决策，必须在 Core 60 秒预算内返回；上网本身是 `LifeRuntime.spawn` 登记的后台任务，
`shutdown` 取消并等待，再把存储刷盘。`turn_context` 在事件循环里同步读写内存，不碰磁盘、网络、模型，
它对状态的改动随后由登记的后台任务落盘。

## 模块

模块之间是单向依赖：活动（`session` / `research` / `work_on`）都依赖 `fetching` 和 `activity`，彼此不互相导入。

| 模块 | 职责 |
|---|---|
| `main.py` | `init` / `shutdown` / `handle`（/life）/ `scheduled_tick` / `turn_context` / `export_memes`；`LifeRuntime` 持有存储、唯一的上网任务与登记的后台任务 |
| `config.py` | `LifeConfig`：读 `config.plugins.xiaoqing_life`，越界钳制，类型错回默认 |
| `days.py` | 她的一天：凌晨 5 点分界的 `day_key` / `life_date`；`episode_day` 按经历时间戳现算；`DAY` |
| `persona_bridge.py` | 读人设卡事实与〔作息〕：此刻在做什么、离睡觉 / 醒来多少分钟；读不到退回 `quiet_hours` |
| `planner.py` | 门控 `gate`、打分 `choose`、话题选择 `choose_topic`、`Decision`；分数与因素写进 `last_decision` |
| `session.py` | 入口 `run_session`、浏览（取材 → 消化 → 提交）、查词、消化提议的开题门槛 |
| `fetching.py` | 取材：热榜、B站搜索、点开条目（详情 / 弹幕 / 热评 / 深读）、按兴趣预选、来源成败 |
| `activity.py` | 一次活动的公共部分：`SessionDeps` / `SessionOutcome`、写经历、学词条 / 表达 / 兴趣、收拾、记时段、记失败、写回强化 |
| `digest.py` | 浏览消化的提示词（外部文本只在 user 消息的 JSON 里）、编号体系 `i1` / `c1.1` / `c1.1.r1` / `d1.1` / `p1.1`、本地核对；`LEARN_FIELDS` / `LEARN_RULES` 与研究共用 |
| `research.py` | 有限步骤的研究：第一轮怎么查（`first_action`）、研究提示词、要点核对（摘录、`is_gap`、`is_rumor`）、每轮提交、空研究与搁置 |
| `threads.py` | 小课题状态机 open → reviewing → done / parked / dropped；笔记、更正、步数 |
| `knowledge.py` | 靠不靠得住：`assess`（同一 UP 主算一个来源；百科 / 维基或两个独立来源才算 verified）、`is_rumor`、`hedge` |
| `chat_topics.py` | 群聊话题词：听到（`heard_topics`）→ 睡前挑（`chat_topics`）→ 去看 / 火花 |
| `wind_down.py` | 睡前整理：日记、小课题收获与可信度、核实课题的开与结、后续问题、方法提炼、开作品、挑群聊话题、周记 |
| `lifecycle.py` | 词条 candidate → known → fading → 删除、表达、经历保留、seen、兴趣证据与阶段、快照（纯函数） |
| `reinforce.py` | 自我强化：收获公式、EMA 偏好、来源可靠性、习惯分桶、好奇边界（`frontier_ok` 挡掉泛词和投稿活动标签）、说法的社交反馈 |
| `methods.py` | 查法：睡前提炼、相近小课题复用、wins / 退役 |
| `works.py` / `work_on.py` | 小作品的数据、核对规则、聊天侧作品行 / 整理作品这一种活动 |
| `dossier.py` | 主题档案：从小课题、作品、经历确定性同步的“来龙去脉”快照；召回与更正行 |
| `memes.py` | 梗图：固定图床下载、vision 判定、sha256 去重落盘、淘汰与撤销、分页导出 |
| `turn_context.py` | 聊天侧取用：按问法选经历 / 小课题 / 档案 / 作品 / 打算 / 查明白的事，780 字预算按优先级整段取舍、同一句不说两遍；听懂与可以用的说法；表达提示；分享候选；`verified_terms` |
| `stats.py` | 每天的运行计数（只记数），跨日一行汇总，`/life stats` |
| `repair.py` | 旧数据升级：日记和睡前整理的日期按“她的一天”重算、各项每日标记收进 `done_on`、清掉研究条目留下的兴趣证据和好奇边界。每项在 `state.repairs` 记一次名字，只执行一次 |
| `stores.py` | `LifeStore`：内存快照 + 原子 JSON（`write_with_backup`），一把 `asyncio.Lock` 串行写；预算 `reserve_llm_call`；`done_today` / `mark_done`；查词落空记录 |
| `safety.py` | 外部文本清洗、注入样句、屏蔽词、脏话、摘录核对用的归一化、相似度 |
| `bili_auth.py` | B站扫码登录与凭证（只读，Cookie 只发 `api.bilibili.com`，凭证不进日志 / 状态 / 提示词） |
| `commands.py` | `/life` 子命令文本 |
| `sources/` | `base`（有界 HTTP、条目模型）、`bilibili`、`hotlists`、`watch`（弹幕）、`deep`（简介全文 / 置顶 / 字幕）、`subtitle`（WBI）、`baike`、`wiki`、`reference`（研究和作品核实共用的查百科 / 维基） |

## 小课题与研究

- **开题**：浏览消化提议，门槛是有 `done_when`、问题类型（`fact` / `practice`），并且起因是她点开、喜欢或又爱又嫌的那条；
  睡前的后续问题同样要 `done_when` 和类型。名额 `max_open_threads`，相近的不开。
- **推进**：只由研究循环推进。`planner` 一天只给同一个小课题一次（`last_session_day`）。第一轮怎么查：上次留下且没连着
  落空的 `next_action` → 问题类型（fact 先 lookup、practice 先 search）→ 哪种连着两次落空就换另一种 → 研究行动偏好。
  任何一轮这条路没东西就换另一种查法再试一次。
- **真进展**：要点要附逐字摘录并经本地核对；“材料没有说明 / 没给出 / 不足以”这类不算要点（`is_gap`），营销谣言材料
  不能当依据（`is_rumor`）。一次研究有真要点记一步，到 `thread_max_steps` 进回看；没有就记空步，连着两次搁置。
  answered → 回看；stuck → 搁置（核实课题已经查到依据时进回看）。
- **收尾**：睡前整理 done 时按笔记所引经历判断可信度，写进小课题和“自己查明白的事”。事实类、只有一个来源的开核实课题
  （`verifies` / `claim` / `claim_basis`）；核实课题 done 时按 verdict 更新原来那条和原小课题，refuted 在原小课题上记更正。

## 关键约定

- **证据**：词条与表达只来自外部材料；同一 `item_id` 不重复计作独立来源；新词条要材料里不止一处在用（`digest.voices`）。
  兴趣证据只来自浏览时她点开的条目，研究搜出来的不算（小课题话题每天记一条）；`seen` 里的条目不再消化。
- **她的一天**：凌晨 5 点分界。预算计数、睡前整理、日记日期、周记（周一凌晨仍算周日）、“昨天 / 前天”都按它；
  经历上不另存日子，按时间戳现算。一天只做一次的事记在 `state.done_on`（wind_down / followup / method / verify）。
- **预算**：先 `reserve_llm_call`（计数 +1 并落盘）再调用模型；失败不退还。查词（百科）不调模型。
- **休息**：没有候选或门控不满足时只更新 `last_decision`，不写经历。
- **失败**：所有来源失败、模型未配置 / 失败 / 输出读不懂，本次不产生经历和学习结果；`last_failure_at` 触发退避。
- **群聊话题**：聊天只在群聊、只给 `mention_policy` 判为可以自然提起的话题词；life 侧 `is_private` 不是明确的 false 一律
  不收；搜索词只来自她睡前写的，原话题词不直接发给外部网站。
- **日志**：`xiaoqing_life step=<步骤>`，只记名称、数量、原因、错误类型；`step=daily_summary` 是前一天的计数。

## 与 Core 和聊天的桥

`life.turn_context`、`life.export_memes` 在 `core/models.py` 的服务契约中声明：只有 `xiaoqing_life` 能导出、只有
`xiaoqing_chat` 能调用；`core/capabilities.py:LifeContextService` 只校验载荷和返回类型是映射，字段由两边约定：

| 方向 | 字段 |
|---|---|
| 聊天 → life（`mode=reply`） | `text`、`recent`、`bot_recent`、`is_private`、`unknown_terms`（本群没确认的黑话）、`topics`（仅群聊） |
| life → 聊天 | `life_lines`、`slang_notes`、`expression_hint`、`episode_ids`、`term_keys`、`verified_terms`（百科查实的说法，聊天回写黑话库） |
| `mode=proactive` / `probe` | 分享候选 / `{"version":1,"online_life":true}` |

## 验证

`tests/plugins/xiaoqing_life/`：按模块的测试（`test_planner`、`test_research`、`test_digest`、`test_turn_context`……）、
生产案例（`test_research_production_cases.py`）、对照 dafeiyu 的话题与知识门控（`test_friends_topics_and_knowledge.py`）、
活动比例（`test_planner_mix.py`）、14 天与 23 天可控时钟模拟。真实端点与真实模型的回放用
`scripts/run_xiaoqing_life_session.py`（`--activity`、`--thread`、`--kind`、`--done-when`、`--chat-topic`、`--bili-credential`）。

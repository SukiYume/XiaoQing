# XiaoQing Life 架构

目标与原则见 [DESIGN.md](DESIGN.md)，功能与配置见 [README.md](README.md)。本文写现在的模块、数据流和约定。

## 数据流

```text
Core scheduler ── */15 min ──► main.scheduled_tick（没开启或正在关闭时不碰任何状态）
                                 │ _decide：跨日时记 daily_summary → roll_day → 播种子兴趣
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

xiaoqing_chat.reply_plan ─► 群聊话题词（统一记忆给的）、本群没确认 / 已经解释过的黑话 ─► life.turn_context
                               ◄─ life_lines + shown_keys / slang_notes / expression_hint / verified_terms（回写黑话库）
```

定时回调只做同步门控和决策，必须在 Core 60 秒预算内返回；上网本身是 `LifeRuntime.spawn` 登记的后台任务，
`shutdown` 取消并等待，再把存储刷盘。`turn_context` 在事件循环里同步读写内存，不碰磁盘、网络、模型，
它对状态的改动随后由登记的后台任务落盘。

## 模块

模块之间是单向依赖：`session` 按活动分派到 `research` 和 `work_on`；这三者都依赖 `fetching` 和 `activity`，`research` 与 `work_on` 互不导入。

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
| `digest.py` | 浏览消化的提示词（外部文本只在 user 消息的 JSON 里）、编号体系 `i1` / `c1.1` / `c1.1.r1` / `d1.1` / `p1.1`、本地核对（含每条经历的那句原话：出处必须在这条内容里、逐字对得上）；`LEARN_FIELDS` / `LEARN_RULES` 与研究共用 |
| `research.py` | 有限步骤的研究：第一轮怎么查（`first_action`）、研究提示词、要点核对（摘录、`is_gap`、`is_rumor`）、每轮提交、空研究与搁置 |
| `threads.py` | 小课题状态机 open → reviewing → done / parked / dropped；笔记、更正、步数 |
| `knowledge.py` | 靠不靠得住：`assess`（同一 UP 主算一个来源；百科 / 维基或两个独立来源才算 verified）、`is_rumor`、`hedge` |
| `chat_topics.py` | 群聊话题词：听到（`heard_topics`）→ 睡前挑（`chat_topics`）→ 去看 / 火花 |
| `wind_down.py` | 睡前整理：日记、小课题收获与可信度、核实课题的开与结、后续问题、方法提炼、开作品、挑群聊话题、周记 |
| `lifecycle.py` | 词条 candidate → known → fading → 删除、表达、经历保留、seen、兴趣证据与阶段、快照（纯函数）。一条内容既喜欢又嫌弃记 `mixed`（半个喜欢）；兴趣按 14 天一期判断又爱又嫌 / 变冷 / 越看越上头；快照在兴趣达到 repeated（隔半天以上的两次上网都刷到）/ consistent 时按固定模板生成“最近常看……”，证据只跨几天时不说“这两周” |
| `reinforce.py` | 自我强化：收获公式、EMA 偏好、来源可靠性、习惯分桶、好奇边界（`frontier_ok` 挡掉泛词和投稿活动标签）、说法的社交反馈；参数见“选活动与自我强化” |
| `methods.py` | 查法：睡前提炼、相近小课题复用、wins / 退役 |
| `works.py` / `work_on.py` | 小作品的数据、核对规则、聊天侧作品行 / 整理作品这一种活动（每次 ≤3 处改动） |
| `dossier.py` | 主题档案：从小课题、作品、经历确定性同步的“来龙去脉”快照；召回与更正行 |
| `memes.py` | 梗图：固定图床下载、vision 判定、sha256 去重落盘（来源 `life:<sha256>`）、淘汰与撤销、分页导出。送去判定的图先由模型挑选，再做确定性预选：点赞不低于该条评论中位数、评论 ≤20 字或没有文字，gif / webp / png 优先于大 jpg，总数受 `max_vision_per_session` 限制 |
| `memory_bridge.py` | 定时检查后把新的经历、日记、查明白的事发布到统一记忆，规则见“与 Core 和聊天的桥” |
| `turn_context.py` | 聊天侧取用：按问法选素材，780 字预算按优先级整段取舍、同一句不说两遍，规则见“turn_context 取用规则” |
| `stats.py` | 每天的运行计数（只记数），跨日一行汇总，`/life stats` |
| `stores.py` | `LifeStore`：内存快照 + 原子 JSON（`write_with_backup`），一把 `asyncio.Lock` 串行写；预算 `reserve_llm_call`；`done_today` / `mark_done`；查词落空记录 |
| `safety.py` | 外部文本清洗、注入样句、屏蔽词、脏话、摘录核对用的归一化、相似度 |
| `bili_auth.py` | B站扫码登录与凭证（只读，Cookie 只发 `api.bilibili.com`，凭证不进日志 / 状态 / 提示词） |
| `commands.py` | `/life` 子命令文本 |
| `sources/` | `base`（有界 HTTP、条目模型）、`bilibili`、`hotlists`、`watch`（弹幕）、`deep`（简介全文 / 置顶 / 字幕，字幕全文或均匀取 6 段节选）、`subtitle`（WBI）、`baike`、`wiki`、`reference`（研究和作品核实共用的查百科 / 维基） |

## 小课题与研究

- **开题**：浏览消化提议，门槛是有 `done_when`、问题类型（`fact` / `practice`），并且起因是她点开、喜欢或又爱又嫌的那条；
  睡前的后续问题同样要 `done_when` 和类型。名额 `max_open_threads`，相近的不开。
- **推进**：只由研究循环推进。`planner` 一天只给同一个小课题一次（`last_session_day`）。第一轮怎么查：上次留下且没连着
  落空的 `next_action` → 问题类型（fact 先 lookup、practice 先 search）→ 哪种连着两次落空就换另一种 → 研究行动偏好。
  任何一轮这条路没东西就换另一种查法再试一次。关键词按整词截；百科取最具体的词，维基全部关键词搜不到再只搜它。
- **真进展**：要点要附逐字摘录并经本地核对；“材料没有说明 / 没给出 / 不足以”这类不算要点（`is_gap`），营销谣言材料
  不能当依据（`is_rumor`）。一次研究有真要点记一步，到 `thread_max_steps` 进回看；没有就记空步，连着两次搁置。
  answered → 回看；stuck → 搁置（核实课题已经查到依据时进回看）。搁置 `park_days` 天后可再拿起，搁置两次后再卡住就放弃。
- **收尾**：睡前整理 done 时按笔记所引经历判断可信度，写进小课题和“自己查明白的事”。事实类、只有一个来源的开核实课题
  （`verifies` / `claim` / `claim_basis`，一天一个，名额满了就不开，带上原说法的依据原文）；核实课题 done 时按 verdict 更新原来那条和原小课题，refuted 在原小课题上记更正；做法类的照实说没核实，不自动核实。
  done 的小课题可以提一个后续问题（全天最多 1 个）开成新小课题，带 `parent_id` 和 `depth`（最多 3 层，规划时 +4）。
  “自己查明白的事”引用的经历已核对，保留 `learning_keep_days` 天、上限 100 条；聊到相关话题给 1 条，问“最近学到啥”给 ≤2 条并加一句确定性的成长概括；周记的输入包含本周的 learnings。

## 选活动与自我强化

- **节律**：醒来后 2 小时内续做小课题 +6；作息活动文字含 `anime` 关键词（“刷番”“番”）时按兴趣搜的动画话题 +10，含 `meal` 关键词（“吃饭”等）时刷热榜 +10 且条目减半，含 `no_browse` 关键词（“社 / 活动 / 课 / 实验 / 考试”）时不上网。关键词表是配置项 `activity_affinity`。
- **收获**：每次上网的收获 = 喜欢 − 0.5×不喜欢 + 新确认词条 + 新表达 + 小课题推进 + 梗图，截断到 [−1, 3]；按活动和热榜来源维护 EMA 偏好（α=0.3），进入打分（±8）和来源权重。
- **来源可靠性**：同一来源连续失败 ≥2 次（含 B 站风控）6 小时内权重 ×0.2，之后自动恢复，不重试不绕过。
- **负面证据**：一次 interest_dive 一条喜欢的都没有，给所搜话题记一条 −0.5 的证据（每话题每天一次）。
- **习惯**：按“工作日 / 周末 × 小时”记录收获，形成习惯后这一刻出发的概率在 0.7～1.3 倍之间浮动。
- **好奇边界**：喜欢的经历带来新话题（消化写的话题优先，再加至多 3 个视频标签；≤50 个，排除屏蔽词、已是强兴趣的、太泛的词和 B站投稿活动 / 购物节标签）；研究搜出来的条目不长好奇边界。interest_dive 的探索部分一半从这个好奇边界抽、一半从探索种子抽；最近 5 次都没探索时 interest_dive +10；热榜预选给与最近 3 天话题不重合的条目一点新鲜感加分。
- **说法的社交反馈**：只评最近 2 小时内推荐过的说法，同一条回复对同一项只评一次（存回复文本的指纹）。ok ≥2 成为口头习惯（推荐冷却减半、优先），confused ≥2 且没有 ok 则 14 天内不再推荐（仍然听得懂）。反馈只从 `turn_context` 请求里按顺序的 `recent` 和 `bot_recent` 在内存里算出，聊天原文不落盘。

## 关键约定

- **证据**：词条与表达只来自外部材料；同一 `item_id` 不重复计作独立来源；新词条要材料里不止一处在用（`digest.voices`：不同评论 / 楼中楼，或弹幕出现多次）；已在收的说法再碰到一次照常记来源。新的、≤8 字的未查实词条进百科待查队列，查不到就保持候选并按期过期。暴力 / 自伤 / 诅咒类说法不收为词条（只检查词条本身）。
  兴趣证据只来自浏览时她点开的条目，研究搜出来的不算（小课题话题每天记一条）；`seen` 里的条目不再消化。
- **她的一天**：凌晨 5 点分界。预算计数、睡前整理、日记日期、周记（周一凌晨仍算周日）、“昨天 / 前天”都按它；
  经历上不另存日子，按时间戳现算。一天只做一次的事记在 `state.done_on`（wind_down / followup / method / verify）。
- **预算**：先 `reserve_llm_call`（计数 +1 并落盘）再调用模型；失败不退还。查词（百科）不调模型。
- **休息**：没有候选或门控不满足时只更新 `last_decision`，不写经历。
- **失败**：所有来源失败、模型未配置 / 失败 / 输出读不懂，本次不产生经历和学习结果；`last_failure_at` 触发退避。
- **群聊话题**：话题词由统一记忆整理时记下（只认群友自己说的话，标了伤心事、吵架、私事的不给），聊天只在群聊转交；life 侧 `is_private` 不是明确的 false 一律
  不收；搜索词只来自她睡前写的，原话题词不直接发给外部网站。
- **话题上网**：睡前她挑 0–2 个自己也想去网上看看的公开话题并写下搜索词，谁的私事、工作、学业、身体、感情不挑；之后按兴趣上网时一半概率去搜它（+4，每个只看一次），刚挑出来的让下一次上网不用掷骰子；搜到的经历记下是因为哪个话题去搜的（话题搜不到、退回热门榜刷到的不记）。话题不算兴趣证据，也不因为没看到喜欢的记负面证据；本群没确认意思的说法进百科待查队列。
- **日志**：`xiaoqing_life step=<步骤>`，只记名称、数量、原因、错误类型；`step=daily_summary` 是前一天的计数。

## 与 Core 和聊天的桥

`memory_bridge.py` 在每次定时检查后把游标（`state.memory_cursor`）之后的新经历、日记和查明白的事写成 `memory.record` 的笔记操作（`episode` / `self.diary` / `world.learning`，键是经历 ID、`diary:<日期>`、查明白的事的 ID），分批提交，全部成功才把游标移到这一刻。一次上网或睡前整理还在跑时游标不动：它稍后写下的条目带的是它开始时的时间，可能不晚于这一刻；这时已经写下的照发，下一次按同样的键再发一遍，记忆更新同一条。插件清单在 `uses_services` 里声明 `memory.record`。

`life.turn_context`、`life.export_memes` 在 `core/models.py` 的服务契约中声明：只有 `xiaoqing_life` 能导出、只有
`xiaoqing_chat` 能调用；`core/capabilities.py:LifeContextService` 只校验载荷和返回类型是映射，字段由两边约定：

| 方向 | 字段 |
|---|---|
| 聊天 → life（`mode=reply`） | `text`、`recent`、`bot_recent`、`is_private`、`unknown_terms`（本群没确认的黑话）、`topics`（仅群聊） |
| life → 聊天 | `life_lines`、`shown_keys`（`life_lines` 里给了细节的经历、查明白的事和日记的键，和发布到统一记忆时的键一致）、`slang_notes`、`expression_hint`、`episode_ids`、`term_keys`、`verified_terms`（百科查实的说法，聊天回写黑话库） |
| 聊天 → life（`mode=proactive`） | `exclude_ids`（已经说过的）、群聊另带 `is_private=false` 和 `topics`（这个群还记着的话题词，只用来认出哪些经历是这个群聊到才去搜的，不计入“听到”） |
| life → 聊天（`mode=proactive`） | `share_candidates`：每件有 `episode_id`（经历 ID、`learning:<ID>` 或 `work:<ID>:<日期>`）、`line`、`topics`、`heard_here` |
| `mode=probe` | `{"version":1,"online_life":true}`；插件关闭或不在时是 `None`。xiaoqing_chat 据此在每日生活小事的生成说明里加一条：网上刷到的具体视频和帖子另有真实记录，这里不写 |

### turn_context 取用规则

- **按问法选素材**：问近况给今天的日记或经历和在查的小课题；问这周给周记 / 近几天日记和快照；问最近迷上什么给快照；问等会 / 今晚 / 明天干嘛给今天剩下的作息时段（≤3 个）和小课题的下一步，标明只是打算；要链接 / 问哪个视频给标题、UP 主和链接（链接只在这时给）；追问她刚提到的那个视频给它的简介摘要。被追问“后来怎么样了”时召回主题档案，问“怎么查的”时给 1 条方法；作品行和查明白的事也按问法选入。
- **预算与去重**：780 字预算按优先级整段取舍，同一句不说两遍；交出去的经历、查明白的事和日记的键记进 `shown_keys`。
- **经历**：只给一条时带完整简介和那句原话。经历的话题对得上请求里这个会话的话题词，才写“听群里在聊才去搜的”；在别的群和私聊里它只是她自己看到的一条内容。
- **说法**：给听懂的和可以用的说法，本群黑话块已经解释过的不再解释；另有表达提示；百科查实的说法放进 `verified_terms`，聊天侧回写黑话库。
- **说明文字**：注入的说明只交代“这是什么、拿来干什么”和唯一的硬边界“事实照记录”（谁做的、看到了多少），不堆叠形式要求，其余交给她自己判断；块末一句：这些是依据，不是要讲的清单，打算和做完分开说，兴趣变了要前后都有记录才算。
- **分享候选**（`mode=proactive`）：至多 6 件，调用方给的 `exclude_ids` 不再给。两天内可说的经历按“这个会话聊到才去搜的 > 标了想分享 > 记下了原话 > 喜欢”排序，再加上三天内刚查明白的事和刚更新的作品。每条经历一行：标题、UP 主、讲了什么、那句原话和链接；记下了原话的只带她的态度，不带当时那句感想，话怎么说留给开口的时候。

## 验证

全部测试离线：假 HTTP、假模型，不访问真实网络。`tests/plugins/xiaoqing_life/`：按模块的测试（`test_planner`、`test_research`、`test_digest`、`test_turn_context`……）、
研究的取向与门槛（`test_research_direction.py`）、对照 dafeiyu 的话题与知识门控（`test_friends_topics_and_knowledge.py`）、
活动比例（`test_planner_mix.py`）、14 天与 23 天可控时钟模拟。

### 真实端点回放

`scripts/run_xiaoqing_life_session.py` 在隔离数据目录（默认 `work/xiaoqing_life/<时间戳>/`）里用真实端点和真实模型跑一次活动，打印取材、模型原始 JSON、被本地核对丢弃的条目及原因和落盘结果，并写 `result.json`。它只读 `config/config.json` 与 `secrets.json`；`--routes-from xiaoqing_chat` 在内存里复用该插件的 `reasoning` / `vision` 路由。其余参数有 `--thread`、`--kind`、`--done-when`、`--chat-topic`、`--bili-credential`。

```bash
python scripts/run_xiaoqing_life_session.py --activity hot_scroll --routes-from xiaoqing_chat
python scripts/run_xiaoqing_life_session.py --activity interest_dive --query 天文摄影 --routes-from xiaoqing_chat --data-dir work/xiaoqing_life/x
python scripts/run_xiaoqing_life_session.py --activity wind_down --advance-minutes 15 --routes-from xiaoqing_chat --data-dir work/xiaoqing_life/x
python scripts/run_xiaoqing_life_session.py --no-ai --activity hot_scroll   # 只看取材
python scripts/run_xiaoqing_life_session.py --activity thread_continue --thread "猎户座怎么拍" --routes-from xiaoqing_chat   # 研究循环，逐轮打印行动、原始 JSON 与核对结果
python scripts/run_xiaoqing_life_session.py --activity work_on --routes-from xiaoqing_chat --data-dir work/xiaoqing_life/x   # 整理已有的作品
python scripts/run_xiaoqing_life_session.py --activity thread_continue --thread "…" --bili-credential 凭证文件.json   # 只读复制凭证到隔离目录验证字幕，不打印内容
```

B 站的排行、搜索接口会间歇性触发风控（`-352` / HTTP 412），此时该来源按失败处理并换其它来源，不要高频重试。

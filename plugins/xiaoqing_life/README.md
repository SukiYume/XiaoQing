# 🌐 XiaoQing Life

XiaoQing Life 给小青一份自己的网上生活：在作息空闲的时段，她按兴趣去刷 B 站热门、搜索和热评，看知乎、微博热榜，遇到不懂的流行语就查百度百科。她看到什么、学到什么都有来源记录，聊天时能拿真实经历接话。

本插件只负责“上网”和“提供素材”，不直接说话。`xiaoqing_chat` 每轮回复前经 `life.turn_context` 取走和当前话题相关的一两点，写进提示词；关闭本插件或 `xiaoqing_chat` 一侧的开关时，聊天行为与没装时完全一致。

---

## ✨ 她会做什么

| 能力 | 说明 |
|---|---|
| 经历 | 今天几点刷到了什么、她怎么觉得。被问“最近刷到啥好玩的”或话题对上时拿真实经历接话 |
| 流行语 | 词条、含义、常见用法、语气。先保证听得懂，对得上的时候偶尔用一个 |
| 表达方式 | 评论区里的句式和接梗套路，抽象成“当……时：……”，必须附原文摘录 |
| 小课题 | 对某件事起了好奇就留一个小课题，隔天接着查，查完留下一句收获；没查完只能说“在查” |
| 兴趣 | 喜欢的东西越刷越多，随时间衰减，偶尔探索新方向 |
| 查词 | 群里听不懂的词进待查队列，之后由她自己去百科查，查到流行语才记下 |

| 新词条 | 新的、≤8 字的未查实词条自动进待查队列，由百科查实；查不到就保持候选并按期过期。暴力 / 自伤 / 诅咒类说法（灭门、砍死、去死……）不收为词条（只检查词条本身，不影响整条内容） |
| 看视频 | 被她点开的 B 站视频（至多 2 个）取详情、标签、弹幕（按频次与时间点）和热评；她读到的是简介、弹幕和评论区，经历里不声称看完或看到画面 |
| 梗图 | 评论区里像反应表情的图（模型挑的加上确定性预选：点赞不低于该条评论中位数、评论 ≤20 字或没有文字，gif / webp / png 优先于大 jpg，总数受 `max_vision_per_session` 限制），经视觉判定安全后进入 xiaoqing_chat 的表情库（全局可见，来源 `life:<sha256>`） |
| 睡前整理 | 睡前写 2–4 句日记、给查完的小课题写收获、决定继续还是不查了；周日再写周记 |
| 巩固快照 | 兴趣达到 repeated / consistent 时按固定模板生成“最近常看……”，不调模型 |

| 主动分享 | 群聊与白名单私聊的主动开口可以带上她最近真的刷到、想分享的事（`turn_context(mode="proactive")` 返回 `share_candidates`），已分享的经历不再给 |
| 节律 | 醒来后 2 小时内续做小课题 +6；作息活动文字含“刷番”偏向动画话题，含“吃饭”偏向随手刷热榜且条目减半，含“社 / 活动 / 课 / 实验 / 考试”时不上网（关键词表见 `activity_affinity`） |

| 自我强化 | 每次上网的收获（喜欢 − 0.5×不喜欢 + 新确认词条 + 新表达 + 小课题推进 + 梗图，截断到 [−1, 3]）按活动和热榜来源维护 EMA 偏好（α=0.3），进入打分（±8）和来源权重；同一来源连续失败 ≥2 次（含 B 站风控）6 小时内权重 ×0.2 后自动恢复，不重试不绕过；一次 interest_dive 一条喜欢的都没有，给所搜话题记一条 −0.5 的证据（每话题每天一次）；按“工作日 / 周末 × 小时”记录收获，形成习惯后这一刻出发的概率在 0.7～1.3 倍之间浮动。`/life status` 能看到偏好、不稳的来源、习惯和上次决策的因素 |
| 说法的社交反馈 | 她用过的词条 / 表达，之后群友笑、认同或跟着用记一次 ok，没懂记一次 confused（同一条回复同一项只评一次）；ok ≥2 成为口头习惯（推荐冷却减半、优先），confused ≥2 且没有 ok 则 14 天内不再推荐（仍然听得懂）。反馈只从 `turn_context` 请求里按顺序的 `recent` 和 `bot_recent` 算出 |
| 自由探索 | 喜欢的经历带来新话题（视频标签、消化话题、小课题关键词，≤50 个，排除屏蔽词、已是强兴趣的和太泛的栏目名）；interest_dive 的探索部分一半从这个好奇边界抽、一半从探索种子抽；最近 5 次都没探索时 interest_dive +10；热榜预选给与最近 3 天话题不重合的条目一点新鲜感加分 |
| 自我成长 | 小课题睡前整理 done 时收获变成“自己查明白的事”（引用的经历已核对，保留 180 天、上限 100 条）；聊到相关话题给 1 条，问“最近学到啥”给 ≤2 条加一句确定性的成长概括；done 的小课题可以提一个后续问题（全天最多 1 个）开成新小课题，带 `parent_id` 和 `depth`（最多 3 层，规划时 +4）；周记的输入包含本周的 learnings |

聊天侧按意图取用（`turn_context`）：问近况给今天的日记或经历和在查的小课题；问这周给周记 / 近几天日记和快照；问最近迷上什么给快照；问等会 / 今晚 / 明天干嘛给今天剩下的作息时段（≤3 个）和小课题的下一步，标明只是打算；要链接 / 问哪个视频给标题、UP 主和链接（链接只在这时给）；追问她刚提到的那个视频给它的简介摘要。注入的说明只交代“这是什么、拿来干什么”和唯一的硬边界“事实照记录”（谁做的、看到了多少），不堆叠形式要求，其余交给她自己判断；块末一句：这些是依据，不是要讲的清单，打算和做完分开说，兴趣变了要前后都有记录才算。

`turn_context(mode="probe")` 在插件开启时返回 `{"version":1,"online_life":true}`，关闭或不在时是 `None`；xiaoqing_chat 据此在每日生活小事的生成说明里加一条：网上刷到的具体视频和帖子另有真实记录，这里不写。

**不做的事**：不登录任何账号，不发评论、点赞、私信；不执行网页里的任何指令；不把群聊内容上传到外网；不新增第三方依赖。

---

## 🔐 安全与数据边界

- 外部文本（标题、简介、评论）一律是低信任数据：去标签、链接、`@名字`、方括号片段，删“忽略以上……”这类对模型下指令的整句，命中屏蔽词（时政、色情、血腥、赌博、引流、地域与性别对立，加上 `blocked_keywords`）整条丢弃。
- 外部文本只以 JSON 数据块交给模型，提示词里不放在 system 里。
- 模型的输出只是候选。每个词条、表达必须附原文逐字摘录，且摘录确实出现在被引用的评论或条目里；编号越界、伪造摘录、超量的一律丢弃。模型写的总结和她自己的回复不能成为“新学到的东西”的来源。
- 只请求固定域名（`api.bilibili.com`、`api.zhihu.com`、`weibo.com`、`top.baidu.com`、`baike.baidu.com`），不带 Cookie，响应体与 JSON 复杂度有上限。
- 日志只记 ID、数量、原因和错误类型，不记外部原文。
- 聊天侧请求不含任何用户 ID、群号。

---

## ⚙️ 开启

在 `config/config.json` 中开启，并配置 `reasoning` 路由（缺失时上网只记“模型未配置”并退避，不产生任何经历）：

```json
{
  "plugins": {
    "xiaoqing_life": {
      "enabled": true,
      "ai": {
        "routes": {
          "reasoning": { "models": ["deepseek-pro"], "timeout_seconds": 30, "total_timeout_seconds": 90 },
          "vision":    { "models": ["glm-4.6v"] }
        }
      }
    }
  }
}
```

`vision` 路由用于梗图判定（图片以 data URL 送入）；缺失或 PIL 不可用时不收梗图。

### 配置字段

数值越界时钳制到边界，类型错误时回退到默认值。

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关；关闭时定时任务不做事，`turn_context` 返回 `None` |
| `sources` | 见下 | 各来源开关：`bili_popular`、`bili_ranking`、`bili_search`、`bili_comments`、`zhihu_hot`、`weibo_hot`、`baike_lookup` 默认开，`baidu_hot` 默认关 |
| `seed_interests` | 13 个初始兴趣 | 作为搜索词；起始分 1，不算证据 |
| `max_sessions_per_day` | `5` | 每天（配置时区自然日）最多上网次数 |
| `max_llm_calls_per_day` | `24` | 所有模型调用合计，调用前先计数并落盘，失败不退还，重启不清零 |
| `min_session_gap_minutes` | `75` | 两次上网最短间隔 |
| `session_probability` | `0.5` | 满足条件的每次检查实际出发的概率 |
| `activity_window_minutes` / `activity_limit_minutes` | `180` / `45` | 滚动窗口内累计上网时长达到上限就休息 |
| `failure_backoff_minutes` | `60` | 所有来源都失败或模型不可用后的退避 |
| `browse_when_busy` | `false` | 作息为“忙”时是否允许偷偷刷一下 |
| `quiet_hours` | `"00:30-08:00"` | 读不到人设卡〔作息〕时的睡眠时段 |
| `wind_down_minutes` | `60` | 距睡觉多少分钟内只整理、不取新材料 |
| `max_items_per_session` | `6` | 交给消化的条目数 |
| `max_comments_per_item` | `12` | 每条取的热评数（含楼中楼合计）；取评论的 ≤3 个条目按兴趣重合与热度预打分挑选 |
| `max_watch_per_session` | `2` | 每次最多“点开”几个视频取详情 / 标签 / 弹幕 |
| `activity_affinity` | `{anime:[刷番,番], meal:[吃饭,午饭,晚饭,早饭], no_browse:[社,活动,课,实验,考试]}` | 作息活动文字的关键词表：`anime` 偏向动画话题（+10），`meal` 偏向热榜且条目减半（+10），`no_browse` 即使标为空闲也不上网 |
| `learning_keep_days` | `180` | 自己查明白的事保留多少天（上限 100 条） |
| `choice_temperature` | `8` | 选活动的 softmax 温度：分数相近时更随机，调小趋近取最高分 |
| `session_minutes` | 热榜 8–20 / 兴趣 15–35 / 续课题 15–30 / 查词 5–10 | 每种活动名义上的浏览时长（分钟，开始时随机取）；经历时间戳均匀撒在这段时间里，聊天只拿“已经刷到”的 |
| `max_open_threads` / `thread_max_steps` | `2` / `3` | 同时进行的小课题数 / 每个最多续查次数 |
| `episode_detail_days` / `episode_keep_days` | `7` / `45` | 经历细节期 / 保留期 |
| `term_candidate_days` / `term_fading_days` / `term_forget_days` | `14` / `30` / `90` | 词条生命周期 |
| `active_use_probability` | `0.3` | 每轮推一个“可以用”的梗或表达的概率 |
| `suggest_cooldown_hours` | `6` | 同一词条或表达两次推荐的最短间隔 |
| `blocked_keywords` | `[]` | 额外屏蔽词 |
| `memes_enabled` / `meme_auto_approve` | `true` / `true` | 梗图收集；自动通过视觉安全判定，关掉后新图为 pending，需 `/life meme on <短ID>` |
| `max_memes` / `max_meme_bytes` / `max_vision_per_session` | `200` / `1048576` / `2` | 梗图数量上限（淘汰最久未导出的）、单张字节上限、每次最多判定几张（每张 1 次 vision 调用） |

`xiaoqing_chat` 一侧还有 `life.enable_online_life`（默认 `true`）和 `life.online_life_timeout_seconds`（默认 `0.8`），见 [xiaoqing_chat 手册](../xiaoqing_chat/README.md)。

---

## 🕒 作息与节奏

- 读 `xiaoqing_chat` 人设卡的〔作息〕：睡觉不动，上课（忙）默认不动，空闲才可能上网。读不到〔作息〕（`xiaoqing_chat` 不在、人设卡没写或写错）时退回 `quiet_hours`。
- 时间一律用配置时区（`timezone`，默认 `Asia/Shanghai`），不看宿主机时区。
- 距离睡觉不足 `wind_down_minutes` 时不再取新材料；今天有经历且还没整理时做一次睡前整理：写日记、收尾小课题、周日写周记。
- 每 15 分钟检查一次，依次判断：开关、是否已有上网任务、作息、当天次数与模型预算、滚动时长、间隔与失败退避、概率。任何一项不满足就休息，只更新 `last_decision`，不写经历、不调模型。
- 选活动是无模型的可解释打分：续做小课题、刷热榜、按兴趣搜、查词。分数与理由都保存，`/life status` 可查。

---

## 🔧 命令

`/life` 全部子命令只给 Bot 管理员。

| 用法 | 作用 |
|---|---|
| `/life`、`/life status` | 开关、此刻作息、今日次数与模型调用、最近一次决策与理由、各类数量 |
| `/life recent` | 最近 8 条经历 |
| `/life slang` | 词条前 15（状态、来源数、最近看到） |
| `/life threads` | 小课题 |
| `/life memes` | 梗图列表（短 ID、名字、状态） |
| `/life meme on\|off <短ID>` | 开关一张梗图；off 后经 `revoked` 让聊天侧表情库删掉 |
| `/life run` | 立刻安排一次上网：跳过概率、间隔和失败退避，不跳过开关、睡眠和预算 |
| `/life forget <词>` | 删除词条 |

---

## 📁 数据目录

`data/xiaoqing_life/`：`episodes.json`、`terms.json`、`expressions.json`、`interests.json`、`threads.json`、`seen.json`、`snapshots.json`、`journal.json`（日记与周记，保留 8 周）、`memes.json` 与 `memes/<sha256>.<ext>`、`state.json`。全部原子写入并保留 `.bak` 备份；主文件损坏时读备份，都坏时旧文件改名为 `.corrupt` 留底并从空开始。

---

## 🧪 测试

```bash
python -m pytest tests/plugins/xiaoqing_life tests/core/test_app_life_capabilities.py -q
python -m pytest tests/plugins/xiaoqing_chat/test_xiaoqing_chat_online_life.py -q
```

全部离线：假 HTTP、假模型，不访问真实网络。模块与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

### 真实验证

```bash
python scripts/run_xiaoqing_life_session.py --activity hot_scroll --routes-from xiaoqing_chat
python scripts/run_xiaoqing_life_session.py --activity interest_dive --query 天文摄影 --routes-from xiaoqing_chat --data-dir work/xiaoqing_life/x
python scripts/run_xiaoqing_life_session.py --activity wind_down --advance-minutes 15 --routes-from xiaoqing_chat --data-dir work/xiaoqing_life/x
python scripts/run_xiaoqing_life_session.py --no-ai --activity hot_scroll   # 只看取材
```

在隔离数据目录（默认 `work/xiaoqing_life/<时间戳>/`）里用真实端点和真实模型跑一次，打印取材、模型原始 JSON、被本地核对丢弃的条目及原因和落盘结果，并写 `result.json`。只读 `config/config.json` 与 `secrets.json`；`--routes-from xiaoqing_chat` 在内存里复用该插件的 `reasoning` / `vision` 路由。B 站的排行、搜索接口会间歇性触发风控（`-352` / HTTP 412），此时该来源按失败处理并换其它来源，不要高频重试。

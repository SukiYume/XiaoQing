# XiaoQing Life 架构

设计依据见 [DESIGN.md](DESIGN.md)（实现规格）。本文记录已实现部分的模块与数据流。

## 数据流

```text
Core scheduler ── */15 min ──► main.scheduled_tick
                                 │ planner.decide（无模型）：开关 / 作息 / 预算 / 间隔 / 概率 → 打分选活动
                                 ▼
                               rest / wind_down：只写 last_decision
                                 │ 其余
                                 ▼  登记的后台任务（单飞，shutdown 取消）
                               session.run_session
                                 ├─ sources/*    取材（固定域名，有界 HTTP，单来源失败只影响自己）
                                 ├─ seen 过滤    看过的不再消化
                                 ├─ digest       1 次 reasoning（调用前先占预算）→ 严格本地核对
                                 └─ stores       经历、词条、表达、兴趣、小课题、seen、状态

xiaoqing_chat.reply_plan ──► life.turn_context ──► turn_context.py（纯内存，不碰磁盘、网络、模型）
```

定时回调只做同步门控和决策，必须在 Core 60 秒预算内返回；上网本身是 `LifeRuntime.spawn` 登记的后台任务，`shutdown` 取消并等待，再把存储刷盘。

## 模块

| 模块 | 职责 |
|---|---|
| `main.py` | `init` / `shutdown` / `handle`（/life）/ `scheduled_tick` / `turn_context` / `export_memes`；`LifeRuntime` 持有存储、唯一的上网任务与登记的后台任务 |
| `config.py` | `LifeConfig`：读 `config.plugins.xiaoqing_life`，越界钳制，类型错回默认；`route_configured` |
| `persona_bridge.py` | 延迟导入 `xiaoqing_chat` 读人设卡事实与〔作息〕，求距离睡觉的分钟数；读不到退回 `quiet_hours`；时间用配置时区 |
| `safety.py` | 外部文本清洗、注入样句删除、屏蔽词、脏话判断、摘录核对用的归一化 |
| `sources/base.py` | `SourceItem` / `SourceComment` / `SourceError`、`Fetcher` 协议、`BoundedFetcher`（`aiohttp_request_bounded` + `parse_bounded_json`） |
| `sources/bilibili.py` | 热门、排行（前 30）、搜索、热评（含楼中楼与图片地址，评论与楼中楼合计受限） |
| `sources/hotlists.py` | 知乎热榜、微博热搜（只读 `realtime`，跳过 `hotgov` 与广告）、百度热搜（递归取嵌套卡片） |
| `sources/baike.py` | 百科词条卡片；desc / abstract 表明是流行语才算查到 |
| `planner.py` | 门控 `gate`、打分 `choose`、话题选择 `choose_topic`（70% 兴趣加权 / 30% 探索种子，相似话题降权）、`Decision` |
| `digest.py` | 消化提示词（外部文本只在 user 消息的 JSON 里）、编号体系 `i1` / `c1.1` / `c1.1.r1`、本地核对 |
| `session.py` | 一次上网的编排与提交；失败路径只记 `last_failure_at` |
| `lifecycle.py` | 词条 candidate → known → fading → 删除、表达、经历保留、seen、兴趣证据与阶段（纯函数，时间由调用方传入） |
| `threads.py` | 小课题状态机 open → reviewing → done / dropped；`finish` 要求引用已有经历 ID |
| `stores.py` | `LifeStore`：内存快照 + 原子 JSON（`write_with_backup`），一把 `asyncio.Lock` 串行写；损坏恢复；预算 `reserve_llm_call` |
| `turn_context.py` | 聊天侧取用的选择规则与文本块；`unknown_terms` 入队 |
| `commands.py` | `/life` 子命令文本 |

## 关键约定

- **证据**：词条与表达只来自外部材料；同一 `item_id` 不重复计作独立来源；兴趣证据同一 `item_id` 只计一次；`seen` 表里的条目不再消化，所以“回忆”不新增证据。
- **预算**：先 `reserve_llm_call`（计数 +1 并落盘）再调用模型；失败不退还；跨日按配置时区重置。查词（百科）不调模型。
- **休息**：没有候选或门控不满足时只更新 `last_decision`，不写经历、不编造“刚才睡了一觉”。
- **一致性**：`turn_context` 在事件循环里同步读内存；它对 `last_suggested`、`lookup_queue` 的修改随后由登记的后台任务落盘。
- **失败**：所有来源失败、模型未配置 / 失败 / 输出读不懂，本次不产生经历和学习结果；`last_failure_at` 触发 `failure_backoff_minutes` 退避。
- **日志**：`xiaoqing_life step=<步骤>`，只记名称、数量、原因、错误类型。

## 与 Core 的桥

`life.turn_context`、`life.export_memes` 在 `core/models.py` 的服务契约中声明：只有 `xiaoqing_life` 能导出、只有 `xiaoqing_chat` 能调用。`core/capabilities.py:LifeContextService` 校验载荷与返回类型，`core/app_plugin_context.py` 在调用方声明 `uses_services` 时才签发 `capabilities.life_context`。数据只关于小青自己，不按用户身份门控，系统主体也可用。

## 尚未实现

- 梗图下载、视觉判定、存储与导出（`export_memes` 暂返回空页）；`xiaoqing_chat` 一侧的表情库导入与同步任务。
- 睡前整理：日记、reviewing 小课题的收获、兴趣巩固（planner 会在睡前窗口选出 `wind_down`，目前只记决策）。
- 主动开口分享候选（`share_candidates` 暂为空）。
- 真实验证脚本 `scripts/run_xiaoqing_life_session.py`。

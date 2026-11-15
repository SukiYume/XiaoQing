# XiaoQing Memory 架构

本文面向维护者，说明模块划分、读写数据流、和三个调用方之间的桥。功能和配置见 [README.md](README.md)，设计取舍见 [DESIGN.md](DESIGN.md)。

## 数据流

```text
                 每轮取用（纯内存，不调模型）
  chat ──recall──►┌──────────────────────────────────────────┐
  agent ─recall──►│ 常驻块：按预算从页面和笔记里取几行          │
                  ├──────────────────────────────────────────┤
                  │ 页面：人物页 / 群页 / 自我页               │◄─┐
                  ├──────────────────────────────────────────┤  │ 夜间整理
                  │ 笔记：带范围、来源、摘录的原子记录          │◄─┤（写笔记、并页面、
                  ├──────────────────────────────────────────┤  │  核对、清理）
  chat ──record──►│ 事件：只追加的原始记录                     │──┘
  agent ─record──►│                                          │
  life ──record──►└──────────────────────────────────────────┘
```

- **chat → 事件**：`xiaoqing_chat` 的持久化任务落盘一个会话后，`memory/client.py` 的 `push_pending` 把近期历史里还没推送的消息作为事件交给 `memory.record`。幂等键是“重置代际 + 本地消息编号”，推过的键记在 `memory_outbox.json`；推送失败时留在本地，下一次持久化或 `xiaoqing_chat.memory_sync`（每小时 13、43 分）补推。
- **chat → 笔记**：回复投递后的生活记录抽取（`life/extract.py`）把她说出口的近况、打算、承诺、往事和群友的未完话头写成笔记操作，连同还没推送的消息一次提交；每条自我记录都引用她这条发言的事件键，记忆插件核对出处确实是她自己发出的话。
- **agent → 事件**：任务结束时 `xiaoqing_agent/memory.py` 写一条 `task` 事件（原话、补充、执行的命令和结局、最终回复、结局）。记忆插件收到 `task` 事件时自动留一条关于发起人的 `work.task` 笔记。
- **life → 笔记**：`xiaoqing_life` 的定时检查把游标之后的新经历、日记、查明白的事用稳定 ID 发布为 `episode` / `self.diary` / `world.learning`（`global`，来源记为网页文字或整理产出）。
- **夜间整理**：事件 → 笔记 → 页面，再核对、清理。
- **取用**：chat 每轮一次 `recall(purpose="reply")`；主动开口 `proactive`；agent 交接时取她的近况 `self`；agent 开新办事会话时 `task`，结果存进会话，会话内不变。

## 模块

| 模块 | 职责 |
|---|---|
| `main.py` | 入口：`init` / `shutdown`、四个服务回调（`record`、`recall`、`search`、`forget`）、定时检查 `scheduled_tick`、`/mem` 命令；`MemoryRuntime` 持有存储和唯一的整理任务 |
| `config.py` | `config.plugins.xiaoqing_memory` 的解析与钳制；`reasoning` 路由的调用参数 |
| `model.py` | 事件、笔记、页面的数据结构与 JSON 转换；类型、来源、可信度、用法、状态的取值；页面小节和字数上限 |
| `scopes.py` | 范围合法性、可见性（笔记、事件、页面行）、调用方规则（能写什么、作用范围怎么定）、来源链检查 |
| `events.py` | 每个范围按月的 JSONL 事件日志：追加、幂等键去重、编号、读取时跳过坏行、按月删除 |
| `store.py` | 状态文件、按范围懒加载的笔记和页面、每日预算、统计、清空范围 |
| `ops.py` | `memory.record`：事件追加和笔记操作（新增、导入、更新、结束）的逐条核对 |
| `recall.py` | `memory.recall`：各段常驻块的选择、排序和预算，到期话头，自我记录；自我页逐行带上引用的网上生活条目键（`self_lines`），群聊带话题词 |
| `search.py` | `memory.search`：按关键词、对象、类型查笔记、页面和事件 |
| `forget.py` | `memory.forget`：清空范围、删一个人、删指定笔记 |
| `topics.py` | 群里的话题词：清洗、按话题合并（用法取更谨慎的）、保留期和上限；回复时取这两三天可以自然提起的，主动开口时取还记着的全部 |
| `render.py` | 页面行和笔记写成给模型看的文字，标注用法、可信度和时间久了 |
| `commands.py` | `/mem` 的子命令解析和输出 |
| `consolidate/runner.py` | 整理编排：门控、预算、代际核对、四步的顺序和提交、统计 |
| `consolidate/extract.py` | 第一步：事件批次、写笔记的提示词、摘录和说话人核对、去重和改口；群聊顺带产出话题词（出处必须是群友的消息） |
| `consolidate/pages.py` | 第二步：并页面的提示词、页面校验、确定性追加、失去依据的行 |
| `consolidate/verify.py` | 第三步：抽样、核对提示词、判断落到页面和笔记 |
| `consolidate/prune.py` | 第四步：到期、删除、对象上限、事件过期 |
| `consolidate/common.py` | 提示词版本、JSON 解析、事件写成行 |

## 关键约定

- **事件是底座**。事件之上的每一层都能从下一层重建：`/mem rebuild` 从笔记重写页面；笔记和页面记 `prompt_version`，改了提示词可以只重建旧版本的内容。
- **每条笔记带出处**。整理写的笔记引用事件编号并附逐字摘录（空白归一后比较）；她自己的记录引用她发出的那条消息；导入的旧数据记来源键、可信度为 `legacy`。
- **页面每行引用笔记**。读取时一行引用的笔记全部可见、至少一条有效、没有被撤回才显示；清理时依据都不在的行删掉。
- **取用只读内存**。`recall` 不调模型、不等磁盘（首次加载除外）；取用次数只改内存，随定时检查和关闭落盘。
- **写入串行**。同一范围的写入经 `write:<范围>` 异步锁串行；整理经 `consolidate:<范围>` 锁，一个范围同时只有一次整理。
- **代际**。清空范围时代际加一；整理开始时记下代际，提交前核对，期间被清空的结果丢弃。chat 侧推送的幂等键也带重置代际，重置后的新消息照常写入。

## 与 Core 和三个插件的桥

Core 的服务契约（`core/models.py`）固定了四条服务的所有者和调用方：

| 服务 | 调用方 |
|---|---|
| `memory.record` | `xiaoqing_chat`、`xiaoqing_agent`、`xiaoqing_life` |
| `memory.recall` | `xiaoqing_chat`、`xiaoqing_agent` |
| `memory.search` | `xiaoqing_chat` |
| `memory.forget` | `xiaoqing_chat` |

`core/capabilities.py` 的 `MemoryService` 把调用方名字、主体类型和主体所在的范围（群聊 `g<群号>`，私聊 `u<QQ号>`，系统主体为空）写进载荷，覆盖调用方填的同名字段；只声明了的方法可用。插件里用 `context.capabilities.memory`。

各调用方的接入点：

| 插件 | 位置 |
|---|---|
| `xiaoqing_chat` | `memory/client.py`（推送、取用、检索、清空）、`memory/legacy.py`（旧数据导入）、`task_scheduler.schedule_memory_persist`、`reply_plan.recall_for_reply`、`life/extract.py`、`post_reply_tasks._settle_surfaced_open_loop`、`life/proactive.py`、`life/proactive_private.py`、`agent_bridge._agent_recent_self`、`planning/pfc_engine._fetch_pfc_knowledge`、`commands.reset_chat_session` / `handle_memory` |
| `xiaoqing_agent` | `memory.py`；`runtime._prepare` 开新会话时取一次，存进 `Session.memory`；`runtime._run` 收尾时写 `task` 事件 |
| `xiaoqing_life` | `memory_bridge.py`；`main.scheduled_tick` 每次检查后发布 |

## 验证

- 单元与契约测试：`tests/plugins/xiaoqing_memory/`、`tests/core/test_app_memory_capabilities.py`，以及三个调用方各自的 `test_*memory*` 测试。
- 评测：`scripts/chat_eval` 的 `memory` 组；进程内驱动经 `MemoryBridge` 接上真的记忆插件并按 Core 的方式写入调用方和范围，剧本里的 `consolidate()` 立刻整理一次。
- 回放：`scripts/run_xiaoqing_memory_consolidate.py` 在数据副本上跑迁移和整理。

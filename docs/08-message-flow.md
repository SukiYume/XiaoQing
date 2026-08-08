# 🔄 08 - 消息处理流程

本章跟踪一条 OneBot 事件从网络接入到消息发送的完整路径，适合排查连接、路由、会话、插件调用和回复投递问题。

---

## 🔄 流程总览

```text
OneBot Event
  → 网络接入与鉴权
  → 事件模型校验
  → MessageContext
  → Dispatcher A–G 流程
  → 插件执行 gate
  → 插件返回值标准化
  → OneBot Action
  → HTTP / WebSocket 投递
  → 发送回执与指标
```

---

## 🌐 1. 网络接入

### 被动 Inbound

- `POST /event` 接收 HTTP 事件。
- `WebSocket /ws` 接收 WebSocket 事件并发送 Action。
- 两个入口共享 `inbound_token`、接纳队列和会话排序。

请求进入处理队列前完成：

1. Listener 代际状态检查
2. Bearer token 校验
3. 请求体与字节预算
4. JSON 解析
5. OneBot 事件模型校验
6. 当前 token revision 复验

### 主动 WebSocket

`OneBotWsClient` 连接 `onebot_ws_uri`，读取事件 JSON，并将其交给同一应用分发入口。连接任务负责退避、抖动、配置切换和关闭回收。

---

## 🔄 2. 事件标准化

`OneBotEvent` 校验常用字段：

```json
{
  "post_type": "message",
  "message_type": "group",
  "user_id": 123456789,
  "group_id": 987654321,
  "message_id": 456,
  "message": [
    {"type": "text", "data": {"text": "/help"}}
  ],
  "sender": {
    "user_id": 123456789,
    "role": "member"
  }
}
```

字符串消息与消息段列表会归一为标准消息段。`core.message` 提取文本、引用、@、图片、face、mface 和其他媒体信号。

---

## 🧩 3. MessageContext

Dispatcher 从事件构建 `MessageContext`。关键字段：

| 字段 | 说明 |
|---|---|
| `event` | 标准化 OneBot 事件 |
| `text` | 消息段提取的文本 |
| `clean_text` | Bot 名称与命令前缀处理后的文本 |
| `user_id` | 当前用户 |
| `group_id` | 当前群，私聊为空 |
| `message_id` | OneBot 消息 ID |
| `has_command_prefix` | 原消息包含命令前缀 |
| `has_prefix` | 原消息包含命令前缀或 Bot 名称 |
| `is_private` | 私聊标记 |
| `is_at_me` | @Bot 标记 |
| `is_reply_to_bot` | 引用 Bot 消息标记 |
| `principal` | 用户、群角色和管理员能力 |
| `request_id` | 本轮追踪 ID |

Bot 名称和命令前缀按配置顺序处理。例如：

```text
输入：小青 /help pendo
Bot 名称处理：/help pendo
命令前缀处理：help pendo
路由输入：help pendo
```

---

## 🔄 4. 处理门控

Step A 根据以下信号决定消息是否进入业务流程：

- 私聊
- 命令前缀
- Bot 名称
- @Bot
- 活动 Session
- 配置允许的群聊普通消息
- Smalltalk Provider 的观察需求

群聊静音状态在此阶段参与普通聊天与 URL 路径判断。命令与会话按各自权限和场景继续处理。

---

## 🔄 5. Dispatcher A–G 流程

### A. 处理门控

完成消息上下文、群聊规则、身份和静音判断。

### B. URL 解析

`clean_text` 是单个 HTTP/HTTPS URL 时，Dispatcher 调用 URL Provider。URL 先通过安全网络边界，再进入内容提取。

### C. Bot 名称回应

用户消息只包含 Bot 名称时，Dispatcher 生成名称回应或交给 Smalltalk Provider 的名称场景。

### D. 活跃会话

当前用户键存在 Session 时，Dispatcher 调用会话所属插件：

```python
await plugin.handle_session(text, event, context, session)
```

同一会话键串行执行。插件通过 `update_session()` 提交工作副本，或通过 `end_session()` 结束流程。

### E. 命令

Router 从命令目录解析顶层触发词和递归子命令：

```text
/pendo todo add 写周报
  → plugin: pendo
  → root: pendo
  → child: todo
  → leaf: add
  → arguments: 写周报
```

调用前完成：

1. 命令与别名匹配
2. 子命令上下文继承
3. 私聊或群聊场景校验
4. Bot 管理员或群管理员权限校验
5. 插件代际授权
6. 插件执行 gate 接纳

解析结果通过 `context.command_invocation` 传给插件。

### F. 命令兜底

带命令前缀的文本在命令目录中缺少匹配项时，Dispatcher 返回帮助提示和查询入口。

### G. Smalltalk Provider

普通聊天候选消息进入配置的 `smalltalk_provider`。`xiaoqing_chat` 可在内部执行 attention、频率控制、行为规划、AI 生成和 reply checker。

观察入口接收普通聊天候选消息。命令、URL、Session 和命令前缀文本在前述阶段完成处理。

---

## 🔄 6. Router

Router 的命令目录来自已验证 Manifest。每个节点包含：

- 稳定 code
- 规范名与别名
- 用法与帮助
- 权限
- 场景
- 正确样例与错误样例
- 直接子节点

启动和插件重载时，Router 检测同优先级顶层触发词冲突。目录发布完成后保持只读，`/help`、JSON 导出、权限校验和插件调用共享同一份数据。

---

## 💬 7. Session 路径

会话键：

```text
私聊：(user_id, None)
群聊：(user_id, group_id)
```

Session 流程：

```text
create_session()
  → SessionManager 保存初始值
  → 后续事件命中 Step D
  → get_session() 返回隔离快照
  → update_session(callback) 原子提交
  → end_session() 或空闲超时结束
```

Session 由单个插件拥有。插件名用于选择 `handle_session()`，用户键用于串行与隔离。

---

## 🧩 8. 插件执行

命令、Session、Smalltalk、URL、调度和声明式服务调用都进入插件运行时授权。执行链：

```text
插件代授权
  → admission queue
  → concurrency gate
  → timeout / circuit policy
  → handler
  → result normalization
  → generation release
```

`parallel` 插件按配置并发，`sequential` 插件单并发。同步函数通过 `run_sync()` 进入独立同步预算。

---

## 🔄 9. 返回值标准化

插件入口可返回：

- 字符串
- OneBot 消息段列表
- OneBot Action 列表
- 空结果

Core 将字符串转换为文本段，将消息段转换为当前用户或群的 Action，并对文件、图片、语音与长文本执行边界检查。

---

## 🔄 10. 出站投递

### HTTP Action

`OneBotHttpSender` 向 `onebot_http_base` 发送 OneBot Action，并使用可选 `onebot_token`。

### WebSocket Action

主动 WebSocket 连接或 Inbound WebSocket 会话可承载 Action。广播路径受 worker 数量、队列和单连接预算控制。

### 回执

Delivery 层将投递结果返回给回执对象。需要 commit-after-ack 的插件在发送成功后提交提醒、任务或通知状态。

---

## 🔄 11. 并发与排序

### Inbound 接纳

HTTP 与 WebSocket 入口共享按会话键调度器：

- 同一私聊用户按接纳顺序串行。
- 同一群与用户组合按接纳顺序串行。
- 各会话键可并行。
- `inbound_ws_max_workers` 配置范围为 `1..128`，控制活动处理 worker。
- `ws_queue_size` 配置范围为 `1..10000`，控制等待 backlog。
- 总接纳容量为 worker 与 backlog 之和，HTTP 与 WebSocket 共用同一个有界调度器。

### Dispatcher

`max_concurrency` 限制全局活动消息处理数。

### 插件

`plugin_execution` 限制每个插件的入口队列、并发、超时、熔断和同步任务。

### 配置切换

Listener、token、管理员状态与插件 Context 按 revision 发布。队列项在执行前复验当前授权状态。

---

## 💬 12. 静音语义

`/闭嘴` 为当前群设置普通回复暂停时间，`/说话` 清除该状态。静音影响普通聊天和 URL 自动解析；管理员命令、允许的业务命令与活动 Session 继续按自身契约处理。

`context.get_mute_remaining(group_id)` 返回分钟。

---

## 🩺 13. 观测与排障

每轮事件关联 request ID。建议按以下顺序检查日志：

1. Inbound 或主动 WebSocket 接收记录
2. token 与事件模型校验
3. MessageContext 门控信号
4. Dispatcher 当前阶段
5. Router 命令 code 与权限结果
6. 插件代、队列与 handler
7. OneBot Action 与发送回执

`/metrics` 提供消息数、插件调用、错误、延迟和队列指标。

---

## 🛠️ 关键代码位置

| 阶段 | 文件 |
|---|---|
| Inbound HTTP/WS | `core/server.py` |
| 主动 WebSocket 与 HTTP Action | `core/onebot.py` |
| 事件与消息段模型 | `core/models.py`, `core/message.py` |
| MessageContext 与 A–G 流程 | `core/dispatcher.py` |
| 命令目录与解析 | `core/router.py` |
| Session | `core/session.py` |
| 插件代与执行 | `core/plugin_generation.py`, `core/plugin_runtime.py`, `core/plugin_execution.py` |
| 出站回执 | `core/delivery.py`, `core/app_delivery.py` |
| 配置 revision | `core/config.py`, `core/app_config_apply.py` |

---

## 🧭 下一步

- 组件关系：[系统架构](02-architecture.md)
- 配置与网络边界：[配置详解](06-configuration.md)
- 插件入口：[插件开发指南](03-plugin-development.md)

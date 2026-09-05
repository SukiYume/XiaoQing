# 💬 Chat

`chat` 通过 Coze API v3 提供单轮 AI 对话。命令面向 Bot 管理员，插件同时发布 `chat.reply` 服务，供清单授权的 `smalltalk` 插件调用。

---

## 🔐 使用条件

- 已创建并发布 Coze 智能体；
- 已取得 Coze 个人访问令牌和智能体 ID；
- Core 已加载 `plugins/chat/plugin.json`；
- `smalltalk` 调用服务时，其插件 ID 位于服务调用者白名单中。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| Coze 单轮对话 | `/chat` | `/gpt` `/ai` |
<!-- manifest-command-aliases:end -->

```text
/chat <问题>
/chat help
```

问题去除首尾空白后，长度范围为 1～2000 个字符。`help` 和 `帮助` 显示本地帮助。

---

## ⚙️ 配置与凭据

在 `config/secrets.json` 中填写 Coze 凭据和可选代理：

```json
{
  "plugins": {
    "chat": {
      "token": "pat_your-coze-api-token",
      "bot_id": "your-coze-bot-id",
      "proxy": ""
    }
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `token` | 是 | Coze 个人访问令牌 |
| `bot_id` | 是 | 已发布智能体的 ID |
| `proxy` | 否 | `aiohttp` 代理地址；空字符串表示直连 |

在 `config/config.json` 中设置每日额度：

```json
{
  "plugins": {
    "chat": {
      "daily_user_limit": 20,
      "daily_global_limit": 100
    }
  }
}
```

两个额度均接受 `1`～`1000000` 的整数。业务日期由 Core 的全局 `timezone` 配置决定。

---

## 🔄 调用与配额

一次调用依次创建对话、轮询状态、读取消息，并返回第一条非空 `answer` 文本。创建、轮询和消息读取共享 30 秒总时限；插件最多同时处理 2 个 Coze 调用。超时清理使用独立的 3 秒预算取消远端对话。

额度在远端调用前原子预留，在答案成功返回后提交。网络错误、协议错误、超时、任务取消和空答案会释放预留额度。配额状态保存在：

```text
data/chat/chat_quota.json
```

线程中的预留写入已经开始时，取消流程会等待它产生结果，再释放对应预留。该补偿覆盖“调用方尚未收到预留令牌”的窗口。

`chat.reply` 服务复用同一套配置、额度和远端调用流程。该服务接收 `smalltalk` 提供的文本与已签名调用上下文。

---

## 🔐 数据与隐私

插件将 `bot_id` 与调用者 ID 组合后计算 SHA-256 摘要，并向 Coze 发送前 32 位十六进制字符作为用户标识。日志记录查询长度、状态、消息数量和安全错误码；问题、答案、令牌、代理凭据与调用者标识保留在日志边界之外。

所有 Coze 请求复用 Core 提供的 HTTP 会话，并执行响应大小、JSON 深度、MIME、重定向和解压上限检查。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示缺少配置 | 核对 `token` 与 `bot_id` 的层级、拼写和内容 |
| 提示达到额度 | 核对两项每日额度及 Core 的 `timezone` |
| 请求超时 | 检查 Coze 服务、代理连通性和运行日志中的安全错误码 |
| `smalltalk` 调用被拒绝 | 核对 `plugin.json` 的服务调用者白名单与调用签名 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/chat tests/plugins/chat/test_chat.py
python -m mypy plugins/chat
python -m pytest -q tests/plugins/chat/test_chat.py \
  tests/plugins/contracts/test_public_error_redaction.py \
  tests/plugins/contracts/test_configured_http_clients.py \
  tests/plugins/contracts/test_plugin_resource_lifecycle.py \
  tests/tooling/test_bounded_http_adoption.py
```

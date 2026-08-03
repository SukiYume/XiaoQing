# chat

`chat` 是基于 Coze API v3 的单轮 AI 对话插件。命令默认仅允许 Bot 管理员使用，同时声明 `chat.reply` 服务供 `smalltalk` 插件按白名单调用。

## 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | 等价别名 | 权限 |
| --- | --- | --- | --- |
| AI 对话 | `/chat` | `/gpt`、`/ai` | 管理员 |
<!-- manifest-command-aliases:end -->

使用 `/chat <问题>` 发起对话，使用 `/chat help` 或 `/chat 帮助` 查看本地帮助。

查询不能为空，去掉首尾空白后最多 2000 个字符。`help` 或 `帮助` 作为第一个参数时显示本地帮助，不会调用远端 API。

## 配置

凭证和代理属于敏感配置，写入 `config/secrets.json`：

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

- `token`：Coze 个人访问令牌，必填；
- `bot_id`：已发布智能体的 ID，必填；
- `proxy`：可选的 aiohttp 代理地址，空字符串表示不使用代理。

这些值必须是无首尾空白、无控制字符且长度受限的字符串。旧示例中的 `user` 从未参与真实身份隔离，`stream` 也会被实现固定覆盖，因此已经删除。

每日额度不是密钥，写入 `config/config.json`：

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

两个额度都必须是 `1` 到 `1000000` 的整数，布尔值、字符串和浮点数不会被隐式转换。未配置时分别使用 20 次/用户/日和 100 次/全局/日。日期边界采用全局 `timezone`；时区不可用时安全回退到 `Asia/Shanghai`。

## 调用流程

插件按照 Coze 当前非流式 v3 契约执行：

1. `POST /v3/chat` 创建对话，使用 `additional_messages` 传入问题；
2. 若状态为 `created` 或 `in_progress`，按 1 秒间隔调用 `GET /v3/chat/retrieve`；
3. 状态为 `completed` 后，通过 `GET /v3/chat/message/list` 获取消息；
4. 返回第一条非空的 `answer` 文本。

请求固定为非流式，并按 Coze 契约保存本轮历史，供轮询与消息列表接口读取；插件不传 `conversation_id`，所以每条命令仍是彼此隔离的单轮对话。QQ 用户 ID 不会直接发送给 Coze；插件将 `bot_id` 与调用者 ID 组合后做 SHA-256，只发送前 32 位十六进制摘要。当前协议依据 [Coze 官方 Python SDK](https://github.com/coze-dev/coze-py) 的 v3 实现。

所有请求均复用应用提供的 HTTP 会话，并经过仓库统一的有界响应、JSON 深度、MIME、禁止重定向和解压上限检查。最多同时发出 2 个 Coze 调用；取得并发槽位后，创建、轮询和消息读取共享 30 秒总 deadline，每次 HTTP 请求只使用当时的剩余预算，不会在临近截止时重新获得完整 30 秒。超时后会在独立的 3 秒清理预算内尽力取消远端对话。

## 配额事务

调用远端前，插件会在共享状态中原子预留用户额度和全局额度。以下情况都会回滚预留：

- HTTP、MIME、JSON 或 Coze 业务错误；
- 轮询超时或终态不是 `completed`；
- 消息列表畸形；
- 没有可用的文本答案；
- 任务取消或其他异常。

只有成功取得并返回答案后才提交额度。配额按业务日期自动换窗，并通过有界键锁防止并发请求超额。

## 错误和日志

日志只记录查询长度、状态、消息数量和安全错误码，不记录调用者标识、问题正文、答案正文、令牌、代理凭证或远端错误消息。返回给用户的意外错误由统一公开错误边界生成，不会直接回显异常内容。

插件不执行隐式重试；调用失败后额度会恢复，用户可以自行重试。这样可以避免一次命令在付费或限额 API 上产生不可见的重复调用。

## 文件结构

```text
chat/
├── __init__.py   # 包说明
├── main.py       # 配置、配额、Coze v3 调用和命令入口
├── plugin.json   # 命令、服务与权限声明
└── README.md     # 本文档
```

`init()` 是加载器要求的统一生命周期钩子；本插件没有需要预热的资源，因此有意保持无操作。`reply()` 则是清单声明的服务适配器，只把 `smalltalk` 的文本和已签名上下文交给同一命令实现。

## 验证

在仓库根目录运行：

```powershell
python -m ruff check plugins/chat tests/plugins/test_chat.py
python -m pytest -q tests/plugins/test_chat.py `
  tests/plugins/test_public_error_redaction.py `
  tests/plugins/test_configured_http_clients.py `
  tests/plugins/test_plugin_resource_lifecycle.py `
  tests/test_bounded_http_adoption.py
```

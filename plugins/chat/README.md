# Chat 插件

AI 对话插件，提供与 AI 的对话功能（基于 Coze API）。

## 功能介绍

Chat 插件集成了 Coze AI API，允许用户与 AI 进行自然语言对话。

## 使用方法

### 基本命令

```
/chat <消息内容>
/chat help
```

### 示例

```
/chat 你好，今天天气怎么样？
/chat 介绍一下黑洞的形成原理
/chat help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "chat": {
      "token": "your_coze_api_token",
      "bot_id": "your_bot_id"
    }
  }
}
```

### 配置项说明

- `token` - Coze API 访问令牌（必需）
- `bot_id` - Coze Bot ID（必需）

## 功能特性

- 支持自然语言对话
- 流式响应（实时返回）
- 自动错误处理和重试
- 查询长度限制保护
- 超时控制

## API 信息

- API 端点: https://api.coze.com/open_api/v2/chat
- 请求超时: 30 秒
- 最大查询长度: 2000 字符

## 注意事项

- 需要先在 Coze 平台创建 Bot 并获取凭证
- 查询内容不能超过 2000 字符
- API 调用可能产生费用，请注意使用量
- 网络延迟可能影响响应速度

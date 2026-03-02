# XiaoQing Chat 插件

XiaoQing 智能对话插件，提供高级 AI 对话和上下文管理功能。

## 功能介绍

XiaoQing Chat 是一个增强版的 AI 对话插件，支持多轮对话、上下文记忆、个性化设置等高级功能。

## 使用方法

### 基本命令

```
/chat <消息>             # AI 对话
/chat history            # 查看对话历史
/chat clear              # 清空对话历史
/chat persona <角色>     # 设置 AI 角色
/chat help               # 显示帮助信息
```

### 示例

```
/chat 介绍一下黑洞
/chat history
/chat clear
/chat persona scientist
/chat help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "api_key": "your_api_key",
      "model": "gpt-4",
      "max_tokens": 2000,
      "temperature": 0.7,
      "max_history": 10,
      "system_prompt": "你是 XiaoQing，一个友好的 AI 助手。"
    }
  }
}
```

### 配置项说明

- `api_key` - OpenAI API 密钥（必需）
- `model` - 使用的模型（默认: gpt-3.5-turbo）
- `max_tokens` - 最大生成 token 数（默认: 2000）
- `temperature` - 创造性参数（0-2，默认: 0.7）
- `max_history` - 保留的历史对话轮数（默认: 10）
- `system_prompt` - 系统提示词

## 功能特性

- 多轮对话支持
- 上下文记忆
- 对话历史管理
- 角色设定
- 流式响应
- 个性化配置
- 多用户独立会话

## 预设角色

- `default` - 通用助手
- `scientist` - 科学家（专注科学问题）
- `poet` - 诗人（文学风格）
- `teacher` - 教师（教育风格）
- `friend` - 朋友（随意聊天）

## 对话历史

- 每个用户独立的对话历史
- 自动保留最近 N 轮对话
- 可手动清空历史
- 支持查看历史记录

## API 使用

支持 OpenAI 兼容的 API：
- OpenAI
- Azure OpenAI
- 其他兼容 API

## 注意事项

- API 调用可能产生费用
- 注意 token 使用量
- 对话历史占用内存
- 敏感信息不要泄露给 AI
- 遵守 API 使用条款

## 隐私说明

- 对话内容会发送到 AI API 服务商
- 本地会保存对话历史
- 定期清理敏感对话
- 不要分享包含隐私的对话

## 依赖

- openai (OpenAI Python SDK)

安装依赖：
```bash
pip install openai
```

## 适用场景

- 知识问答
- 创意写作
- 代码解释
- 学习辅导
- 日常闲聊
- 问题解决

# Smalltalk 闲聊

插件处理 bot 名称触发、非命令闲聊、管理员维护的 QA，以及可选的 Chat/Voice provider。不存在笑话命令。

## 管理员 QA 命令

- `/记忆 <问题> <回答>`（别名：`记住`、`学习`）
- `/对话 [问题]`
- `/删除对话 <问题> [回答]`

三个命令均为 `admin_only`。QA 按群或私聊用户 scope 隔离，使用原子持久化、审计记录及问题/回答/条数配额；普通用户不能写入 QA。

未命中 QA 时，插件通过核心 `call_plugin` provider 调用已加载的 `chat`，不会直接导入另一份插件模块。回复可按 `plugins.smalltalk.voice_probability` 概率通过 `voice` provider 转成语音；未加载 provider 或调用失败时安全降级。

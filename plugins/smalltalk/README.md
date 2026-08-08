# 💬 Smalltalk

`smalltalk` 提供机器人名字短回复、管理员维护的精确问答，以及通过 `chat.reply` 服务生成的基础闲聊。可选的 `voice.synthesize_text` 服务会按概率把纯文本回复转换为语音。

---

## ⌨️ Dispatcher 入口

Core 在 `config/config.json` 中选择闲聊 provider：

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}
```

消息通过命令、Session、静音和 Dispatcher 门控后，Core 才调用 `handle_smalltalk`。选择 `xiaoqing_chat` 时，相应回落消息交给 XiaoQing Chat。

---

## ⌨️ 管理员 QA 命令

```text
/记忆 <问题> <回答>
/记住 <问题> <回答>
/学习 <问题> <回答>
/对话 [问题]
/删除对话 <问题> [回答]
```

三组 Manifest 命令均为 Bot 管理员入口。

| 项目 | 规则 |
| --- | --- |
| 问题 | 第一个非空白 token，最长 128 个字符 |
| 回答 | 可包含空格，最长 1000 个字符 |
| 单问题回答 | 最多 20 个不同文本，命中时随机选择 |
| 单作用域问题 | 最多 2000 个 |
| `/对话 <问题>` | 精确匹配完整问题 |
| 列表输出 | 2800 字符预算，并显示省略数量 |

群聊 QA 按群号共享，私聊 QA 按用户号隔离：

```text
data/smalltalk/QA_group_<群号>.json
data/smalltalk/QA_private_<用户号>.json
data/smalltalk/QA_audit.json
```

主文件更新由进程内锁和原子写保护。变更审计最多保存 5000 条，记录操作与问题摘要。群号和用户号校验在作用域创建前完成。

---

## 💬 名字短回复

只喊机器人名字时，插件按以下顺序选择回复集合：

1. `data/smalltalk/小青.json` 的 `小青` 数组；
2. `data/smalltalk/responses.json` 的 `responses` 数组；
3. 内置短回复。

每个集合最多接收 200 条唯一非空字符串，单条上限为 1000 个字符。加载器会过滤结构、类型、长度和重复值。

---

## 💬 Chat 服务

精确 QA 命中时直接返回本地回答。其余闲聊通过 Core 签发的 `context.capabilities.chat_reply` 调用 `chat.reply`，并传递当前 `user_id` 与 `group_id` actor 字段。

服务权限由 `smalltalk` Manifest 的 `uses_services` 和 `chat` Manifest 的调用者白名单共同约束。provider 异常或消息段校验异常时，用户收到固定降级提示，日志记录脱敏错误类别。

---

## 🎨 语音概率

在 `config/config.json` 中配置：

```json
{
  "plugins": {
    "smalltalk": {
      "voice_probability": 0.2
    }
  }
}
```

字段接受 0～1 的有限数字，默认值为 `0.2`。命中概率且回复是 3000 字符以内的纯文本时，插件调用 `voice.synthesize_text`。混合媒体与语音服务异常会保留原文本回复。

---

## 🔐 并发与隐私

插件 Manifest 使用 `parallel`。QA 文件写入由各文件锁串行化，远端 Chat 调用沿用 `chat` 插件的并发与额度边界。

日志记录操作、长度、数量、状态和错误类别。QA 回答、闲聊正文、语音文本和 actor 标识保留在普通日志边界之外。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 回落消息交给另一插件 | 核对 `plugins.smalltalk_provider` |
| QA 精确查询为空 | 核对作用域、问题 token 和写入管理员身份 |
| 闲聊返回降级提示 | 检查 `chat.reply` 服务授权、Chat 凭据和额度 |
| 回复保持文本 | 核对 `voice_probability`、纯文本长度与 Voice 服务 |
| QA 状态文件异常 | 备份对应 JSON 后检查结构与写权限 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/smalltalk tests/plugins/test_smalltalk.py
python -m mypy plugins/smalltalk
python -m pytest -q tests/plugins/test_smalltalk.py -n 2
```

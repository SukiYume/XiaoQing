# 💬 Smalltalk

`smalltalk` 提供机器人名字短回复、管理员维护的精确问答，以及通过 `chat.reply` 服务生成的基础闲聊。可选的 `voice.synthesize_text` 服务会按概率把纯文本回复转换为语音。

---

## ⌨️ Dispatcher 入口

闲聊 provider 在 `config/config.json` 中选择，省略时为 `smalltalk`：

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}
```

消息先经过命令、会话和静音等处理，剩下的才交给闲聊 provider。取值 `smalltalk` 时由本插件处理（见下文）。取值 `xiaoqing_chat` 时这些消息交给 XiaoQing Chat，群里没有点名机器人的普通消息也会进入闲聊。

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

变更审计最多保存 5000 条，记录操作、作用域、操作者和问题原文（至多 128 字）。

---

## 💬 名字短回复

只喊机器人名字时，插件按以下顺序选择回复集合：

1. `data/smalltalk/小青.json` 的 `小青` 数组。
2. `data/smalltalk/responses.json` 的 `responses` 数组。
3. 内置短回复。

文件内容形如：

```json
{
  "小青": ["在的在的", "嗯？"]
}
```

```json
{
  "responses": ["在的在的", "嗯？"]
}
```

键名固定为 `小青` 和 `responses`，不随 `bot_name` 变化。每个集合最多接收 200 条唯一非空字符串，单条上限为 1000 个字符，不合格的条目会被忽略。

---

## 💬 Chat 服务

精确 QA 命中时直接返回本地回答。其余闲聊调用 `chat` 插件的 `chat.reply` 服务，并把当前用户和群一并传入。调用失败或返回内容异常时，用户收到固定提示“暂时无法回复，请稍后再试~”。

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

字段接受 0～1 的有限数字，默认 `0.2`；取值非法时关闭语音转换。命中概率且回复是纯文本、长度不超过 `voice` 插件的 500 字符上限时，插件调用 `voice.synthesize_text`。包含图片等非文本内容的回复，以及语音服务出错时，保留原文本回复。

---

## 🔐 并发与隐私

闲聊回复调用 `chat` 插件，并发和额度受 `chat` 插件限制。

日志只记录操作、长度、数量、状态和错误类别，不记录 QA 回答、闲聊正文、语音文本以及用户和群标识。

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
python -m ruff check plugins/smalltalk tests/plugins/smalltalk/test_smalltalk.py
python -m mypy plugins/smalltalk
python -m pytest -q tests/plugins/smalltalk/test_smalltalk.py -n 2
```

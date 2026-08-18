# 🧭 Bot Core

Bot Core 提供命令帮助、运行时重载、插件列表、群静音、敏感配置管理和运行指标。

---

## 🔐 使用条件

- `/help` 与 `/plugins` 面向群聊和私聊用户。
- `/reload` 与 `/metrics` 需要 Bot 管理员身份。
- 群静音命令需要 Bot 管理员身份和群聊场景。
- secret 命令需要 Bot 全局管理员私聊与 `secret_admin` capability。

---

## ⌨️ 命令

| 命令 | 场景与权限 | 功能 |
|---|---|---|
| `/help [查询] [page N]` | 公开 | 分层浏览或搜索命令目录 |
| `/help json [查询] [page N]` | 公开 | 导出结构化命令目录 |
| `/reload` | Bot 管理员 | 重载配置与插件，并在完成后通知结果与耗时 |
| `/plugins` | 公开 | 已加载插件列表 |
| `/闭嘴 [时长]` | Bot 管理员群聊 | 暂停当前群普通回复 |
| `/说话` | Bot 管理员群聊 | 恢复当前群普通回复 |
| `/set_secret <路径> <值>` | 全局管理员私聊 | 更新已有 secret 路径 |
| `/get_secret <路径>` | 全局管理员私聊 | 查看脱敏值或对象键名 |
| `/metrics` | Bot 管理员 | 运行时间、调用量、成功率和慢插件 |

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
|---|---|---|
| 帮助 | `/help` | `/h` `/帮助` `/commands` `/命令目录` `/catalog` |
| 重载 | `/reload` | `/重载` |
| 插件列表 | `/plugins` | `/插件` |
| 群静音 | `/闭嘴` | `/shutup` `/mute` `/安静` |
| 群恢复 | `/说话` | `/speak` `/unmute` |
| 写入 secret | `/set_secret` | `/setsecret` `/设置密钥` |
| 查询 secret | `/get_secret` | `/getsecret` `/查看密钥` |
| 指标 | `/metrics` | `/stats` `/性能` `/指标` |
<!-- manifest-command-aliases:end -->

完整子命令、权限、场景与错误样例可通过 `/help bot_core` 查看。

---

## ⌨️ 分层帮助

帮助系统为手机阅读提供逐层导航：

```text
/help                         # 功能域与插件
/help pendo                   # 插件一级入口
/help pendo todo              # 直接子命令
/help pendo todo add          # 叶节点详情
/help pendo.pendo.todo.add    # 稳定命令 code
/help search 提醒             # 关键词搜索
/help json pendo              # JSON 目录
```

总览、插件页、分支页和搜索结果均支持 `page N`。叶节点集中显示用法、别名、权限、场景、正确样例和错误样例。长用法在参数边界换行。

---

## 📌 重载

`/reload` 先发布配置 revision，再请求 Core 创建插件后台重载任务。首条消息确认配置已发布且插件开始后台重载。后台任务等待当前 `bot_core` 调用释放执行 gate，随后扫描、校验和发布插件代；结束后向原管理员会话发送一次成功或失败结果及耗时。重复命令复用同一后台任务时只登记一条完成通知。

外部工具保存完整有效的 `secrets.json` 且公开配置与当前已确认版本一致时，watcher 保留现有可信运行代并暂存候选。Core 私聊当前管理员列出新增、删除和修改的字段路径，消息内容仅包含路径；`/reload` 重新读取磁盘并确认候选。公开配置变更和双来源整体替换在停服窗口内完成，保留独立受保护 Inbound 的实例也可在文件稳定后通过该通道执行 `/reload`。

---

## 💬 群静音

空参数使用 10 分钟。支持分钟、小时与中文单位：

```text
/闭嘴
/闭嘴 30
/闭嘴 30m
/闭嘴 30min
/闭嘴 1.5h
/闭嘴 2小时
/闭嘴 30分钟
```

有效范围为大于 0 且至多 24 小时。`/说话` 清除当前群静音。静音作用于普通聊天与 URL 自动解析，业务命令和活动 Session 按各自契约处理。

---

## ⚙️ Secret 管理

### 路径

路径由字母、数字、下划线或连字符组成的片段构成，片段通过 `.` 分隔：

```text
plugins.signin.yingshijufeng.sid
plugins.demo.api-key
admin_user_ids
```

`set_secret` 更新 secrets 树中的已有路径。实际写入由 `SecretAdminService` 与 `ConfigManager` 完成，成功提交后发布新 revision。

该命令适合服务运行期间更新现有 API Key、token 和插件凭据。新增 secret 路径可写入完整有效的 `secrets.json`，核对管理员收到的字段摘要后通过 `/reload` 确认。

### 值

标准 JSON 值会按原类型保存，其他输入按字符串保存：

```text
/set_secret plugins.demo.enabled true
/set_secret plugins.demo.retries 3
/set_secret plugins.demo.tags ["a", "b"]
/set_secret plugins.demo.options {"mode": "safe"}
/set_secret plugins.demo.token plain-text-token
```

### 脱敏展示

| 值类型 | 展示方式 |
|---|---|
| 短字符串 | `****` |
| 长字符串 | 前后各 2 字符，中间 `****` |
| 数字与布尔值 | `****` |
| 列表 | 元素数量 |
| 对象 | 键数量或最多 20 个直属键名 |
| 其他类型 | `[hidden]` |

命令参数使用脱敏日志边界。生产运维请在受控管理员私聊中执行，并在聊天记录暴露风险出现后轮换凭据。

---

## 🛡️ 运行指标

`/metrics` 摘要包含：

- 运行时间
- 总调用数
- 成功率
- 平均耗时
- 慢调用数
- 错误数
- 最多 5 个最慢插件

缺省或畸形观测值显示为 `n/a`。插件名称经过换行和空白清理，指标服务状态会显示在回复中。

---

## 💾 数据与生命周期

Bot Core 使用 Core 配置、指标和静音服务。插件自身拥有轻量命令逻辑，运行数据由对应 Core 服务管理。

---

## 🩺 排障

1. 使用 `/help bot_core` 检查当前命令目录。
2. 使用 `/plugins` 检查加载状态。
3. 使用 `/metrics` 检查插件错误与延迟。
4. 使用日志 request ID 跟踪重载、权限和 secret revision。

---

## ✅ 开发验证

```bash
python -m pytest -q \
  tests/plugins/bot_core/test_bot_core.py \
  tests/plugins/contracts/test_internal_log_redaction.py \
  tests/plugins/contracts/test_public_error_redaction.py
python -m ruff check plugins/bot_core tests/plugins/bot_core/test_bot_core.py
python -m mypy plugins/bot_core
```

# 📱 Pendo Scriptable 小组件

Pendo 提供 iPhone Scriptable 只读摘要，将近期日程、待办、财务或笔记显示在主屏。

---

## 🔐 接口与权限

| 项目 | 值 |
|---|---|
| 摘要接口 | `GET /api/widget/summary` |
| 模块参数 | `section=tasks|ledger|notes|all|auto` |
| 鉴权 | `Authorization: Bearer <widget_token>` |
| 权限范围 | `GET /api/widget/*` |
| 默认期限 | 365 天 |

`tasks`、`ledger` 与 `notes` 返回日程和一个指定面板，`all` 返回日程及三个面板。`auto` 按小时在 `tasks → ledger → notes` 之间轮换。Widget Token 是只读 Bearer 凭据，与浏览器 Cookie 会话使用独立权限范围。

---

## 📌 生成与吊销 Token

在管理员私聊中执行：

```text
/pendo web widget-token
```

默认期限由 `WEB_WIDGET_TOKEN_EXPIRE_SECONDS` 定义。每次签发生成独立 `jti`，服务端在 `pendo.db` 中保存摘要、用户、签发时间和绝对到期时间。

吊销当前用户的 Widget Token：

```text
/pendo web widget-revoke
```

浏览器 Cookie 会话拥有独立记录和吊销边界。

---

## 📌 Scriptable 脚本

脚本位于：

```text
plugins/pendo/web/scriptable/pendo_widget.js
```

设置自己的 Pendo Web 地址：

```javascript
const BASE_URL = normalizeBaseUrl('https://example.com/pendo');
```

常见地址：

- 本机：`http://127.0.0.1:12001`
- 反向代理子路径：`https://example.com/pendo`

首次在 Scriptable App 中运行时，脚本显示安全输入框。粘贴 `/pendo web widget-token` 返回的 Token 后，脚本将其写入 iOS Keychain。401 响应会触发 Keychain 条目清理，下一次 App 内运行会再次显示输入框。

脚本源码只保存 `BASE_URL`，Token 保存在 Keychain。

---

## 📌 摘要布局

日程范围为未来 30 天，最多返回 5 条；待办、财务和笔记面板各自最多返回 5 条。三种组件尺寸使用以下固定数据与布局契约：

| 尺寸 | 请求参数 | 展示内容 |
|---|---|---|
| `small` | 固定请求 `section=auto` | 日程摘要和日程列表 |
| `medium` | 读取组件参数 `tasks`、`ledger`、`notes` 或 `auto` | 左侧日程，右侧一个面板 |
| `large` | 固定请求 `section=all` | 日程、待办、财务、笔记四个区域 |

`small` 与 `large` 使用固定 section；组件参数只控制 `medium`。非法或空的 medium 参数回退为 `auto`。三种尺寸共享标题、时间、正文和状态字号层级。

---

## 📌 iPhone 设置

1. 安装 Scriptable。
2. 新建脚本并粘贴 `pendo_widget.js`。
3. 设置 `BASE_URL`。
4. 在 QQ 私聊执行 `/pendo web widget-token`。
5. 在 Scriptable App 内运行脚本，并将 Token 存入 Keychain。
6. 在 iPhone 主屏添加 Scriptable 小组件。
7. 选择 Pendo 脚本。
8. 使用中号组件时，在组件参数中填写 `tasks`、`ledger`、`notes` 或 `auto`。

自定义服务端口时，同时更新 `config.plugins.pendo.web_port` 与脚本 `BASE_URL`。

---

## 📌 iOS 日历同步

脚本顶部的日历名称控制同步目标：

```javascript
const SYNC_CALENDAR_NAME = 'Pendo';
```

在系统日历 App 中创建同名且可写的日历，并允许 Scriptable 访问日历。将 `SYNC_CALENDAR_NAME` 设为空字符串可关闭同步。

同步仅在 Scriptable App 内直接运行脚本时执行，主屏组件刷新只读取摘要。脚本复用本次接口返回的最多 5 条日程，在当天起 30 天范围内按“标题 + 开始时间”识别已有事件，并采用仅新增策略：

- 有明确时间的日程默认持续 1 小时；有效结束时间优先使用接口值。
- 仅含日期的日程写为持续 1 天的全天事件。
- 地点写入 Calendar 事件，备注写入 `[由 Pendo Widget 同步]` 标记。
- 已有同键事件、摘要范围外事件和日历中的其他事件保持原状。

直接运行结束后，Scriptable 通过通知和控制台输出新增数与跳过数。目标日历缺失时，通知提示需要创建的日历名称。

---

## 📌 交互

- 点击日程区域打开 Pendo 日程页。
- 点击待办、财务或笔记区域打开对应模块页。
- 数据编辑在 Pendo Web 页面完成。
- iOS 管理实际刷新频率，脚本提供建议刷新时间。

---

## 🔐 安全建议

- 使用 HTTPS 反向代理访问公网 Pendo Web。
- 将 Widget Token 视为个人只读数据凭据。
- 手机转移、丢失或凭据暴露后执行 `/pendo web widget-revoke`。
- 通过 Pendo 日志检查用户、`jti`、到期时间和吊销结果。

Pendo 的完整 Web 鉴权与数据模型见 [插件 README](../plugins/pendo/README.md) 和 [架构说明](../plugins/pendo/ARCHITECTURE.md)。

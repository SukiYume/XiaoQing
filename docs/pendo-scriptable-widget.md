# 📱 Pendo Scriptable 小组件

Pendo 提供 iPhone Scriptable 只读摘要，将近期日程、待办、财务或笔记显示在主屏。

---

## 🔐 接口与权限

| 项目 | 值 |
|---|---|
| 摘要接口 | `GET /api/widget/summary` |
| 日历对账接口 | `GET /api/widget/calendar?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` |
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

在脚本顶部填写 Pendo Web 地址和 Widget Token：

```javascript
const BASE_URL = normalizeBaseUrl('https://example.com/pendo');
const TOKEN = 'PASTE_WIDGET_TOKEN_HERE';
```

常见地址：

- 本机：`http://127.0.0.1:12001`
- 反向代理子路径：`https://example.com/pendo`

`TOKEN` 填写 `/pendo web widget-token` 返回的完整值。接口返回 401 时，在 QQ 私聊生成新 Token，并替换脚本顶部的 `TOKEN`。

---

## 📌 摘要布局

日程范围为未来 30 天，最多返回 5 条；待办、财务和笔记面板各自最多返回 5 条。三种组件尺寸使用以下固定数据与布局契约：

| 尺寸 | 请求参数 | 展示内容 |
|---|---|---|
| `small` | 固定请求 `section=auto` | 日程摘要和日程列表 |
| `medium` | 读取组件参数 `tasks`、`ledger`、`notes` 或 `auto` | 左侧日程，右侧一个面板 |
| `large` | 固定请求 `section=all` | 日程、待办、财务、笔记四个区域 |

`small` 与 `large` 使用固定 section；组件参数只控制 `medium`。非法或空的 medium 参数回退为 `auto`。三种尺寸共享标题、时间、正文和状态字号层级。

财务摘要按币种分别返回和展示收入、支出及余额，金额保留对应币种标识。当前接口提供原币汇总；汇率换算需要额外的数据来源与规则。

---

## 📌 iPhone 设置

1. 安装 Scriptable。
2. 在 QQ 私聊执行 `/pendo web widget-token`。
3. 新建脚本并粘贴完整的 `pendo_widget.js`。
4. 设置脚本顶部的 `BASE_URL` 和 `TOKEN`。
5. 在 Scriptable App 内运行脚本，确认摘要能够加载。
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

同步仅在 Scriptable App 内直接运行脚本时执行，主屏组件刷新只读取最多 5 条的摘要。直接运行会通过日历接口取得完整窗口。带 `Pendo-ID` 标记的事件按条目 ID 识别；带同步标记但尚无 ID 的旧事件可按“标题 + 开始时间”认领一次。无同步标记的用户事件不会被修改或删除。

同步窗口由 Keychain 中的成功日游标控制：

- 首次成功运行查询过去 30 天到未来 30 天，建立完整的近期日历窗口。
- 后续运行始终至少回看过去 30 天，并查询到脚本运行日之后 30 天；若上次成功运行更早，则从该游标开始补齐间隔期间的日程。
- 单次接口窗口最多 3660 天；保存时间更早的游标从该上限覆盖的日期开始查询。
- 接口查询与 iOS 目标日历查询各执行一次；游标在接口读取和全部日历对账成功后推进。
- 目标日历缺失、接口失败或事件保存失败时保留原游标，下一次运行继续处理同一窗口。

日历写入采用按 Pendo-ID 对账策略：

有时刻的日程通过带 UTC 偏移的 ISO 时间表示真实时刻，设备按该时刻写入日历。服务端使用日程时间轴生成偏移，覆盖用户与设备时区不同以及夏令时回拨的重复墙钟时间。全天日程保留纯日期语义。

- 有明确时间的日程默认持续 1 小时；有效结束时间优先使用接口值。
- 仅含日期的日程写为持续 1 天的全天事件。
- 地点写入 Calendar 事件，备注写入 `[由 Pendo Widget 同步]` 和 `Pendo-ID` 标记。
- 同一 `Pendo-ID` 已存在时原地更新标题、开始时间、结束时间、全天状态和地点；字段一致时不重复保存。
- 本次完整窗口内，服务端已删除或已移出窗口的托管事件会被删除，同一 `Pendo-ID` 的重复副本也会清理。
- 只有同时带同步标记和 `Pendo-ID` 的事件参与自动删除；无标记事件、无 ID 旧事件和窗口外事件保持原状。

直接运行结束后，Scriptable 通过通知和控制台输出新增、更新、删除、未变化与跳过数量。目标日历缺失时，通知提示需要创建的日历名称。

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
- Scriptable 脚本源码包含 Widget Token；脚本副本与导出文件需要按个人凭据管理。
- 手机转移、丢失或凭据暴露后执行 `/pendo web widget-revoke`。
- 通过 Pendo 日志检查用户、`jti`、到期时间和吊销结果。

Pendo 的完整 Web 鉴权与数据模型见 [插件 README](../plugins/pendo/README.md) 和 [架构说明](../plugins/pendo/ARCHITECTURE.md)。

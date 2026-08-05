# Pendo Scriptable 小组件

Pendo 提供 Scriptable 只读摘要接口，可以把日程、待办、财务或笔记放到 iPhone 主屏，适合每天扫一眼当前事项。

## 接口与鉴权

- 摘要接口：`GET /api/widget/summary`
- 参数：`section=tasks|ledger|notes|auto`
- `auto` 会按小时轮换：`tasks -> ledger -> notes`
- 鉴权：`Authorization: Bearer <widget_token>`

`widget token` 只允许访问 `/api/widget/*` 的 `GET` 请求，不能拿去访问普通 Web 页面或写接口。

## 生成 widget token

在聊天中执行：

```text
/pendo web widget-token
```

默认有效期为 365 天，来自 `plugins/pendo/config.py` 中的
`WEB_WIDGET_TOKEN_EXPIRE_HOURS`。令牌每次签发都有独立 `jti`，服务端会在 Pendo 数据库中
持久化登记。如果手机丢失或令牌可能泄漏，执行：

```text
/pendo web widget-revoke
```

该命令会吊销当前用户所有尚未过期的 Widget Token，不影响其他用户或 Web Cookie 会话。

## Scriptable 脚本

脚本文件位于 `plugins/pendo/web/scriptable/pendo_widget.js`。

使用前只修改 Web 地址：

```javascript
const BASE_URL = "https://example.com/pendo";
```

仓库脚本不包含 Token 常量。首次在 Scriptable App 内直接运行脚本时，
它会弹出安全输入框；粘贴私聊收到的 Token 后，值会写入 iOS Keychain，
不会写回脚本或 iCloud 同步的源码。接口返回 401 时，脚本会删除失效的
Keychain 条目，下次在 App 内运行时可录入新令牌。
`BASE_URL` 应改成你自己的 Pendo Web 地址，例如：

- `http://127.0.0.1:12001`
- `https://example.com/pendo`

## 当前摘要行为

- 左侧日程：未来 30 天内的事件，最多 5 条
- 右侧面板：待办 / 财务 / 笔记摘要，最多 5 条
- `medium`：紧凑双栏，适合主屏常驻
- `large`：和 `medium` 同风格，但每条会显示更多细节
- `small`：自动退化为极简摘要
- 三种尺寸都使用同一套字号层级：标题略大于 item 内容

## iPhone 设置步骤

1. 安装 `Scriptable`
2. 新建脚本并粘贴 `plugins/pendo/web/scriptable/pendo_widget.js`
3. 把 `BASE_URL` 改成你的 Pendo Web 地址
4. 在 Scriptable App 内直接运行一次脚本，将 `/pendo web widget-token` 私聊生成的值存入 Keychain
5. 长按主屏幕，添加 `Scriptable` 小组件
6. 选择这个脚本
7. 可选填写参数：
   - `tasks`
   - `ledger`
   - `notes`
   - `auto`

Windows 部署下默认端口启动失败时，可以先修改服务端 `config/config.json` 中的 `plugins.pendo.web_port`，再把 `BASE_URL` 同步成新的端口。

## 交互说明

- 点击左侧日程区域会跳到 Pendo 日程页
- 点击右侧摘要区域会跳到当前模块页
- 不能在 widget 内直接勾选任务或做原地 tab 切换
- 刷新频率由 iOS 控制，脚本只会设置建议刷新时间

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

默认有效期来自 `plugins/pendo/config.py` 中的 `WEB_WIDGET_TOKEN_EXPIRE_HOURS`。

## Scriptable 脚本

脚本文件位于 `plugins/pendo/web/scriptable/pendo_widget.js`。

使用前修改两个常量。

```javascript
const BASE_URL = "https://example.com/pendo";
const TOKEN = "PASTE_WIDGET_TOKEN_HERE";
```

仓库中的脚本默认只保留占位值，不包含任何真实地址或 token。
`BASE_URL` 应改成你自己的 Pendo Web 地址，例如：

- `http://127.0.0.1:8765`
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
4. 把 `TOKEN` 改成 `/pendo web widget-token` 生成的值
5. 长按主屏幕，添加 `Scriptable` 小组件
6. 选择这个脚本
7. 可选填写参数：
   - `tasks`
   - `ledger`
   - `notes`
   - `auto`

Windows 部署下默认 `8765` 端口启动失败时，可以先改服务端环境变量 `PENDO_WEB_PORT`，再把 `BASE_URL` 同步成新的端口。

## 交互说明

- 点击左侧日程区域会跳到 Pendo 日程页
- 点击右侧摘要区域会跳到当前模块页
- 不能在 widget 内直接勾选任务或做原地 tab 切换
- 刷新频率由 iOS 控制，脚本只会设置建议刷新时间

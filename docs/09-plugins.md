# 🧩 09 - 插件功能介绍

本章按插件分类整理 XiaoQing 中可直接加载的功能、命令和配置。

> [!NOTE]
> 本章主要介绍可直接加载和使用的内置插件。`plugins/xiaoqing_chat/memory/` 这类辅助子包，以及 `plugins/memo_deprecated/` 这类默认不加载的停用目录，不单独展开。

## 📑 目录

- [🧩 09 - 插件功能介绍](#-09---插件功能介绍)
  - [📑 目录](#-目录)
  - [🏠 核心插件](#-核心插件)
    - [bot\_core - 核心命令](#bot_core---核心命令)
      - [命令列表](#命令列表)
      - [使用示例](#使用示例)
    - [pendo - 个人时间与信息管理中枢](#pendo---个人时间与信息管理中枢)
      - [核心特性](#核心特性)
      - [命令列表](#命令列表-1)
      - [配置说明](#配置说明)
      - [使用示例](#使用示例-1)
      - [定时任务](#定时任务)
      - [注意事项](#注意事项)
    - [echo - 回显示例](#echo---回显示例)
  - [💬 聊天插件](#-聊天插件)
    - [xiaoqing\_chat - 小青拟人聊天](#xiaoqing_chat---小青拟人聊天)
      - [核心特性](#核心特性-1)
      - [命令列表](#命令列表-2)
      - [配置项](#配置项)
      - [记忆系统说明](#记忆系统说明)
      - [使用说明](#使用说明)
      - [配置为默认聊天插件](#配置为默认聊天插件)
    - [smalltalk - 闲聊插件](#smalltalk---闲聊插件)
      - [命令列表](#命令列表-3)
      - [使用示例](#使用示例-2)
      - [配置说明](#配置说明-1)
    - [chat - AI 对话](#chat---ai-对话)
      - [使用示例](#使用示例-3)
    - [voice - 语音功能](#voice---语音功能)
      - [功能特性](#功能特性)
      - [使用示例](#使用示例-4)
  - [🔭 天文科学](#-天文科学)
    - [apod - 每日天文图](#apod---每日天文图)
      - [定时任务](#定时任务-1)
    - [arxiv\_filter - arXiv 论文筛选](#arxiv_filter---arxiv-论文筛选)
      - [定时任务](#定时任务-2)
      - [技术说明](#技术说明)
    - [chime - FRB 重复暴监测](#chime---frb-重复暴监测)
      - [定时任务](#定时任务-3)
    - [dict - 天文学词典](#dict---天文学词典)
      - [使用示例](#使用示例-5)
    - [ads\_paper - 论文与文献管理](#ads_paper---论文与文献管理)
      - [核心特性](#核心特性-2)
      - [命令列表](#命令列表-4)
      - [支持的论文 ID 格式](#支持的论文-id-格式)
      - [配置说明](#配置说明-2)
      - [使用示例](#使用示例-6)
      - [注意事项](#注意事项-1)
    - [astro\_tools - 天文计算工具箱](#astro_tools---天文计算工具箱)
      - [功能列表](#功能列表)
      - [使用示例](#使用示例-7)
      - [依赖库](#依赖库)
    - [color - 颜色查询](#color---颜色查询)
      - [参数选项](#参数选项)
      - [使用示例](#使用示例-8)
  - [🛠️ 实用工具](#️-实用工具)
    - [choice - 随机选择](#choice---随机选择)
      - [参数选项](#参数选项-1)
      - [使用示例](#使用示例-9)
    - [wolframalpha - 万能计算器](#wolframalpha---万能计算器)
      - [特殊后缀](#特殊后缀)
      - [使用示例](#使用示例-10)
    - [codex - Codex 后台任务队列](#codex---codex-后台任务队列)
    - [shell - 终端命令](#shell---终端命令)
      - [功能特性](#功能特性-1)
      - [安全设置](#安全设置)
      - [使用示例](#使用示例-11)
    - [url\_parser - 链接解析](#url_parser---链接解析)
    - [qingssh - SSH 远程控制](#qingssh---ssh-远程控制)
      - [连接管理逻辑（核心机制）](#连接管理逻辑核心机制)
      - [交互与隔离示例](#交互与隔离示例)
      - [命令列表](#命令列表-5)
      - [使用示例](#使用示例-12)
      - [高级功能：用户名指定](#高级功能用户名指定)
      - [配置说明](#配置说明-3)
      - [注意事项](#注意事项-2)
  - [🌐 外部服务](#-外部服务)
    - [github - GitHub Trending](#github---github-trending)
      - [参数选项](#参数选项-2)
      - [定时任务](#定时任务-4)
      - [使用示例](#使用示例-13)
    - [earthquake - 地震快讯](#earthquake---地震快讯)
      - [定时任务](#定时任务-5)
    - [signin - 自动签到](#signin---自动签到)
      - [支持平台](#支持平台)
      - [配置说明](#配置说明-4)
      - [定时任务](#定时任务-6)
      - [使用示例](#使用示例-14)
    - [twitter - Twitter 图片](#twitter---twitter-图片)
      - [配置说明](#配置说明-5)
      - [功能特性](#功能特性-2)
      - [定时任务](#定时任务-7)
      - [使用示例](#使用示例-15)
    - [jupyter - 代码执行](#jupyter---代码执行)
      - [功能特性](#功能特性-3)
      - [使用示例](#使用示例-16)
    - [adnmb - A岛匿名版](#adnmb---a岛匿名版)
      - [使用示例](#使用示例-17)
  - [🎮 娱乐游戏](#-娱乐游戏)
    - [qingpet - QQ群宠物养成系统](#qingpet---qq群宠物养成系统)
      - [核心特性](#核心特性-3)
      - [命令列表](#命令列表-6)
      - [定时任务](#定时任务-8)
      - [使用示例](#使用示例-18)
    - [guess\_number - 猜数字游戏](#guess_number---猜数字游戏)
      - [难度选择](#难度选择)
      - [游戏流程](#游戏流程)
      - [游戏中命令](#游戏中命令)
      - [其他命令](#其他命令)
      - [功能特性](#功能特性-4)
    - [minecraft - MC 服务器通信](#minecraft---mc-服务器通信)
      - [功能特性](#功能特性-5)
      - [使用示例](#使用示例-20)
      - [定时任务](#定时任务-9)
  - [📊 插件统计](#-插件统计)
  - [🔗 另请参阅](#-另请参阅)

---

## 🏠 核心插件

### bot_core - 核心命令

核心系统命令，包括帮助、插件管理、静音控制等。

#### 命令列表

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `help` | `/help`, `/h`, `/帮助` | 查看帮助信息 | ❌ |
| `plugins` | `/plugins`, `/插件` | 查看已加载插件列表 | ❌ |
| `reload` | `/reload`, `/重载` | 热重载配置和插件 | ✅ |
| `metrics` | `/metrics`, `/指标` | 查看运行指标统计 | ✅ |
| `闭嘴` | `/闭嘴`, `/shutup`, `/mute` | 群内静音一段时间 | ❌ |
| `说话` | `/说话`, `/speak`, `/unmute` | 解除群内静音 | ❌ |
| `set_secret` | `/set_secret`, `/设置密钥` | 修改密钥配置 | ✅ |
| `get_secret` | `/get_secret`, `/查看密钥` | 查看密钥配置 | ✅ |

#### 使用示例

```
/help                    # 显示所有命令帮助
/help 天文               # 搜索包含"天文"的命令
/闭嘴 30                 # 静音 30 分钟
/闭嘴 1h                 # 静音 1 小时
/reload                  # 热重载所有插件
```

---

### pendo - 个人时间与信息管理中枢

个人时间与信息管理插件，支持日程、待办、笔记、日记、账本、提醒、搜索、统计和 **Web 控制台**（FastAPI + 原生 JS SPA）。

> `/pendo` 会显示带 emoji 的模块导航帮助；输入 `/pendo <模块>`（如 `/pendo event`、`/pendo ledger`）可直达对应分组帮助。

Pendo 的长期文档入口是 `plugins/pendo/README.md` 和 `plugins/pendo/ARCHITECTURE.md`：前者面向使用和部署，后者面向维护和二次开发。本章提供命令速查和功能索引。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **AI 智能解析** | 日程添加自动识别时间、地点、提醒设置 |
| **多轮对话** | 支持会话式操作 |
| **隐私保护** | 支持群聊隐私模式，敏感内容转私聊 |
| **提醒** | 支持单次、重复、多节点、提前确认和延后提醒 |
| **定时任务** | 每日简报、日记提醒、待办顺延、财务周报/月报和 demo 数据清理 |
| **导出和迁移** | 聊天端 Markdown 档案导出；Web 端 `.pendo.zip` Bundle 导入导出 |
| **撤销功能** | 支持短时间内的操作撤销 |
| **全文搜索** | 跨模块搜索日程、待办、笔记、日记和账本 |
| **Web 控制台** | FastAPI + 原生 JS SPA，JWT 鉴权，高级数据迁移、十个页面和聚合统计 |
| **Scriptable 小组件** | 提供 `/api/widget/summary` 只读摘要接口和专用 widget token，适合 iPhone 主屏显示 |

#### 命令列表

**日程管理 (Event)**

| 命令 | 说明 |
|------|------|
| `/pendo event add <内容>` | 添加日程（AI 解析） |
| `/pendo event list [范围]` | 查看日程 |
| `/pendo event view <id>` | 查看单次日程、重复实例、多节点节点或集合详情 |
| `/pendo event delete <id>` | 删除日程 |
| `/pendo event edit <id> <内容>` | 统一编辑单次 / 重复 / 多节点事件；修改时间时未发送提醒会自动同步平移 |
| `/pendo event reminders [id\|范围]` | 查看提醒 |

**待办事项 (Todo/Task)**

| 命令 | 说明 |
|------|------|
| `/pendo todo add` | 交互式添加待办，只询问内容和计划日期 |
| `/pendo todo add <内容> [plan:日期] [deadline:时间] [remind:时间] [cat:分类] [p:1-5] [#标签]` | 快捷添加待办，可带高级参数 |
| `/pendo todo list [today/open/done/cancelled/overdue/upcoming/inbox/分类]` | 查看待办 |
| `/pendo todo done <id>` | 完成待办 |
| `/pendo todo cancel <id>` | 取消待办 |
| `/pendo todo undone <id>` | 重开待办 |
| `/pendo todo delete <id\|cat:分类>` | 删除待办 |
| `/pendo todo edit <id> <内容>` | 编辑待办 |

**笔记 (Note)**

| 命令 | 说明 |
|------|------|
| `/pendo note add <内容> [cat:分类] [#标签] [ref:条目ID]` | 记录笔记 |
| `/pendo note list [分类名\|cat:分类] [#标签] [since:范围]` | 查看笔记 |
| `/pendo note view <id>` | 查看笔记详情 |
| `/pendo note edit <id> <内容>` | 编辑标题、正文、分类和标签 |
| `/pendo note append <id> <内容>` | 追加正文 |
| `/pendo note tag/untag <id> #标签` | 添加或移除标签 |
| `/pendo note link <id> <关联条目ID>` | 建立条目关联 |
| `/pendo note delete <id\|cat:分类>` | 删除笔记 |

**日记 (Diary)**

| 命令 | 说明 |
|------|------|
| `/pendo diary add [日期] <内容>` | 写一篇日记；同一天可多篇 |
| `/pendo diary list [范围]` | 查看日记列表 |
| `/pendo diary view <日期或ID>` | 查看某天所有日记或某篇详情 |
| `/pendo diary template` | 查看所有模板 |
| `/pendo diary <模板ID>` | 使用模板写日记 |
| `/pendo diary delete <日期或ID>` | 删除日记；同天多篇时按ID删除 |

**记账 (Ledger)**

| 命令 | 说明 |
|------|------|
| `/pendo ledger add` | 进入交互式记账；先手动输入金额和描述，后续类型、账户、分类等可按数字选择 |
| `/pendo ledger add <金额> <描述> [cat:分类] [in\|out\|transfer] [account:账户] [to:账户] [merchant:商户] [date:日期] [remark:备注]` | 快捷单行记账；默认支出、其他分类、现金账户、今天 |
| `/pendo ledger quick <金额> <描述> ...` | 快速记账别名，参数同 `ledger add <金额> <描述> ...` |
| `/pendo ledger list [范围] [type:expense/income/transfer] [account:账户] [to:账户] [merchant:商户] [cat:分类] [amount:min..max]` | 查看账目列表 |
| `/pendo ledger view <id>` | 查看账目详情 |
| `/pendo ledger edit <id> <字段:值>...` | 编辑账目 |
| `/pendo ledger delete <id>` | 删除账目 |
| `/pendo ledger summary [范围]` | 收支汇总统计 |

> 别名：`bill`、`finance`、`记账`、`账单`（如 `/pendo 记账 add`）

**搜索 (Search)**

| 命令 | 说明 |
|------|------|
| `/pendo search <关键词>` | 全文搜索 |
| `/pendo search <关键词> type=event/task/note/diary/ledger` | 按类型搜索 |
| `/pendo search <关键词> range=last7d/2026-01` | 按时间范围搜索 |
| `/pendo search <关键词> status=open/done/cancelled` | 按待办状态筛选 |
| `/pendo search <关键词> category=<分类>` | 按分类筛选 |
| `/pendo search <关键词> transaction_type=income/expense/transfer` | 按账目类型筛选（记账） |

**提醒操作**

| 命令 | 说明 |
|------|------|
| `/pendo confirm <id>` | 确认提醒 |
| `/pendo event reminders confirm <id> [today\|future\|all\|提醒时间]` | 提前确认未发送提醒，不再发出 |
| `/pendo snooze <id> <时间>` | 延后提醒（10m, 1h, 19:00） |

**导入导出**

| 命令 | 说明 |
|------|------|
| `/pendo export <文件名> [范围] [类型]` | 聊天端导出单文件 Markdown 档案 |
| Web 迁移页导入/导出 Bundle | 导入/导出 `.pendo.zip` 数据包，支持预览、冲突策略和审计日志 |

> **提示：聊天端只保留 Markdown 档案导出；数据迁移和恢复请使用 Web 端 Bundle 流程。**

**设置 (Settings)**

| 命令 | 说明 |
|------|------|
| `/pendo settings view` | 查看当前设置 |
| `/pendo settings reminder on/off` | 开关提醒 |
| `/pendo settings timezone <时区>` | 设置时区 |
| `/pendo settings quiet_hours <开始>-<结束>` | 静默时段 |
| `/pendo settings daily_report <时间>` | 每日简报时间 |
| `/pendo settings diary_remind <时间>` | 日记提醒时间 |
| `/pendo settings privacy on/off` | 开关隐私模式 |

**Web 控制台**

| 命令 | 说明 |
|------|------|
| `/pendo web token` | 生成登录令牌（JWT，用于浏览器登录） |
| `/pendo web widget-token` | 生成 Scriptable 小组件令牌（只读） |
| `/pendo web start` | 启动 Web 服务 |
| `/pendo web stop` | 停止 Web 服务 |
| `/pendo web status` | 查看运行状态和访问地址 |

> pendo 插件初始化时会尝试自动启动 Web 服务；如果启动失败，仍可用 `/pendo web start` 手动重试。默认监听 `127.0.0.1:12001`，可通过环境变量 `PENDO_WEB_HOST` / `PENDO_WEB_PORT` 调整。

Web 控制台提供以下页面：

| 页面 | 功能 |
|------|------|
| Dashboard | 核心数据汇总（待办、事件、账本余额、最近笔记） |
| 任务 | Kanban 看板，按优先级管理待办 |
| 事件 | 日历视图，查看/添加日程 |
| 账本 | 收支记录、分类筛选、余额统计、快速录入 |
| 日记 | 时间线视图，按日期浏览日记 |
| 笔记 | 卡片网格，按分类/标签浏览笔记 |
| 搜索 | 跨模块全文搜索 |
| 统计 | 活跃度、任务、账本、日记、事件等聚合图表 |
| 设置 | 在线修改配置 |
| 迁移 | **高级数据迁移（导入/导出 Bundle）**、冲突策略和操作日志 |

**其他操作**

| 命令 | 说明 |
|------|------|
| `/pendo undo [分钟]` | 撤销删除（默认 5 分钟内） |

#### 配置说明

在 `secrets.json` 中配置：

```json
{
  "plugins": {
    "pendo": {
      "api_base": "https://your-llm-api.com/v1",
      "api_key": "your-llm-api-key",
      "model": "gpt-4o-mini"
    }
  }
}
```

#### 使用示例

**1. 添加日程（AI 智能解析）**

```
/pendo                                        # 查看完整帮助总览
/pendo event                                  # 只看 event 模块帮助
/pendo event add 3月8日下午两点，国自然截止，提前一周和一天提醒
/pendo event add 每月18号上午十点，公积金提取，重复7个月
/pendo event list today        # 查看今日日程
/pendo event list 2026-03     # 查看三月日程
/pendo event list last7d      # 查看最近7天
/pendo event edit 80efbef6 会议开始改成4月22日12:43，备注从北京南坐G123去会场
```

**2. 待办管理**

```
/pendo todo add 完成论文初稿 p:1              # 紧急待办
/pendo todo add 整理数据 cat:工作 p:2         # 工作分类高优先级
/pendo todo list today                        # 今日待办
/pendo todo list 工作 done                    # 工作分类已完成
/pendo todo done abc12345                     # 完成指定待办
```

**3. 笔记管理**

```
/pendo note add 直接折叠找脉冲星 cat:工作 #文章
/pendo note list cat:工作                    # 查看工作笔记
/pendo note list #文章                       # 查看带文章标签的笔记
/pendo note view abc12345                    # 查看详情
```

**4. 日记管理**

```
/pendo diary add 今天完成了论文初稿        # 写今天日记
/pendo diary add 2026-02-01 昨天很充实    # 补写日记
/pendo diary add mood:happy score:8 favorite:true 今天推进顺利
/pendo diary list week                       # 查看本周日记
/pendo diary view 2026-02-01                 # 查看详情
/pendo diary template                        # 查看模板
```

**5. 搜索功能**

```
/pendo search 脉冲星                         # 全文搜索
/pendo search 脉冲星 type=note              # 只搜笔记
/pendo search 论文 range=last7d              # 最近7天
/pendo search 报告 status=open              # 只看未完成待办
/pendo search 餐 category=餐饮              # 按分类筛选
/pendo search 外卖 transaction_type=expense # 记账支出搜索
```

**6. 提醒操作**

```
/pendo confirm abc12345                       # 确认提醒
/pendo event reminders confirm ea66203d today   # 今天未发送的提醒不再发送
/pendo event reminders confirm ea66203d future  # 未来全部未发送提醒不再发送
/pendo snooze abc12345 10m                   # 延后10分钟
/pendo snooze abc12345 19:00                 # 延后到19点
```

**7. 数据导入导出**

```
/pendo export 我的档案                      # 导出所有数据
/pendo export 工作回顾 last7d event,todo    # 导出最近7天日程和待办
/pendo export 账本快照 2026-03 ledger       # 导出指定月份账本
# 导入和 Bundle 迁移在 Web 迁移页完成
```

**8. 设置管理**

```
/pendo settings view                         # 查看设置
/pendo settings privacy on                  # 开启隐私模式
/pendo settings diary_remind 21:30          # 设置日记提醒时间
```

**9. 撤销操作**

```
/pendo undo                                  # 撤销最近5分钟内的删除
/pendo undo 10                               # 撤销最近10分钟内的删除
```

**10. 记账管理**

```
/pendo ledger add 35 午餐 account:微信       # 一条消息记账；默认支出、其他分类、现金账户、今天
/pendo ledger add                            # 进入交互式记账：金额、描述手动输入，后续按数字选择
/pendo ledger add 35 午餐 account:微信 merchant:食堂
/pendo ledger add 100 兼职收入 in account:支付宝
/pendo ledger add 1000 还款 transfer account:微信 to:招行
/pendo ledger add 20 咖啡 cat:餐饮 account:现金
/pendo ledger quick 35 午餐 account:微信      # quick 别名同样可用
/pendo ledger list                           # 查看本月账目
/pendo ledger list week                      # 查看本周
/pendo ledger list 2026-03                   # 查看三月账目
/pendo ledger list type:income               # 只看收入
/pendo ledger list account:微信              # 只看某个账户
/pendo ledger list cat:餐饮                  # 只看餐饮分类
/pendo ledger summary                        # 本月收支汇总
/pendo ledger summary last7d                 # 最近7天汇总
/pendo 记账 add                             # 用中文别名也可以
```

**11. Web 控制台**

```
/pendo web token                             # 获取浏览器登录令牌
/pendo web widget-token                      # 获取 Scriptable 小组件令牌
/pendo web start                             # 启动 Web 服务（默认端口 12001）
/pendo web status                            # 查看访问地址
/pendo web stop                              # 停止服务
```

启动后默认通过 `http://127.0.0.1:12001` 访问。反向代理部署时，也可以通过自己的外网地址访问。浏览器登录用 `/pendo web token`，iPhone Scriptable 小组件用 `/pendo web widget-token`。

Scriptable 小组件使用 `plugins/pendo/web/scriptable/pendo_widget.js`，脚本仓库版本只保留 `BASE_URL` 和 `TOKEN` 占位值，导入 Scriptable 后需要替换成你自己的 Pendo Web 地址和 widget token。它可把未来 30 天内最多 5 条日程与右侧最多 5 条待办 / 财务 / 笔记摘要显示到主屏，并支持 `small` / `medium` / `large` 三种尺寸。

如果在 Windows 上默认端口启动失败，但 `netstat -ano` 看不到进程占用，通常是系统拒绝绑定该端口。优先改 `PENDO_WEB_PORT`，而不是继续排查“哪个进程占了它”。

公开 demo 会话默认关闭；只有在显式设置 `PENDO_WEB_DEMO_ENABLED=1` 时才会开放临时演示空间。

#### 定时任务

- **每分钟** - 检查提醒（事件/待办提醒按分钟级轮询）
- **每分钟** - 每日简报（用户可自定义触发时间，因此逐分钟检查）
- **每分钟** - 日记提醒（用户可自定义触发时间，因此逐分钟检查）
- **每天 00:05** - 待办计划迁移（将昨日仍 open 的计划顺延到今天）
- **每周日 21:00** - 每周财务总结
- **每月最后一天 21:00** - 月底财务总结
- **每 6 小时** - 清理过期 Web demo 数据

#### 注意事项

- 日程添加需要配置 LLM API 以使用 AI 解析功能
- 群聊中长消息会自动转为私聊以保护隐私
- 支持会话式交互，使用"退出"或"q"结束会话
- Pendo 的本地运行时数据包括 SQLite 数据库和 Web Token 签名密钥
- Web 控制台需要额外依赖：`PyJWT`、`fastapi`、`uvicorn`、`passlib[bcrypt]`（已包含在根目录 `requirements.txt`）

---

### echo - 回显示例

简单的示例插件，用于测试和调试。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `echo` | `/echo`, `/回显` | 复读输入的文本 |
| `hello` | `/hello`, `/你好` | 打招呼 |

---

## 💬 聊天插件

### xiaoqing_chat - 小青拟人聊天

`xiaoqing_chat` 提供以聊天体验为中心的拟人对话能力。它以 `/xc` 为统一入口，同时支持图片上下文、QQ 表情参与对话、本地表情包库，以及可单独配置的视觉模型能力。

它的重点是让对话保持自然连贯：文本对话、图片理解、主回复 LLM 的出站媒体 marker、长期记忆和表达学习都围绕同一条聊天主链协同工作。

插件的长期文档入口是 `plugins/xiaoqing_chat/README.md` 和 `plugins/xiaoqing_chat/ARCHITECTURE.md`：前者面向配置、使用和排障，后者说明 attention gate、频控、planner、memory、media、reply checker 的工程边界。本章提供命令速查和配置索引。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **语义记忆检索** | ✅ 基于向量数据库的语义记忆检索，拥有长期记忆能力 |
| **Attention Gate** | 区分“明确冲小青来的消息”和普通群聊插话，支持 `@`、名字、reply-to-bot、上下文共指等触发 |
| **行为规划** | 普通群聊可通过 PFC planner 判断下一步行动；forced/direct 场景直接进入回复生成 |
| **频率控制** | 普通群聊插话支持最小间隔、每分钟上限、连续回复冷却、heartflow 和未回复补偿 |
| **表达学习** | 从对话中学习表达风格和黑话，不断进化 |
| **人物与记忆系统** | 对话历史、事实记忆、人物资料、对话摘要、目标状态 |
| **图片上下文** | 普通图片、NapCat `mface`、QQ `face` 可进入正常对话流 |
| **出站媒体 marker** | 新收的表情包会进入本地图库，主回复 LLM 可在文本里附一个 `[想发...]` marker 来带出本地图片 / 表情包 / QQ 表情 |
| **性能优化** | 可选安装 `faiss-cpu` 加速向量检索，未安装则使用 numpy 实现 |
| **上下文感知** | 文本和图片都能进入统一上下文，进行连贯多轮对话 |

#### 命令列表

| 命令 | 说明 | 管理员 |
|------|------|--------|
| `/xc <内容>` | 和小青聊天；启用媒体能力后也可围绕图片/表情包继续聊 | ❌ |
| `/xc help` | 查看帮助 | ❌ |
| `/xc 清空` | 清空当前会话上下文记忆 | ❌ |
| `/xc 统计` | 查看当前会话统计 | ❌ |
| `/xc 深度` | 查看深度对话模式状态 | ❌ |
| `/xc 配置` | 查看插件配置概要 | ❌ |
| `/xc 记忆 <关键词>` | 检索长期记忆 | ❌ |
| `/xc 表达` | 查看学到的表达方式 | ❌ |
| `/xc 黑话` | 查看学到的黑话 | ❌ |
| `/xc 模型 [名称]` | 查看当前 provider；切换 provider 仅管理员可用 | ✅ |

#### 配置项

聊天模型和视觉模型在 `config/secrets.json` 中配置；插件行为开关在 `plugins/xiaoqing_chat/config/xiaoqing_config.json` 中配置。

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "your-chat-api-key",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions"
        }
      },
      "vision": {
        "default": "glm-4.6v-flash",
        "providers": {
          "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "your-vision-api-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
            "thinking": {
              "type": "disabled"
            }
          }
        }
      }
    }
  }
}
```

常用媒体行为开关如下。

```json
{
  "reply_probability_base": 0.72,
  "min_reply_interval_seconds": 3,
  "active_topic_min_reply_interval": 3.0,
  "max_replies_per_minute": 6,
  "continuous_reply_limit": 5,
  "continuous_cooldown_seconds": 12,
  "planner": {
    "enable_planner": true,
    "think_mode": "dynamic"
  },
  "heartflow": {
    "enable_heartflow": true,
    "base_score": 0.2
  },
  "brain_chat": {
    "enable_private_brain_chat": false,
    "private_planner_always_on": true,
    "brain_think_level": 2
  },
  "media": {
    "enable_inbound_media_context": true,
    "max_media_per_message": 1,
    "vision_provider": ""
  }
}
```

触发和频控说明如下。

- 当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时，群聊消息会先经过插件观察；`random_reply_rate` 不参与 dispatcher 分发。
- `/xc`、私聊、`@小青`、直接叫名字、只喊名字后的追问、reply 引用小青，以及有近期上下文锚点的“她/ta”共指召唤，会跳过普通插话概率并强制回复。
- 没有明确 directed attention 的普通群聊消息才会使用 `reply_probability_base`、heartflow、连续未回复补偿和硬频控。
- 配置边界：`reply_probability_private`、`heartflow.threshold`、`heartflow.enable_random_gate`、`heartflow.weight_mentioned`、`heartflow.weight_private`、`heartflow.weight_rate_limit`、`heartflow.weight_cooldown`、`heartflow.weight_interval` 不参与当前回复主路径。私聊、点名和共指由 attention gate 处理；速率限制由硬频控处理。

#### 记忆系统说明

**1. 语义记忆检索**
- 使用向量嵌入存储对话内容
- 支持语义相似度搜索
- 可以回忆起之前相关的对话
- 推荐安装 `faiss-cpu` 获得更好性能

**2. 事实记忆**
- 记录重要的事实信息
- 用户告诉的个人信息
- 对话中的关键事件

**3. 图片/表情包记忆**
- 收到的图片统一落到 `plugins/xiaoqing_chat/data/media/inbox/`
- 可发送图片固定放在 `plugins/xiaoqing_chat/data/media/reply_images/`
- 识别为表情包的图片会复制进 `plugins/xiaoqing_chat/data/media/library/`
- 图片描述缓存保存在 `plugins/xiaoqing_chat/data/media/render_cache.json`

#### 使用说明

- 作为 `smalltalk_provider` 使用时，会接管所有闲聊消息
- 插件内部有独立频率控制，不依赖全局 `random_reply_rate`
- `/xc`、私聊、`@`、直接叫名字、只喊名字后的追问、reply 引用小青，以及有近期上下文锚点的“她/ta”共指召唤会强制回复
- 普通群聊消息才走 `reply_probability_base`、heartflow 和硬频控
- 启用媒体能力后，纯图片、QQ 表情、NapCat `mface` 都能进入正常对话链
- 回复可以在自然文本里带一个 `[想发图片:hint]` / `[想发表情:hint]` / `[想发QQ表情:hint]` marker；插件解析命中后会发送实际图片、表情包或 QQ face，旧图库里缺失的媒体元数据会在后台修复，不阻断当前回复
- 如果视觉模型缺失或失败，图片会退回为保守 marker，不阻断纯文本聊天

#### 配置为默认聊天插件

在 `config/config.json` 中设置：

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```


---

### smalltalk - 闲聊插件

基础闲聊插件，支持问答学习功能。

#### 命令列表

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `qa` | `/记忆`, `/记住`, `/学习` | 教机器人新的问答 | ❌ |
| `qa_list` | `/对话` | 查看已学内容 | ❌ |
| `qa_remove` | `/删除对话` | 删除指定问答 | ✅ |

#### 使用示例

```
/记忆 你好 你好呀~       # 学习"你好"的回复
/对话                    # 查看所有已学问答
/对话 你好               # 搜索包含"你好"的问答
/删除对话 你好           # 删除"你好"的问答
```

#### 配置说明

在 `config/config.json` 中设置：

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk", // 或 "xiaoqing_chat"
    "smalltalk": {
      "voice_probability": 0           // 语音回复概率
    }
  }
}
```

---

### chat - AI 对话

基于 Coze API 的 AI 对话插件。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `chat` | `/chat`, `/gpt` | 与 AI 对话 |

#### 使用示例

```
/chat 今天天气怎么样
/gpt 帮我写一首诗
```

---

### voice - 语音功能

基于 Azure Cognitive Services 的语音插件。当前公开命令面提供 TTS；STT 作为内部工具函数保留，供其他插件集成时复用。

#### 功能特性

- **文字转语音 (TTS)**: 支持 SSML，可自定义语音、风格、角色
- **内部 STT 能力**: 供框架内其他插件复用，不单独暴露命令
- **音频缓存**: 基于文本与音色配置的缓存机制

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `tts` | `/语音`, `/念`, `/tts` | 文字转语音 |

#### 使用示例

```
/语音 你好，我是小青
/tts Hello World
```

---

## 🔭 天文科学

### apod - 每日天文图

获取 NASA 每日天文图（Astronomy Picture of the Day）。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `apod` | `/apod`, `/每日一天文图` | 获取今日天文图 |

#### 定时任务

- 每天 **13:30** 自动推送到配置的群

---

### arxiv_filter - arXiv 论文筛选

基于 BERT 模型的 arXiv 论文智能筛选插件。它先发送筛选出的论文列表，再把所有 positive 论文链接作为后台侧路交给 Codex `astro-ph` 会话生成中文 Markdown 摘要。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `arxiv` | `/arxiv`, `/论文` | 获取今日推荐论文 |

#### 定时任务

- 周一至周五 **10:00 / 10:30 / 11:00 / 11:30** 检查 arXiv 是否已经更新到当天；更新后执行筛选并推送论文列表。
- 周一至周五 **12:00** 做最后一次检查；如果当天仍未更新，发送停更通知，不再继续追加定时任务。
- 每天自动推送只执行一次，运行状态由 `plugins/arxiv_filter/data/update_status.json` 去重。
- 用户仍可通过 `/arxiv` 手动执行一次筛选；这会重新发送论文列表，并触发 Codex 摘要侧路。

#### 技术说明

使用预训练的 BERT 模型对当日 arXiv 论文进行相关性评分和筛选。筛选结果发送后，`plugins/arxiv_filter/codex_summary.py` 会从结果文本中提取所有 arXiv 链接并异步投递给 Codex；如果 Codex 摘要模块不可用或执行失败，不会影响论文列表消息。

Codex 侧会对同一天摘要做去重：

- 已有成功执行结果时，手动 `/arxiv` 会直接重发历史摘要。
- 已有同一天任务正在队列或运行中时，只发送状态提示。
- 失败过或没有成功记录时，重新总结并发送结果。
- Codex 执行失败时，会单独发送包含日期的总结失败消息。

---

### chime - FRB 重复暴监测

监测 CHIME 望远镜发现的快速射电暴（FRB）重复暴。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `chime` | `/chime`, `/frb` | 查看最新 FRB、列出已知条目或查询指定 FRB |

#### 定时任务

- 每天 **9:00** 和 **21:00** 自动检测并推送新发现

#### 使用示例

```
/chime                # 查看最新重复暴
/chime list           # 查看已知 FRB 列表
/chime FRB20201124A   # 查询指定 FRB
```

---

### dict - 天文学词典

天文学专业术语词典查询。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `dict` | `/dict`, `/词典`, `/字典` | 查询天文术语 |

#### 使用示例

```
/dict galaxy           # 查询"galaxy"
/dict -e galaxy        # 精确匹配 galaxy
/词典 黑洞             # 查询"黑洞"
```


---

### ads_paper - 论文与文献管理

基于 NASA ADS API 的天文论文管理助手，支持论文搜索、引用管理、笔记记录、AI 摘要等功能。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **多格式支持** | 支持 arXiv ID、arXiv URL、Bibcode 三种输入格式 |
| **智能识别** | 自动识别论文标识符类型，无需手动区分 |
| **BibTeX导出** | 一键获取标准 BibTeX 引用 |
| **引用网络** | 查看论文被引用和引用了哪些论文 |
| **AI 摘要** | 可选配置 LLM 生成论文摘要 |
| **笔记管理** | 为论文添加个人笔记和写作灵感 |
| **文献库** | 统一管理 BibTeX 引用文献 |
| **日推功能** | 基于关键词自动推荐相关论文 |

#### 命令列表

| 命令 | 说明 | 支持格式 |
|------|------|---------|
| `/paper search <关键词>` | 搜索论文 | - |
| `/paper author <作者>` | 查找作者论文 | - |
| `/paper cite <ID>` | 获取 BibTeX 引用 | ✅ arXiv ID/URL/Bibcode |
| `/paper cite-network <ID>` | 查看引用网络 | ✅ arXiv ID/URL/Bibcode |
| `/paper related <ID>` | 查找相关论文 | ✅ arXiv ID/URL/Bibcode |
| `/paper note <ID> [内容]` | 添加/查看论文笔记 | - |
| `/paper note del <ID> <序号>` | 删除笔记 | - |
| `/paper writing <章节> [想法]` | 添加/查看写作灵感 | - |
| `/paper writing del <章节> <序号>` | 删除灵感 | - |
| `/paper topics` | 查看研究兴趣关键词 | - |
| `/paper topics add <关键词>` | 添加关键词 | - |
| `/paper topics remove <关键词>` | 删除关键词 | - |
| `/paper deadline` | 查看截稿日期 | - |
| `/paper deadline add <名称> <日期>` | 添加截稿日期 | - |
| `/paper deadline del <序号>` | 删除截稿日期 | - |
| `/paper summarize <ID>` | AI 生成论文摘要 | ✅ arXiv ID/URL/Bibcode |
| `/paper daily` | 基于关键词推荐今日论文 | - |
| `/paper ref_add <ID>` | 添加引用到文献库 | ✅ arXiv ID/URL/Bibcode |
| `/paper refs` | 查看文献库 | - |

#### 支持的论文 ID 格式

插件智能支持多种输入格式，无需手动区分：

**1. arXiv ID (新格式)**
```
2401.12345
2601.22115
0706.0001
```

**2. arXiv ID (旧格式)**
```
astro-ph/0701089
hep-th/9901001
gr-qc/0601001
```

**3. arXiv URL**
```
https://arxiv.org/abs/2401.12345
http://arxiv.org/abs/2401.12345
https://arxiv.org/abs/astro-ph/0701089
```

**4. ADS Bibcode**
```
2026arXiv260122115P
2015ApJS..219...21Z
```

#### 配置说明

在 `secrets.json` 中配置 ADS API Token：

```json
{
  "plugins": {
    "ads_paper": {
      "ads_token": "your-ads-api-token",
      "api_base": "https://your-llm-api.com/v1",  // 可选：AI 摘要
      "api_key": "your-llm-key",                  // 可选：AI 摘要
      "model": "gpt-4"                             // 可选：AI 摘要
    }
  }
}
```

获取 ADS API Token: https://ui.adsabs.harvard.edu/user/settings/token

#### 使用示例

**1. 论文搜索与引用**

```
/paper search "fast radio burst"
/paper author "Smith, J"
/paper cite 2601.22115                              # arXiv ID
/paper cite https://arxiv.org/abs/2601.22115        # arXiv URL
/paper cite 2026arXiv260122115P                     # Bibcode
/paper cite astro-ph/0701089                        # 旧格式 arXiv ID
```

**2. 引用网络与相关论文**

```
/paper cite-network 2601.22115                      # 查看引用关系
/paper related https://arxiv.org/abs/2601.22115     # 查找相关论文
```

**3. 笔记管理**

```
/paper note 2601.22115 这篇用了ML方法分析FRB
/paper note 2601.22115                              # 查看笔记
/paper note del 2601.22115 1                        # 删除第1条笔记
```

**4. 写作灵感**

```
/paper writing 引言 强调FRB研究的重要性
/paper writing 引言                                 # 查看引言部分灵感
/paper writing del 引言 1                           # 删除第1条灵感
```

**5. 研究兴趣与日推**

```
/paper topics add fast radio burst
/paper topics add exoplanet
/paper topics                                       # 查看所有关键词
/paper daily                                        # 基于关键词推荐今日论文
```

**6. 文献库管理**

```
/paper ref_add 2601.22115                           # 添加到文献库
/paper refs                                         # 查看所有引用
```

**7. AI 摘要（需配置 LLM）**

```
/paper summarize 2601.22115                         # 生成 AI 摘要
```

#### 注意事项

- 需要申请 ADS API Token 才能使用
- AI 摘要功能需要额外配置 LLM API
- 所有接受 `<ID>` 参数的命令都支持多种格式
- 笔记、写作灵感、截稿日期数据保存在 `plugins/ads_paper/data/` 目录

---

### astro_tools - 天文计算工具箱

天文计算工具集，支持时间转换、坐标转换、天体查询、单位转换和公式速查。

#### 功能列表

| 子命令 | 说明 | 示例 |
|--------|------|------|
| `time` | 时间转换 (MJD ↔ 日期) | `/astro time 60419.5` |
| `coord` | 坐标转换 (角度 ↔ hmsdms) | `/astro coord 12:34:56 +12:34:56` |
| `obj` | 天文对象查询 (Simbad) | `/astro obj Crab Pulsar` |
| `convert` | 天文单位转换 | `/astro convert 3 Jy mJy` |
| `formula` | 天文公式速查 | `/astro formula dm` |
| `const` | 天文常数查询 | `/astro const c` |
| `redshift` | 红移计算 | `/astro redshift 0.5` |

#### 使用示例

**1. 时间转换**

```
/astro time 60419.5                                 # MJD 转日期
/astro time 2024-05-15                              # 日期转 MJD
/astro time                                         # 获取当前时间
```

**2. 坐标转换**

```
/astro coord 12:34:56 +12:34:56                     # 时角格式转角度
/astro coord 188.734 12.582                         # 角度转时角格式
```

**3. 天体查询**

```
/astro obj Crab Pulsar                              # 查询蟹状星云脉冲星
/astro obj M31                                      # 查询仙女座星系
/astro obj NGC 1275                                 # 查询 NGC 天体
```

**4. 单位转换**

```
/astro convert 3 Jy mJy                             # 流量密度转换
/astro convert 10 kpc Mpc                           # 距离单位转换
/astro convert 1 deg arcmin                         # 角度单位转换
```

**5. 公式速查**

```
/astro formula dm                                   # 视差距离模数公式
/astro formula                                      # 列出所有公式
```

**6. 天文常数**

```
/astro const c                                      # 光速
/astro const H0                                     # 哈勃常数
/astro const                                        # 列出所有常数
```

**7. 红移计算**

```
/astro redshift 0.5                                 # 计算 z=0.5 的各种参数
```

#### 依赖库

- `astropy` - 核心天文计算库
- `astroquery` - 天文数据库查询（Simbad）

---

### color - 颜色查询

中国传统色彩查询与颜色转换工具，支持光谱型颜色查询。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `color` | `/color`, `/颜色`, `/色彩` | 颜色查询 |

#### 参数选项

- `-n <名称>`: 按名称查询（支持中国传统色）
- `-r <RGB>`: 按 RGB 值查询
- `-s <光谱型>`: 按恒星光谱型查询颜色
- `-t [前缀]`: 列出全部光谱型，或按前缀过滤光谱型

#### 使用示例

```
/颜色 -n 天青           # 查询中国传统色"天青"
/color -r 255,128,0    # 查询 RGB 颜色
/色彩 -s G2V           # 查询太阳光谱型颜色
/color -t              # 列出全部光谱型
/color -t G            # 列出 G 开头的光谱型
```

---

## 🛠️ 实用工具

### choice - 随机选择

帮助选择困难症做决定。支持多选、去重、加权等功能。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `choice` | `/choice`, `/决定`, `/选择` | 随机选择 |

#### 参数选项

| 参数 | 说明 |
|------|------|
| `-n <数量>` | 指定选择数量 |
| `-u`, `--unique` | 去重选择（不重复选择同一项） |

说明：
- 默认模式使用有放回抽样；`-n` 大于选项数时，结果里允许重复项。
- `-u/--unique` 模式下，`-n` 不能大于选项数。
- 选项支持用引号包裹多词内容。

#### 使用示例

```
/选择 吃啥 火锅 烤肉 披萨
/决定 去不去 去 不去
/choice 抽奖 小明 小红 小张 -n 3    # 选择3个
/choice 问题 选项1 选项2 -u          # 去重选择
/choice 问题 选项1 选项1 选项2       # 加权选择（选项1权重更高）
/choice 晚饭 \"ice cream\" 披萨 -n 2  # 支持带空格的选项
```

---

### wolframalpha - 万能计算器

Wolfram|Alpha 计算引擎，可以计算数学、物理、化学等问题。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `alpha` | `/alpha`, `/wolfram`, `/wa`, `/计算` | 计算或查询 |

#### 特殊后缀

| 后缀 | 说明 |
|------|------|
| `step` | 显示步骤解答 |
| `cp` | 仅返回完整结果 |

#### 使用示例

```
/alpha 1+1                    # 简单计算
/alpha sin(pi/4)              # 三角函数
/alpha integrate x^2          # 积分
/alpha solve x^2+2x+1=0      # 方程求解
/alpha derivative of sin(x)   # 求导
/alpha integrate x^2 step     # 显示步骤解答
/alpha 1+1 cp                # 仅返回完整结果
/计算 population of China     # 查询数据
```

---

### codex - Codex 后台任务队列

通过 QQ 命令调用生产环境 Codex CLI。插件自己维护 Codex 会话标签、任务队列和对话记录，不占用 XiaoQing 的框架 Session。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `codex` | `/codex` | 查看帮助或执行子命令 |

#### 功能特性

- **独立会话标签**：`/codex create <name>` 创建 Codex 业务会话，不影响普通闲聊或其他命令。
- **显式投递任务**：后续用 `/codex <name> <任务>` 向指定会话追加任务。
- **队列隔离**：同一标签内串行执行，避免并发 resume 同一个 Codex thread；不同标签可并行执行。
- **主动回发结果**：任务完成、失败、超时或取消后，插件主动发送 `[codex:<name> #<job_id>]` 文字和图片消息。
- **图片透传**：每个任务自动获得 artifacts 目录；Codex 生成的本地图片会从 artifacts 或 `$CODEX_HOME/generated_images/` 复制到会话图片目录，并随文字一起发送。
- **会话持久化**：`plugins/codex/data/sessions.json` 保存 label、cwd 和 thread id；`plugins/codex/data/session/<name>/conversation.jsonl` 保存每个会话的任务、回复和图片记录。
- **受保护会话与归档**：`astro-ph` 等受保护会话不能被普通删除；删除会话时旧历史会移动到 `plugins/codex/data/deleted_sessions/`。
- **arXiv 摘要会话**：为 arXiv Filter 提供固定 `astro-ph` 会话、首次静默初始化、历史摘要重发和失败重试。
- **路径归一化**：QQ 中建议统一输入 `/` 斜杠路径，插件按 bot 所在系统解析。

#### 命令列表

| 命令 | 说明 |
|------|------|
| `/codex create <name> [cwd:<path>]` | 创建 Codex 会话标签；未指定 `cwd:` 时使用默认工作目录 |
| `/codex <name> <任务>` | 向指定会话追加任务，立即返回排队或开始执行状态 |
| `/codex list` | 查看所有 Codex 会话 |
| `/codex status [name]` | 查看全部或指定会话的运行状态 |
| `/codex cancel <name> [job_id]` | 取消正在运行的任务；指定 `job_id` 时也可移除排队任务 |
| `/codex stop <name> [job_id]` | `cancel` 的别名 |
| `/codex clear <name>` | 清空指定会话的排队任务 |
| `/codex delete <name> [--force] [--protected]` | 删除会话并归档历史；受保护会话必须同时带 `--force --protected` |

#### 配置说明

基础配置放在 `config.json -> plugins.codex`：

```json
{
  "plugins": {
    "codex": {
      "default_cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
      "allowed_cwd_roots": ["C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"],
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "job_timeout_seconds": 3600,
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true
    }
  }
}
```

如果 Codex CLI 不在 PATH 中，可在 `config.json` 或 `secrets.json` 的 `plugins.codex.codex_bin` 指定完整路径。`allowed_cwd_roots` 是安全边界，用户创建会话时指定的 `cwd:` 必须位于这些目录下。`arxiv_summary.cwd` 也应位于允许目录内，并提前放置 `arxiv-summary-methodology.md`。

#### arXiv 摘要会话

`arxiv_filter` 发送论文列表后，会把当天所有 positive 论文链接投递给 `arxiv_summary.label` 指定的 Codex 会话，默认是 `astro-ph`。如果该会话还没有 Codex thread，插件会先排入一条静默初始化任务，只写入会话历史，不推送到 QQ；随后再排入当天摘要任务。

摘要任务的 prompt 会明确要求 Codex 读取当前工作目录下的 `arxiv_summary.methodology`，并发送：

```markdown
## 2026-05-19
https://arxiv.org/abs/2605.16917
https://arxiv.org/abs/2605.18050
```

同一天如果已经有成功执行结果，插件会直接重发历史摘要；如果上一轮失败或没有记录，会重新总结。受保护的 `astro-ph` 会话需要以下命令才会删除：

```text
/codex delete astro-ph --force --protected
```

删除后旧目录会归档，新建同名会话时从空历史开始，不会读取已归档摘要。

#### 使用示例

```
/codex create main
/codex create repo cwd:C:/Users/testuser/Desktop/project
/codex main 总结一下当前项目结构
/codex repo 跑一下测试并说明失败点
/codex list
/codex status repo
/codex cancel repo
/codex delete repo --force
/codex delete astro-ph --force --protected
```

Windows 下可以写 `C:/Users/testuser/Desktop/project`。Linux/macOS 下照常写 `/home/user/project`。插件只负责路径解析和允许目录校验，不会绕过 Codex CLI 自身的 sandbox、审批策略和系统权限。

Codex 插件会自动把图片输出约定追加到每次任务的 prompt 后，用户不需要额外要求“把图片保存到哪里”。如果 Codex 在最终回复里用 Markdown 图片语法或 `图片: <path>` 标出图片，或直接把图片保存到本任务 artifacts 目录，插件会把图片复制到 `plugins/codex/data/session/<name>/images/` 并发送到 QQ。内置 imagegen 若只落到 `$CODEX_HOME/generated_images/`，插件也会按任务开始和结束时间扫描生成的图片作为兜底。

---

### shell - 终端命令

在服务器上执行终端命令。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `shell` | `/shell`, `/sh` | 执行命令 |
| `shell list` | `/shell list`, `/shell 列表` | 查看白名单 |

#### 功能特性

- **命令白名单**：仅允许执行白名单中的命令（可配置）
- **执行超时**：默认 30 秒超时
- **输出限制**：输出最大 4000 字符
- **安全防护**：禁止命令链接符（&&, ||, ;, |）除非在白名单
- **超时清理**：超时后会终止整棵子进程树，而不只是直接子进程
- **路径归一化**：QQ 中可统一输入 `/` 斜杠路径，插件按 bot 所在系统转换

#### 安全设置

通过 `secrets.json` 配置：

```json
{
  "plugins": {
    "shell": {
      "whitelist": ["ls", "pwd", "git"],
      "whitelist_mode": "extend",
      "timeout": 30,
      "disable_whitelist": false
    }
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `whitelist` | 自定义白名单 |
| `whitelist_mode` | `replace`(默认) 或 `extend` |
| `timeout` | 超时时间（秒） |
| `disable_whitelist` | 禁用白名单（危险模式） |

#### 路径格式

Shell 插件会在拆分命令参数后，对看起来像路径的参数做系统相关归一化：

- Windows 上可以输入 `C:/Users/testuser/Desktop/a.txt`，执行前会规范化成 Windows 本机路径。
- Linux/macOS 上继续输入 `/home/user/a.txt`、`~/a.txt`、`./file` 或 `../file`。
- `key=value` 中的 value 如果像路径，也会被归一化，例如 `--output=C:/tmp/a.txt`。
- URL（如 `https://example.com/a/b`）不会被当作路径改写。
- Windows 选项（如 `cmd /c`、`xcopy /Y`）不会被误判为绝对路径。

插件直接启动外部命令，不经过系统 shell。Windows 的 `copy`、`del`、`type` 等内建命令不能直接执行；需要用 `cmd /c copy ...`，或改用外部命令 `cp`、`xcopy`、`robocopy`。

#### 使用示例

```
/sh ls -la
/sh python --version
/sh ping -c 3 google.com
/sh list                    # 查看白名单
/shell help                 # 显示帮助
/shell cp C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
/shell cmd /c copy C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
/shell robocopy C:/Users/testuser/Desktop/src C:/Users/testuser/Desktop/dst a.txt
```

> ⚠️ **警告**: 此命令具有高危险性，请谨慎使用，仅管理员可用。

---

### url_parser - 链接解析

自动解析消息中的链接，生成预览信息。

**无需命令触发**，当消息中包含 URL 时自动解析。

支持的平台：
- Bilibili 视频/动态
- 微博
- 知乎
- GitHub
- 通用网页

如果网页提供 `og:image` 或 `twitter:image`，插件会在文字摘要后附带实际图片消息段。

对于使用相对路径图片地址的网页，插件也会基于页面 URL 自动补全成绝对地址后再发送。

---

### qingssh - SSH 远程控制

SSH 远程控制插件，支持交互式会话、命令执行和配置管理。

**核心特性**:
- **环境保持**: 支持 `cd` 切换目录和 `export` 环境变量
- **流式输出**: 实时推送长命令的执行结果
- **用户隔离**: 支持多用户、多群组同时与不同服务器交互
- **配置管理**: 支持导入 `~/.ssh/config`，支持密钥和密码认证
- **用户名支持**: ✅ 支持 `user@server` 格式指定连接用户名
- **Host Key 校验**: 默认严格校验 `~/.ssh/known_hosts`
- **安全跳板**: 支持 `ProxyJump` 和安全的 `ssh -W` ProxyCommand；拒绝执行本地 shell 型 ProxyCommand

#### 连接管理逻辑（核心机制）

本插件采用严格的 **用户 + 群组 + 服务器** 三维隔离机制，确保连接的安全性和独立性：

1.  **连接隔离**：
    - 连接标识符 (Key) = `用户ID : 群ID : 服务器名`
    - 群 A 中建立的服务器连接不能在群 B 中直接使用，需要重新连接。
    - 同样，其他用户也无法复用你的连接。

2.  **交互逻辑**：
    - 所有交互都在独立的 Socket 通道中进行。
    - 支持长连接和状态保持（如 `cd` 目录切换在回话期间持续有效）。

3.  **断开逻辑**：
    - `/ssh断开` 命令仅断开 **当前用户** 在 **当前群** 的指定连接。
    - **即使**你在多个群都连接了同一个服务器，在一个群断开**不会影响**其他群的连接。
    - **安全设计**：你永远无法断开其他用户的连接。

#### 交互与隔离示例

假设已添加服务器 `myserver`，不同用户在不同场景下的操作如下：

| 时间 | 操作者 | 环境 |指令 | 状态/结果 |
|------|--------|------|------|-----------|
| T1 | 用户A | 群1 | `/ssh myserver` | ✅ **建立连接 C1** (Key: `A:群1:myserver`) |
| T2 | 用户A | 群1 | `cd /var/www` | 📂 C1 切换目录到 `/var/www` |
| T3 | 用户B | 群1 | `/ssh myserver` | ✅ **建立连接 C2** (Key: `B:群1:myserver`) <br> *用户B拥有独立环境* |
| T4 | 用户B | 群1 | `pwd` | 📄 C2 输出 `/root` (不受 A 的 `cd` 影响) |
| T5 | 用户A | 群2 | `/ssh myserver` | ✅ **建立连接 C3** (Key: `A:群2:myserver`) <br> *即便是同一用户，换了群也是新环境* |
| T6 | 用户A | 群1 | `/ssh断开` | 🔌 **断开 C1** <br> *C2 (用户B) 和 C3 (A在群2) 保持连接，不受影响* |

#### 命令列表

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `ssh` | `/ssh`, `/远程` | 连接服务器或进入交互会话 |
| `ssh断开` | `/ssh断开`, `/sshdisconnect` | 断开当前会话的连接，或断开显式指定的服务器 |
| `ssh列表` | `/ssh list`, `/ssh列表` | 查看已保存的服务器 |
| `ssh状态` | `/ssh status`, `/ssh状态` | 查看当前活跃的连接数和详情 |
| `ssh添加` | `/ssh add`, `/ssh添加` | 添加服务器配置 |
| `ssh删除` | `/ssh remove`, `/ssh删除` | 删除服务器配置 |
| `ssh导入` | `/ssh import`, `/ssh导入` | 从 ~/.ssh/config 导入 |
| `sshconfig` | `/ssh config`, `/sshconfig` | 查看 ~/.ssh/config |
| `showimg` | `/showimg` | 查看远程服务器上的图片 |

#### 使用示例

**1. 添加服务器**

```
/ssh添加 myserver 192.168.1.100 22 root
/ssh添加                    # 引导式添加
```

**2. 连接服务器**

```
# 方式 1: 使用服务器配置的默认用户名
/ssh myserver               # 使用添加服务器时配置的用户名

# 方式 2: 指定用户名连接
/ssh user2@myserver         # 以 user2 用户连接 myserver
/ssh admin@webserver        # 以 admin 用户连接 webserver
```

**3. 执行命令**

```
> ls -la                    # 列出文件
> cd /var/log               # 切换目录
> export PATH=$PATH:/opt    # 设置环境变量
> tail -f syslog            # 查看日志
> 停止                      # 发送中文停止强行中断命令
> 退出                      # 结束会话
```

**4. 查看远程图片**

```
/showimg /home/user/plot.png              # 查看远程图片
/showimg user2@myserver:/data/chart.png   # 指定用户查看图片
```

**5. 查看状态与断开**

```
/ssh状态                    # 查看当前有多少活跃连接
/ssh断开                    # 断开当前的连接
/ssh断开 myserver           # 断开当前用户在当前群里的 myserver 连接
```

**6. 导入配置**

```
/sshconfig                  # 查看本机 ~/.ssh/config
/ssh导入 all                # 导入所有 Host
/ssh导入 myserver           # 导入单个 Host
```

#### 高级功能：用户名指定

**场景**: 你的服务器配置中使用 `root` 用户，但有时需要用其他用户连接

**解决方案**: 使用 `user@server` 格式

```
# 服务器配置：myserver 默认用户 root
/ssh myserver               # 以 root 连接
/ssh admin@myserver         # 临时以 admin 连接
/ssh deploy@myserver        # 临时以 deploy 连接

# 所有命令都支持这种格式
/showimg user@server:/path/to/image.png
```

**注意**：
- `user@server` 中的用户名会覆盖服务器配置中的默认用户
- 不同用户的连接是独立的，即使连接同一台服务器也不会互相影响

#### 配置说明

服务器配置保存在 `plugins/qingssh/data/servers.json`：

```json
{
  "myserver": {
    "host": "192.168.1.100",
    "port": 22,
    "username": "root",
    "password": "password123",     // 可选：密码认证
    "key_file": "~/.ssh/id_rsa"   // 可选：密钥认证
  }
}
```

**认证优先级**: 密钥 > 密码

#### 注意事项

- 插件默认要求管理员权限
- 支持 `Ctrl+C` 中断信号（发送 "停止" 或 "stop"）
- 会话超时会自动断开连接，避免资源泄露
- 使用 `user@server` 格式时，确保该用户在服务器上存在
- 密钥文件路径支持 `~` 展开


---

## 🌐 外部服务

### github - GitHub Trending

获取 GitHub 热门项目。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `github` | `/github`, `/gh`, `/trending` | 获取热门项目 |

#### 参数选项

- `daily`: 今日热门（默认）
- `weekly`: 本周热门
- `monthly`: 本月热门

#### 定时任务

- 每天 **8:30** 自动推送

#### 使用示例

```
/gh                    # 今日热门
/gh weekly             # 本周热门
```

---

### earthquake - 地震快讯

实时监测中国地震台网的地震速报。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `earthquake` | `/earthquake`, `/地震` | 查看最新地震 |

#### 定时任务

- 每 **5 分钟** 检测一次，有新地震自动推送

---

### signin - 自动签到

自动签到多个网站。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `signin` | `/signin`, `/签到` | 执行签到 |

#### 支持平台

- **Sony** (`/signin sony`, `/signin s`) - Sony 官网签到
- **影视飓风** (`/signin yingshi`, `/signin y`) - 影视飓风签到

#### 配置说明

需要在 `secrets.json` 中配置相应平台的账号信息：

```json
{
  "plugins": {
    "signin": {
      "sony": {
        "login_id": "账号",
        "password": "密码"
      },
      "yingshijufeng": {
        "app_id": "应用ID",
        "kdt_id": "店铺ID",
        "access_token": "访问令牌",
        "sid": "会话ID"
      }
    }
  }
}
```

#### 定时任务

- 每天 **0:30** 自动签到影视飓风

#### 使用示例

```
/signin                  # 显示帮助
/signin sony             # Sony 官网签到
/signin s                # Sony 签到（简写）
/signin yingshi          # 影视飓风签到
/signin y                # 影视飓风签到（简写）
```

---

### twitter - Twitter 图片

Twitter 图片抓取与随机发送。

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `twimg` | `/twimg`, `/twitter`, `/推特` | 随机发送一张图片 | ❌ |
| `tw_fetch` | `/tw_fetch`, `/抓取推特` | 手动抓取新图 | ✅ |

#### 配置说明

需要在 `secrets.json` 中配置：

```json
{
  "plugins": {
    "twitter": {
      "user_id": "Twitter用户ID",
      "proxy": "http://127.0.0.1:1080",
      "max_pages": 50,
      "headers": {},
      "cookies": {}
    }
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `user_id` | 要抓取的 Twitter 用户 ID |
| `proxy` | 代理地址（可选） |
| `max_pages` | 最大检查页数（默认 50） |
| `headers` | 自定义请求头 |
| `cookies` | Cookie 配置 |

#### 功能特性

- **智能抓取**：自动下载新图片，避免重复
- **本地存储**：图片存储在本地，无需重复下载
- **随机发送**：随机选择一张未发送过的图片
- **循环播放**：所有图片发送完后自动重置

#### 定时任务

- 每天 **3:00** 自动抓取新图片

#### 使用示例

```
/twimg                   # 随机发送推特图片
/twitter                 # 随机发送推特图片
/推特                    # 随机发送推特图片
/tw_fetch                # 手动抓取新图片（管理员）
/抓取推特                # 手动抓取新图片（管理员）
```

说明：
- `proxy` 不再默认指向本地 `127.0.0.1:1080`；只有显式配置时才启用代理。
      
---

### jupyter - 代码执行

Python 代码执行环境，支持绘图。
      
| 命令 | 触发词 | 说明 |
|------|--------|------|
| `jupyter` | `/jupyter`, `/py` | 执行 Python 代码 |
| `jupyter_kernel` | `/jupyter_kernel`, `/kernel` | 管理运行内核 |
      
#### 功能特性
      
- **代码执行**: 支持异步、并发执行 Python 代码
- **绘图支持**: matplotlib 绘图自动转换为图片发送
- **持久内核**: 变量状态在会话间保留
- **自动管理**: 空闲自动关闭，按需自动启动
- **隔离粒度**: 内核按“用户 + 群”隔离，同一用户跨群不会共享变量
- **超时处理**: 代码超时会主动中断当前执行，避免继续污染内核状态
      
#### 使用示例
      
```
/py print("Hello")
/py import numpy as np; np.random.rand(3)
/py import matplotlib.pyplot as plt; plt.plot([1,2,3]); plt.show()
/kernel restart        # 重启内核（清空变量）
```
      
---

### adnmb - A岛匿名版

A岛匿名版 (ADNMB) 客户端，支持浏览时间线和串内容。
      
| 命令 | 触发词 | 说明 |
|------|--------|------|
| `adnmb` | `/adnmb`, `/a岛` | 浏览 A岛 |
      
#### 使用示例
      
```
/adnmb                 # 查看时间线
/adnmb 1234567         # 查看串内容
/adnmb -h              # 查看帮助
```
      
---

## 🎮 娱乐游戏

### qingpet - QQ群宠物养成系统

虚拟宠物养成游戏，支持领养、喂养、互动、装扮和交易。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **宠物养成** | 领养、喂养、清洁、玩耍、睡眠、训练 |
| **状态系统** | 饱食度、心情、清洁度、健康、体力、经验 |
| **成长进化** | 宠物随时间成长，等级提升 |
| **物品系统** | 食物、玩具、药品、装扮等多类道具 |
| **社交互动** | 访问他人宠物、送礼、点赞、留言 |
| **装扮展示** | 多种装扮，宠物展示会 |
| **交易系统** | 玩家间物品交易 |
| **小游戏** | 内置小游戏赚取奖励 |
| **每日任务** | 每日签到和任务系统 |
| **排行榜** | 等级榜、财富榜、人气榜 |
| **管理功能** | 群组启用/禁用、封禁、数据管理 |
| **反脚本** | 频率限制和反刷屏机制 |
| **数据导出** | 支持导出宠物数据 |

#### 命令列表

**基础命令**

| 命令 | 说明 |
|------|------|
| `/宠物 领养` | 领养一只宠物 |
| `/宠物 状态` | 查看宠物状态 |
| `/宠物 排行榜` | 查看各种排行榜 |

**日常照顾**

| 命令 | 说明 |
|------|------|
| `/宠物 喂养 [道具]` | 喂食宠物 |
| `/宠物 清洁` | 清洁宠物 |
| `/宠物 玩耍` | 和宠物玩耍 |
| `/宠物 睡觉` | 让宠物休息 |
| `/宠物 醒来` | 唤醒宠物 |
| `/宠物 治疗 [道具]` | 治疗宠物 |

**成长训练**

| 命令 | 说明 |
|------|------|
| `/宠物 训练` | 训练宠物增加经验 |
| `/宠物 探索` | 探索获得物品 |

**物品系统**

| 命令 | 说明 |
|------|------|
| `/宠物 背包` | 查看背包 |
| `/宠物 商店` | 查看商店物品 |
| `/宠物 购买 <物品> [数量]` | 购买物品 |
| `/宠物 使用 <物品>` | 使用物品 |
| `/宠物 装扮 <装扮>` | 更换装扮 |

**社交互动**

| 命令 | 说明 |
|------|------|
| `/宠物 访问 [@某人]` | 访问他人宠物 |
| `/宠物 送礼 @某人 <物品>` | 赠送物品 |
| `/宠物 点赞 [@某人]` | 为他人宠物点赞 |
| `/宠物 留言 @某人 <内容>` | 留言给宠物 |
| `/宠物 查看 [@某人]` | 查看他人宠物 |
| `/宠物 展示会` | 参加宠物展示会 |

**交易系统**

| 命令 | 说明 |
|------|------|
| `/宠物 交易 @某人 <物品> <数量> [价格]` | 发起交易 |

**其他功能**

| 命令 | 说明 |
|------|------|
| `/宠物 小游戏` | 玩小游戏 |
| `/宠物 任务` | 查看每日任务 |
| `/宠物 签到` | 每日签到 |
| `/宠物 改名 <新名字>` | 给宠物改名 |
| `/宠物 召回 [天数]` | 召回失踪的宠物 |

**管理命令（管理员）**

| 命令 | 说明 |
|------|------|
| `/宠物 管理 启用` | 在当前群启用插件 |
| `/宠物 管理 禁用` | 在当前群禁用插件 |
| `/宠物 管理 配置` | 查看配置 |
| `/宠物 管理 封禁 @某人` | 封禁用户 |
| `/宠物 管理 解封 @某人` | 解封用户 |
| `/宠物 管理 日志` | 查看操作日志 |
| `/宠物 管理 统计` | 查看数据统计 |
| `/宠物 管理 删除 @某人` | 删除宠物 |
| `/宠物 管理 重置 [@某人]` | 重置宠物状态 |
| `/宠物 管理 导出` | 导出数据 |
| `/宠物 管理 公告 <内容>` | 发布群公告 |

#### 定时任务

- **每分钟** - 衰减宠物状态
- **每天 00:00** - 每日重置（年龄+1，刷新任务）
- **每周一 10:00** - 每周活动结算

#### 使用示例

```
/宠物 领养                # 领养宠物
/宠物 喂喂                 # 喂食
/宠物 玩耍                 # 玩耍
/宠物 状态                 # 查看状态
/宠物 排行榜 等级          # 等级排行榜
/宠物 装扮 墨镜            # 装扮墨镜
/宠物 访问 @小明           # 访问小明的宠物
/宠物 展示会               # 参加展示会
```

---

### guess_number - 猜数字游戏

多轮对话示例插件，猜数字游戏。支持难度选择、动态范围缩小等功能。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `guess_number` | `/猜数字`, `/guess`, `/猜` | 开始游戏 |

#### 难度选择

| 难度 | 范围 | 机会 | 命令 |
|------|------|------|------|
| 简单 | 1-50 | 10 | `/猜数字 简单`, `/guess easy` |
| 普通 | 1-100 | 7 | `/猜数字`, `/guess normal` |
| 困难 | 1-200 | 8 | `/猜数字 困难`, `/guess hard` |
| 地狱 | 1-1000 | 10 | `/猜数字 地狱`, `/guess hell` |

#### 游戏流程

1. 发送 `/猜数字` 开始游戏（可指定难度）
2. 机器人生成指定范围的随机数
3. 输入数字进行猜测
4. 系统会动态缩小猜测范围
5. 在机会用尽前猜中即可获胜
6. 3 分钟无操作自动结束会话

#### 游戏中命令

| 命令 | 说明 |
|------|------|
| 输入数字 | 进行猜测 |
| `status`, `状态` | 查看当前游戏状态 |
| `退出`, `取消`, `q` | 放弃游戏 |

#### 其他命令

| 命令 | 说明 |
|------|------|
| `/猜数字 help` | 显示帮助 |
| `/猜数字 status` | 查看当前游戏状态 |
| `/猜数字 restart` | 重新开始游戏 |

#### 功能特性

- **动态范围**：根据猜测自动缩小数字范围
- **评价系统**：根据尝试次数给出评价
- **会话管理**：3 分钟超时自动结束
- **多难度**：4 种难度可选

---

### minecraft - MC 服务器通信

Minecraft 服务器通信插件，支持多服务器、双向聊天和状态查询。

| 命令 | 触发词 | 说明 | 优先级 |
|------|--------|------|--------|
| `mc` | `/mc`, `/minecraft` | 发送消息或查询状态 | - |
| `mcconnect` | `/mcconnect`, `/mc连接` | 连接服务器 | 1 |
| `mcdisconnect` | `/mcdisconnect`, `/mc断开` | 断开连接 | 1 |

#### 功能特性

- **RCON 协议**: 标准 Minecraft RCON 通信
- **双向聊天**: QQ ↔ MC 实时消息同步
- **多服务器**: 支持连接多个服务器（不同群/私聊可连接不同服务器）
- **日志监控**: 自动读取服务器 `latest.log`

#### 使用示例

```
/mc help                # 显示帮助
/mcconnect 127.0.0.1:25575 password          # 连接服务器
/mcconnect 127.0.0.1:25575 password /path/to/latest.log # 连接服务器并指定日志路径
/mc status             # 查看连接状态
/mc list               # 查看在线玩家
/mc time set day       # 发送命令到服务器
/mc 大家好             # 向服务器发送消息
/mcdisconnect          # 断开连接
```

> [!NOTE]
> 如果传入日志文件路径，插件会先校验它是否指向 `latest.log`，校验通过后才会建立 RCON 连接。

#### 定时任务

- 每 **5 秒** 检查一次服务器日志

---

## 📊 插件统计

统计时间口径：当前仓库内 `plugins/**/plugin.json` 共 `29` 个。

| 分类 | 数量 | 插件 |
|------|------|------|
| **核心** | 3 | bot_core, echo, pendo |
| **聊天** | 4 | xiaoqing_chat, smalltalk, chat, voice |
| **天文科学** | 7 | apod, arxiv_filter, chime, dict, ads_paper, astro_tools, color |
| **实用工具** | 8 | choice, wolframalpha, codex, shell, url_parser, jupyter, adnmb, qingssh |
| **外部服务** | 4 | github, earthquake, signin, twitter |
| **娱乐游戏** | 3 | qingpet, guess_number, minecraft |
| **总计** | **29** | |

---

## 🔗 另请参阅

- [插件开发指南](03-plugin-development.md)
- [配置说明](06-configuration.md)
- [核心模块](04-core-modules.md)

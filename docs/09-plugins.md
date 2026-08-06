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
      - [显式模式](#显式模式)
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
| `help` | `/help`, `/h`, `/帮助`, `/catalog` | 分层查看插件级功能导航、插件内命令或 JSON 目录 | ❌ |
| `plugins` | `/plugins`, `/插件` | 查看已加载插件列表 | ❌ |
| `reload` | `/reload`, `/重载` | 热重载配置和插件 | ✅ |
| `metrics` | `/metrics`, `/指标` | 查看运行指标统计 | ✅ |
| `闭嘴` | `/闭嘴`, `/shutup`, `/mute` | 群内静音一段时间 | ✅ |
| `说话` | `/说话`, `/speak`, `/unmute` | 解除群内静音 | ✅ |
| `set_secret` | `/set_secret`, `/设置密钥` | 修改密钥配置 | ✅ |
| `get_secret` | `/get_secret`, `/查看密钥` | 查看密钥配置 | ✅ |

#### 使用示例

```
/help                    # 查看 Core 与全部插件的紧凑功能导航
/help page 1             # 按页浏览插件级导航
/help pendo              # 查看 Pendo 的一级功能入口
/help pendo todo         # 查看 Todo 的直接操作
/help pendo todo add     # 查看 Todo 添加命令的完整详情
/help pendo.pendo.todo.add # 稳定命令码仍可打开同一详情
/help search 天文        # 搜索别名、说明和用法
/help json page 1        # 为自动化按页导出全量结构化 JSON
/help json qingpet       # 只导出 QingPet 的结构化 JSON
/闭嘴 30                 # 静音 30 分钟
/闭嘴 1h                 # 静音 1 小时
/reload                  # 热重载所有插件
```

---

### pendo - 个人时间与信息管理中枢

个人时间与信息管理插件，支持日程、待办、笔记、日记、账本、提醒、搜索、统计和 **Web 控制台**（FastAPI + 原生 JS SPA）。

> `/pendo` 会显示带 emoji 的模块导航帮助；输入 `/pendo <模块>`（如 `/pendo event`、`/pendo ledger`）可直达对应分组帮助。
> 全部 Pendo 聊天命令只允许私聊，群聊请求由 Core 在进入插件前拒绝。

Pendo 的长期文档入口是 `plugins/pendo/README.md` 和 `plugins/pendo/ARCHITECTURE.md`：前者面向使用和部署，后者面向维护和二次开发。本章提供命令速查和功能索引。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **AI 智能解析** | 日程添加自动识别时间、地点、提醒设置 |
| **多轮对话** | 支持会话式操作 |
| **隐私保护** | manifest 声明全部聊天命令仅允许私聊 |
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
| `/pendo settings daily_briefing on/off` | 开关每日简报 |
| `/pendo settings diary_remind <时间>` | 日记提醒时间 |
| `/pendo settings ai_consent on/off` | 是否允许把日记正文发送给已配置的外部 AI |

**Web 控制台**

| 命令 | 说明 |
|------|------|
| `/pendo web token` | 生成私聊一次性登录码（7 天内仅可使用一次） |
| `/pendo web widget-token` | 生成 Scriptable 小组件令牌（只读） |
| `/pendo web widget-revoke` | 吊销自己的全部 Scriptable 小组件令牌 |
| `/pendo web start` | 启动 Web 服务 |
| `/pendo web stop` | 停止 Web 服务 |
| `/pendo web status` | 查看运行状态和访问地址 |

> pendo 插件初始化时会在 `plugins.pendo.web_enabled` 为 `true` 时尝试自动启动 Web 服务；如果启动失败，仍可用 `/pendo web start` 手动重试。默认监听 `127.0.0.1:12001`。绑定非 loopback 地址时，服务要求在 TLS 反向代理后配置 `"web_session_cookie_secure": true`，不会开放明文 HTTP 登录入口。

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
| `/pendo undo [分钟]` | 撤销最近 5 分钟内的删除或编辑；参数可将范围缩短至 1～5 分钟 |

#### 配置说明

Pendo 的模型连接复用项目级统一注册表，只在 `config.json` 声明 `parse` route：

```json
{
  "plugins": {
    "pendo": {
      "ai": {
        "routes": {
          "parse": {
            "models": ["deepseek-flash", "glm-5.2"],
            "temperature": 0.3,
            "max_tokens": 1000
          }
        }
      }
    }
  }
}
```

provider 与模型 profile 位于 `config.ai`，API Key 位于 `secrets.ai.providers`。route 不可用时自动回退到本地规则解析。

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
/pendo settings diary_remind 21:30          # 设置日记提醒时间
/pendo settings daily_briefing off           # 关闭每日简报
/pendo settings ai_consent off               # 禁止把日记正文发送给外部 AI
```

**9. 撤销操作**

```
/pendo undo                                  # 撤销最近5分钟内的删除
/pendo undo 3                                # 撤销最近3分钟内的删除或编辑
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
/pendo web token                             # 获取一次性浏览器登录码（私聊）
/pendo web widget-token                      # 获取 Scriptable 小组件令牌
/pendo web widget-revoke                     # 吊销自己的全部小组件令牌
/pendo web start                             # 启动 Web 服务（默认端口 12001）
/pendo web status                            # 查看访问地址
/pendo web stop                              # 停止服务
```

启动后默认通过 `http://127.0.0.1:12001` 访问。若在 TLS 反向代理后绑定非 loopback 地址，必须在 `config/config.json` 的 `plugins.pendo` 中配置 `"web_session_cookie_secure": true`；服务会拒绝不安全的外网绑定。浏览器登录使用 `/pendo web token` 私聊发送的原始一次性登录码（7 天、仅一次），在登录页粘贴后使用短期 HttpOnly session cookie。iPhone Scriptable 小组件仍使用 `/pendo web widget-token` 的只读 bearer。

Scriptable 小组件使用 `plugins/pendo/web/scriptable/pendo_widget.js`，脚本仓库版本只保留 `BASE_URL`，Widget Token 在首次 App 内运行时通过安全输入框写入 iOS Keychain。Token 默认 365 天有效，可用 `/pendo web widget-revoke` 吊销。它可把未来 30 天内最多 5 条日程与右侧最多 5 条待办 / 财务 / 笔记摘要显示到主屏，并支持 `small` / `medium` / `large` 三种尺寸。

如果在 Windows 上默认端口启动失败，但 `netstat -ano` 看不到进程占用，通常是系统拒绝绑定该端口。优先改 `plugins.pendo.web_port`，而不是继续排查“哪个进程占了它”。

公开 demo 会话默认关闭；只有把 `plugins.pendo.web_demo_enabled` 显式配置为 `true` 时才会开放临时演示空间。

#### 定时任务

- **每分钟** - 检查提醒（事件/待办提醒按分钟级轮询）
- **每分钟** - 每日简报（用户可自定义触发时间，因此逐分钟检查）
- **每分钟** - 日记提醒（用户可自定义触发时间，因此逐分钟检查）
- **每天 00:05** - 待办计划迁移（将昨日仍 open 的计划顺延到今天）
- **每天 00:15** - 清理过期操作日志与撤销快照
- **每周日 21:00** - 每周财务总结
- **每月最后一天 21:00** - 月底财务总结
- **每 6 小时** - 清理过期 Web demo 数据

#### 注意事项

- LLM API 是可选能力；未配置或请求失败时，日程解析和日记情绪分析会回退到本地规则
- 所有聊天命令与多轮会话均只在私聊中运行
- 支持会话式交互，使用"退出"或"q"结束会话
- Pendo 的本地运行时数据包括 SQLite 数据库和 Web Token 签名密钥
- Web 控制台依赖已包含在仓库根目录 `requirements.txt` 中；请使用 `python -m pip install -r requirements.txt`
- 默认仅绑定 loopback。部署到非 loopback/TLS 反向代理时必须配置 `plugins.pendo.web_session_cookie_secure=true`，否则服务拒绝启动；请同时由反向代理提供 HTTPS。

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
| `/xc 模型 [别名]` | 查看 route profile；管理员可严格固定模型，`默认` 恢复 fallback | ✅ |

#### 配置项

聊天和视觉模型复用 `config.ai` 统一注册表，API Key 位于 `secrets.ai.providers`；插件行为开关在 `plugins/xiaoqing_chat/config/xiaoqing_config.json` 中配置。

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "default_model_alias": "deepseek",
        "model_aliases": {
          "deepseek": "deepseek-flash",
          "glm": "glm-5.2"
        },
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"]
          },
          "checker": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"]
          },
          "reasoning": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"]
          },
          "vision": {
            "models": ["glm-4.6v-flash", "glm-4.6v"]
          }
        }
      }
    }
  }
}
```

列表第 0 项是主模型，其余项只在允许的故障类别上顺序 fallback。GLM provider 使用标准按量 API；Coding Plan 专属端点只用于官方支持的编码工具。完整 provider、profile 和密钥示例见 `docs/06-configuration.md`。

常用媒体行为开关如下。

```json
{
  "reply_probability_base": 0.45,
  "active_topic_reply_probability": 0.6,
  "active_topic_question_reply_probability": 0.9,
  "min_reply_interval_seconds": 8,
  "active_topic_min_reply_interval": 3.0,
  "active_topic_question_min_reply_interval": 2.0,
  "max_replies_per_minute": 4,
  "continuous_reply_limit": 3,
  "continuous_cooldown_seconds": 25,
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
    "max_media_per_message": 1
  }
}
```

触发和频控说明如下。

- 当 `smalltalk_provider` 设置为 `xiaoqing_chat` 时，群聊消息会先经过插件观察，再由插件内部判断是否回复。
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
- 收到的图片统一落到 `data/xiaoqing_chat/media/inbox/`
- 可发送图片固定放在 `data/xiaoqing_chat/media/reply_images/`
- 识别为表情包的图片会复制进 `data/xiaoqing_chat/media/library/`
- 图片描述缓存保存在 `data/xiaoqing_chat/media/render_cache.json`

#### 使用说明

- 作为 `smalltalk_provider` 使用时，会接管所有闲聊消息
- 插件内部有独立频率控制
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

基础闲聊插件，支持只喊名字回复、分域问答、`chat.reply` 回落和可选的
`voice.synthesize_text` 语音合成。它不会对普通群消息随机插话，也不存在笑话命令。

#### 命令列表

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `qa` | `/记忆`, `/记住`, `/学习` | 教机器人新的问答 | ✅ |
| `qa_list` | `/对话` | 查看已学内容 | ✅ |
| `qa_remove` | `/删除对话` | 删除指定问答 | ✅ |

#### 使用示例

```
/记忆 你好 你好呀~       # 学习"你好"的回复
/对话                    # 查看所有已学问答
/对话 你好               # 精确查询"你好"的回答
/删除对话 你好           # 删除"你好"的问答
```

#### 配置说明

在 `config/config.json` 中设置：

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk",
    "smalltalk": {
      "voice_probability": 0
    }
  }
}
```

三个 QA 命令均为管理员命令；问题是第一个非空白字段，群聊按群号共享，私聊按用户号隔离。
`voice_probability` 必须是 `0` 到 `1` 的有限数字，非法显式值会禁用语音。更完整的配额、运行数据和
provider 降级边界见插件目录中的 `README.md`。

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

基于 Azure Speech 的语音插件。公开命令只提供管理员 TTS；内部同时提供 `voice.synthesize_text` 服务和受限的 `speech_to_text()` 工具函数。

#### 功能特性

- **文字转语音 (TTS)**：文本最多 500 个字符，SSML 属性和正文均经过转义
- **内部 STT 能力**：只接受 16 kHz、单声道、16 位 PCM WAV，不单独暴露命令
- **有界音频缓存**：缓存键包含文本和全部音色配置，最多 2048 项、256 MiB，保留 7 天
- **受限传输**：单个音频最多 10 MiB，TTS/STT 共用最多 2 个并发 Azure 请求

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `tts` | `/语音`, `/念`, `/tts` | 管理员文字转语音 |

#### 使用示例

```
/语音 你好，我是小青
/tts Hello World
```

Azure 密钥、区域、音色和可选代理只配置在 `config/secrets.json` 的 `plugins.voice`；完整字段与运行边界见 `plugins/voice/README.md`。

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
| `arxiv` | `/arxiv`, `/论文` | 获取 arXiv 当前最新列表的推荐论文 |

#### 定时任务

- 周一至周五 **10:00 / 10:30 / 11:00 / 11:30** 检查 arXiv 是否已经更新到当天；更新后执行筛选并推送论文列表。
- 周一至周五 **12:00** 做最后一次检查；如果当天仍未更新，发送停更通知，不再继续追加定时任务。
- 每天自动推送只执行一次，运行状态由 `data/arxiv_filter/update_status.json` 去重。
- 用户仍可通过 `/arxiv` 手动请求筛选；回复会标明 arXiv 源列表的实际发布日期。源站当天更新前返回上一发布日列表属于正常行为。

#### 技术说明

使用预训练模型对 arXiv 当前源列表进行相关性评分和筛选。推理缓存按源列表日期区分，因此同一业务日内从昨日列表切换到今日列表时会重新推理。筛选结果发送后，`plugins/arxiv_filter/codex_summary.py` 会从结果文本中提取所有 arXiv 链接并异步投递给 Codex；如果无法确认源列表日期、Codex 摘要模块不可用或执行失败，不会影响论文列表消息。

Codex 侧按“源列表日期 + 规范化后的论文链接集合”去重：

- 两项身份都相同且已有成功执行结果时，手动 `/arxiv` 会直接重发历史摘要。
- 两项身份都相同且任务正在队列或运行中时，只发送状态提示。
- 日期相同但论文集合变化时，按新列表重新投递，不复用旧摘要。
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

搜索、作者、引用、引用网络、相关论文与摘要命令可在私聊或群聊使用；笔记、写作灵感、研究主题、截稿日期、每日个性化推荐和个人文献库命令只允许私聊。

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
      "ads_token": "your-ads-api-token"
    }
  }
}
```

AI 摘要使用 `config.plugins.ads_paper.ai.routes.summary`，模型链复用 `config.ai.models`，API Key 复用 `secrets.ai.providers`。未配置或调用失败时返回 ADS 原始摘要。

获取 ADS API Token 请访问 https://ui.adsabs.harvard.edu/user/settings/token

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
- 笔记、写作灵感、截稿日期数据保存在 `data/ads_paper/` 目录

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

SIMBAD 查询直接使用受限的 TAP HTTP 请求，不再依赖 `astroquery`。

---

### color - 颜色查询

中国传统色彩查询与颜色转换工具，支持光谱型颜色查询。

当前发行资产内置 526 种中国传统色；普通成员可使用全部查询功能。`-w` 添加和
`-d` 删除会修改当前会话共享数据，因此仅限 Bot 全局管理员，每个会话作用域最多
保存 200 种自定义颜色。生成的色卡使用 256 项、32 MiB、30 天空闲 TTL 的磁盘
LRU 缓存。

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

Wolfram|Alpha 计算引擎，可以计算数学、物理、化学等问题。命令仅限 Bot 管理员，App ID 只从 `config/secrets.json` 的 `plugins.wolframalpha.appid` 读取。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `alpha` | `/alpha`, `/wolfram`, `/wa`, `/计算` | 管理员计算或查询 |

#### 显式模式

| 参数 | 说明 |
|------|------|
| `--mode=step` | 显示步骤解答 |
| `--mode=complete` | 仅返回完整结果 |

`--mode=cp` 是 complete 的兼容别名。模式必须显式指定；普通问题自然以 `step` 或 `cp` 结尾时，这些词仍属于问题正文。查询最多 500 字符，三种模式共享 30 秒超时、1 MiB 响应预算和 2 个并发请求，最终 API 文本最多 2400 字符。

#### 使用示例

```
/alpha 1+1                    # 简单计算
/alpha sin(pi/4)              # 三角函数
/alpha integrate x^2          # 积分
/alpha solve x^2+2x+1=0      # 方程求解
/alpha derivative of sin(x)   # 求导
/alpha --mode=step integrate x^2  # 显示步骤解答
/alpha --mode=complete 1+1        # 仅返回完整结果
/计算 population of China     # 查询数据
```

---

### codex - Codex 后台任务队列

通过 QQ 命令调用生产环境 Codex CLI。插件自己维护 Codex 会话标签、任务队列和对话记录，不占用 XiaoQing 的框架 Session。全部 `/codex` 命令保持 `admin_only: true` 且仅允许私聊，只有 `admin_user_ids` 中的 Bot 管理员能在私聊中使用。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `codex` | `/codex` | 查看帮助或执行子命令 |

#### 功能特性

- **独立会话标签**：`/codex create <name>` 创建 Codex 业务会话，不影响普通闲聊或其他命令。
- **显式投递任务**：后续用 `/codex <name> <任务>` 向指定会话追加任务。
- **队列隔离**：同一标签内串行执行，避免并发 resume 同一个 Codex thread；不同标签可并行执行。
- **主动回发结果**：任务完成、失败、超时或取消后，插件主动发送 `[codex:<name> #<job_id>]` 文字和图片消息。
- **图片透传**：每个任务自动获得 artifacts 目录；Codex 生成的本地图片会从 artifacts 或 `$CODEX_HOME/generated_images/` 复制到会话图片目录，并随文字一起发送。
- **会话持久化**：`data/codex/sessions.json` 保存 label、cwd 和 thread id；`data/codex/session/<name>/conversation.jsonl` 保存每个会话的任务、回复和图片记录。
- **受保护会话与归档**：`astro-ph` 等受保护会话不能被普通删除；删除会话时旧历史会移动到 `data/codex/deleted_sessions/`。
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
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "methodology": "arxiv-summary-methodology.md"
      },
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "session_ttl_days": 90,
      "artifact_retention_days": 30,
      "emergency_disk_bytes": 10737418240,
      "emergency_queue_limit": 1000,
      "spawn_timeout_seconds": 30,
      "job_timeout_seconds": 3600,
      "max_stdout_bytes": 16777216,
      "max_stderr_bytes": 4194304,
      "max_json_line_bytes": 1048576,
      "max_final_output_bytes": 8388608,
      "max_qq_text_chars": 60000,
      "artifact_scan_max_entries": 5000,
      "artifact_scan_max_depth": 8,
      "max_image_artifacts": 20,
      "max_image_bytes": 20971520,
      "max_image_total_bytes": 104857600,
      "max_image_pixels": 40000000,
      "max_image_frames": 120,
      "max_qq_images": 10,
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true
    }
  }
}
```

如果 Codex CLI 不在 PATH 中，可在 `config.json` 或 `secrets.json` 的 `plugins.codex.codex_bin` 指定完整路径。`allowed_cwd_roots` 是安全边界，用户创建会话时指定的 `cwd:` 必须位于这些目录下。`arxiv_summary.cwd` 也应位于允许目录内，并提前放置 `arxiv-summary-methodology.md`。

安全边界必须按本机代码执行权限理解：manifest 只允许 Bot 管理员私聊触发，但 `codex_bin` 是可配置的可执行文件，等价于授予 Bot 进程运行该文件的权限；`sandbox: danger-full-access` 会取消 Codex CLI 的文件系统限制，`approval_policy: never` 不提供人工确认。只有受信任的管理员配置才能修改这些字段，不能把 Codex 命令开放到群聊或不受信任的配置写入路径。任务输出只向聊天发送受限文本、图片和文件名，完整本机路径留在日志/归档内部。

资源预算字段及默认值如下。配置值超出范围时会被钳制到最近边界。

| 字段 | 默认值 | 合法范围 | 超限行为 |
|---|---:|---:|---|
| `per_session_queue_limit` | 10 项 | 1-1,000 | 单会话达到排队上限后硬拒绝新任务。 |
| `emergency_queue_limit` | 1,000 项 | 10-10,000，且不低于会话上限 | 触发进程级紧急队列保护。 |
| `emergency_disk_bytes` | 10 GiB | 不低于 64 MiB | 数据目录达到阈值后拒绝新任务。 |
| `max_stdout_bytes` | 16 MiB | 64 KiB-128 MiB | stdout 累计超限时终止整棵 Codex 进程树。 |
| `max_stderr_bytes` | 4 MiB | 64 KiB-64 MiB | stderr 累计超限时终止整棵 Codex 进程树。 |
| `max_json_line_bytes` | 1 MiB | 16 KiB-8 MiB | 单条 JSON 事件超限时立即终止任务。 |
| `max_final_output_bytes` | 8 MiB | 64 KiB-64 MiB | 最终输出文件超限时终止任务，只归档有界的头尾截断副本。 |
| `max_qq_text_chars` | 60,000 字符 | 2,000-200,000 | 完整结果写入任务归档，QQ 仅发送截断文本和归档位置。 |
| `artifact_scan_max_entries` | 5,000 项 | 10-20,000 | 到达条目上限后停止扫描，未扫描候选不收集。 |
| `artifact_scan_max_depth` | 8 层 | 1-16 | 更深条目不扫描。 |
| `max_image_artifacts` | 20 张 | 1-100 | 超出数量的图片候选被拒绝。 |
| `max_image_bytes` | 20 MiB | 64 KiB-100 MiB | 超过单文件字节上限的图片被拒绝。 |
| `max_image_total_bytes` | 100 MiB | 64 KiB-512 MiB | 超过累计字节预算的后续图片被拒绝。 |
| `max_image_pixels` | 40,000,000 像素 | 1,024-100,000,000 | 真实解码像素超限或签名/解码失败的图片被拒绝。 |
| `max_image_frames` | 120 帧 | 1-500 | 多帧图片超限时被拒绝。 |
| `max_qq_images` | 10 张 | 1-20 | 只发送前 N 张已接受图片，其余已归档图片不发送。 |

输出流/最终输出超限属于进程级硬限制；最终输出文件超限只归档有界头尾副本，QQ 文本字符超限采用“完整归档、截断投递”；图片扫描、数量、字节、签名/解码、像素与帧数超限采用“拒绝不合格产物”，拒绝原因进入任务记录。`max_qq_images` 只限制 QQ 发送数量。`sessions.json` 采用版本化字段白名单，坏状态隔离到 `quarantine/`；`session_ttl_days` 归档空闲非受保护会话，`artifact_retention_days` 清理已结束 job、输出及旧归档，活跃任务不会被回收。资源预算保护 Bot 存活性与投递链路，不改变 Codex 面向可信管理员的 sandbox、审批策略和工作目录灵活性。

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
/codex create repo cwd:C:/workspace/project
/codex main 总结一下当前项目结构
/codex repo 跑一下测试并说明失败点
/codex list
/codex status repo
/codex cancel repo
/codex delete repo --force
/codex delete astro-ph --force --protected
```

Windows 下可以写 `C:/workspace/project`。Linux/macOS 下照常写 `/srv/xiaoqing/workspaces/project`。插件只负责路径解析和允许目录校验，不会绕过 Codex CLI 自身的 sandbox、审批策略和系统权限。

Codex 插件会自动把图片输出约定追加到每次任务的 prompt 后，用户不需要额外要求“把图片保存到哪里”。如果 Codex 在最终回复里用 Markdown 图片语法或 `图片: <path>` 标出图片，或直接把图片保存到本任务 artifacts 目录，插件会把图片复制到 `data/codex/session/<name>/images/` 并发送到 QQ。内置 imagegen 若只落到 `$CODEX_HOME/generated_images/`，插件也会按任务开始和结束时间扫描生成的图片作为兜底。

---

### shell - 终端命令

供 Bot 管理员在私聊中执行服务器终端命令。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `shell` | `/shell`, `/sh` | 执行命令 |
| `shell list` | `/shell list`, `/shell 列表` | 查看启用入口及当前终端可用性 |

#### 功能特性

- **管理员命令启用列表**：控制默认开放哪些命令入口，用于防误触；它不是安全沙箱，Python、PowerShell、Docker 等通用工具仍可执行任意管理员操作
- **执行超时**：默认 30 秒超时
- **输出限制**：输出最大 4000 字符
- **权限边界**：`admin_only`、manifest 私聊场景与入站认证共同构成边界；参数检查和命令链接符限制只降低误操作概率
- **超时清理**：超时后会终止整棵子进程树，而不只是直接子进程
- **路径归一化**：QQ 中可统一输入 `/` 斜杠路径，插件按 bot 所在系统转换
- **可配置终端**：公开配置可选择 `direct` 或带明确可执行文件路径的 `git-bash`
- **运行环境透明**：启用列表不等于程序已安装；`/shell list` 会按当前终端把可执行和未找到的入口分开显示

#### 安全设置

公开终端配置通过 `config.json` 设置；以下示例在 Windows 使用 Git Bash：

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "git-bash",
        "executable": "C:/Program Files/Git/bin/bash.exe"
      }
    }
  }
}
```

Git Bash 不加载 profile/rc；路径失效时会明确失败，不会回退到 WSL Bash 或 direct。命令启用列表和超时继续通过 `secrets.json` 配置：

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

- Windows 上可以输入 `C:/workspace/a.txt`，执行前会规范化成 Windows 本机路径。
- Linux/macOS 上继续输入 `/home/user/a.txt`、`~/a.txt`、`./file` 或 `../file`。
- `key=value` 中的 value 如果像路径，也会被归一化，例如 `--output=C:/tmp/a.txt`。
- URL（如 `https://example.com/a/b`）不会被当作路径改写。
- Windows 选项（如 `cmd /c`、`xcopy /Y`）不会被误判为绝对路径。

`direct` 后端直接启动外部命令，不经过系统 shell。Windows 的 `copy`、`del`、`type` 等内建命令不能直接执行；需要用 `cmd /c copy ...`，或改用外部命令。Git Bash 后端会显式解释原始单命令文本，但命令链接、多行和受限危险模式仍在进入 Bash 前被拒绝。

#### 使用示例

Linux/macOS：

```text
/sh ls -la
/sh python --version
/sh ping -c 3 127.0.0.1
/sh list                    # 查看白名单
/shell help                 # 显示帮助
/shell cp /srv/a.txt /srv/b.txt
```

Windows direct：

```text
/shell python --version
/shell git status --short
/shell cmd /c dir
/shell cmd /c cd
/shell cmd /c copy C:/workspace/a.txt C:/workspace/b.txt
/shell robocopy C:/workspace/src C:/workspace/dst a.txt
```

Windows Git Bash 使用上面的 Linux/macOS 命令形式，例如 `/shell ls -la` 和 `/shell pwd`。

> ⚠️ **警告**: 此命令具有高危险性，请谨慎使用，仅管理员可用。

---

### url_parser - 链接解析

为完整的单 URL 消息生成网页标题、描述和可选预览图。

**无需命令触发**。Dispatcher 仅在清理后的消息整体是一个 HTTP(S) URL 时调用本插件；带附加文字或
多个 URL 的消息不会触发。

- 标题最多 200 个字符；描述按 `description`、`og:description`、`twitter:description` 的顺序读取，
  最多 100 个字符。
- `og:image`、`twitter:image` 支持相对地址，并以页面最终 URL 为基准补全。
- 输入 URL 最多 2048 个字符，最终文字预览不会超过 QQ 单条文本预算。
- HTML 上限为 2 MiB；预览图上限为 5 MiB、2000 万像素和 120 帧，只接收 JPEG、PNG、WebP。
- 页面和图片使用不含应用凭据的公共安全客户端，每次请求与重定向都校验 URL 和 DNS；同时最多处理 4 个预览。
- 图片缓存位于插件数据目录的 `url_previews/`，最多 128 项、128 MiB，7 天未使用后清理。

页面抓取失败时不发送预览；可选图片失败时仍保留已解析出的文字摘要。需要脚本执行、登录态或专有接口的
网站可能无法生成预览。

---

### qingssh - SSH 远程控制

仅供 Bot 管理员私聊使用的 SSH 远程控制插件，支持交互式会话、命令执行和配置管理。

**核心特性**:
- **环境保持**: 支持 `cd` 切换目录和 `export` 环境变量
- **流式输出**: 实时推送长命令的执行结果
- **用户隔离**: 不同私聊管理员的连接和远程环境互不复用
- **配置管理**: 支持导入 `~/.ssh/config`，支持密钥和密码认证
- **用户名支持**: ✅ 支持 `user@server` 格式指定连接用户名
- **Host Key 校验**: 默认严格校验 `~/.ssh/known_hosts`
- **安全跳板**: 支持 `ProxyJump` 和安全的 `ssh -W` ProxyCommand；拒绝执行本地 shell 型 ProxyCommand

#### 连接管理逻辑（核心机制）

本插件按 **私聊用户 + 服务器** 隔离连接，确保连接的安全性和独立性：

1.  **连接隔离**：
    - 连接标识符由用户、私聊场景和服务器共同确定。
    - 同样，其他用户也无法复用你的连接。

2.  **交互逻辑**：
    - 所有交互都在独立的 Socket 通道中进行。
    - 支持长连接和状态保持（如 `cd` 目录切换在回话期间持续有效）。

3.  **断开逻辑**：
    - `/ssh断开` 命令仅断开当前私聊用户的指定连接。
    - **安全设计**：你永远无法断开其他用户的连接。

#### 交互与隔离示例

假设已添加服务器 `myserver`，不同私聊管理员的操作如下：

| 时间 | 操作者 | 环境 |指令 | 状态/结果 |
|------|--------|------|------|-----------|
| T1 | 用户A | 私聊 | `/ssh myserver` | ✅ 建立连接 C1 |
| T2 | 用户A | 私聊 | `cd /var/www` | 📂 C1 切换目录到 `/var/www` |
| T3 | 用户B | 私聊 | `/ssh myserver` | ✅ 建立独立连接 C2 |
| T4 | 用户B | 私聊 | `pwd` | 📄 C2 输出 `/root`，不受 C1 影响 |
| T5 | 用户A | 私聊 | `/ssh断开` | 🔌 只断开 C1，C2 保持连接 |

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
/ssh断开 myserver           # 断开当前私聊用户的 myserver 连接
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

服务器配置保存在 `data/qingssh/servers.json`：

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
- 发送“停止”或 `stop` 时，按远端进程组执行 `TERM` → `KILL` 的有界终止，并无条件清理本地通道；若 PID 尚未解析或控制连接已丢失，会明确报告远端状态未知
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

使用部署者配置的共享凭据执行影视飓风远端签到；该命令保持 Bot 管理员专用。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `signin` | `/signin`, `/签到` | 执行签到 |

#### 签到平台

- **影视飓风** (`/signin yingshi`, `/signin y`) - 影视飓风签到

#### 配置说明

需要在 `secrets.json` 中配置相应平台的账号信息：

```json
{
  "plugins": {
    "signin": {
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
/signin yingshi          # 影视飓风签到
/signin y                # 影视飓风签到（简写）
```

---

### twitter - Twitter 图片

从指定 X/Twitter 账号抓取图片到有限的本地缓存，并随机发送一张本轮尚未发送的图片。

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `twimg` | `/twimg`, `/twitter`, `/推特` | 只从本地缓存随机发送一张图片 | ❌ |
| `tw_fetch` | `/tw_fetch`, `/抓取推特` | 立即抓取新图 | ✅ |

#### 配置说明

需要在 `secrets.json` 中配置：

```json
{
  "plugins": {
    "twitter": {
      "user_id": "Twitter用户ID",
      "headers": {
        "authorization": "Bearer <TWITTER_BEARER_TOKEN>"
      },
      "cookies": {},
      "proxy": "http://proxy.example.com:8080",
      "max_pages": 50
    }
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `user_id` | 目标用户 ID，接受非空字符串或正整数 |
| `headers` | API 自定义请求头，仅接受有限的字符串键值；代码不提供默认认证头 |
| `cookies` | API Cookie，仅接受有限的字符串键值 |
| `proxy` | 可选的 HTTP(S) 代理；空值和非法地址视为未配置 |
| `max_pages` | 最大检查页数，只接受整数并限制在 `1..50`，默认 50 |

#### 功能特性

- **安全抓取**：API 响应限制为 5 MiB；媒体只允许三个 Twitter HTTPS 域名，不携带 API 凭据
- **图片校验**：单图限制为 10 MiB、4000 万像素和 120 帧，只接收 JPEG、PNG、WebP
- **有限并发**：单轮最多新增 100 张、同时下载 4 张，重复抓取任务串行执行
- **有限缓存**：图片按内容哈希去重，最多 5000 项、512 MiB，保留 90 天
- **循环发送**：`posted.txt` 读取上限为 1 MiB，陈旧记录会剔除；全部图片发送后重置轮次

#### 定时任务

- 每天 **03:00** 静默抓取新图片，不主动向群聊发送消息

#### 使用示例

```
/twimg                   # 随机发送推特图片
/twitter                 # 随机发送推特图片
/推特                    # 随机发送推特图片
/tw_fetch                # 手动抓取新图片（管理员）
/抓取推特                # 手动抓取新图片（管理员）
```

说明：
- `/twimg` 只读取本地缓存；缓存为空时需等待定时任务或由管理员执行 `/tw_fetch`。
- `proxy` 不再默认指向本地 `127.0.0.1:1080`；只有显式配置合法地址时才启用。

---

### jupyter - 代码执行

仅供 Bot 管理员私聊使用的 Python 代码执行环境，支持绘图。
      
| 命令 | 触发词 | 说明 |
|------|--------|------|
| `jupyter` | `/jupyter`, `/py` | 执行 Python 代码 |
| `jupyter_kernel` | `/jupyter_kernel`, `/kernel` | 管理运行内核 |
      
#### 功能特性
      
- **代码执行**: 支持异步、并发执行 Python 代码
- **绘图支持**: matplotlib 绘图经过字节、PNG 签名和像素校验后以内联图片段发送，不遗留逐次执行目录
- **持久内核**: 变量状态在会话间保留
- **自动管理**: 空闲自动关闭，按需自动启动
- **隔离粒度**: 内核按私聊用户隔离，不同管理员不会共享变量
- **超时处理**: 代码超时会主动中断当前执行，用户参数和内部调用统一受 600 秒硬上限约束
- **启动回滚**: 只有 ready 握手完成后才发布内核；任一启动阶段失败都会完整回滚，无法确认退出的实例会隔离且不再复用
      
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
| **社交互动** | 访问他人宠物、送礼、点赞、留言；互访原子结算双方资产，留言将每日配额、记录和计数原子提交 |
| **装扮展示** | 多种装扮，宠物展示会 |
| **交易系统** | 玩家间物品交易 |
| **小游戏** | 猜拳、骰子、赛跑；按消息 ID 幂等，并将冷却、实际封顶金币、账本、经验和精力原子结算 |
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

仅供 Bot 管理员私聊使用的 Minecraft 服务器通信插件，支持多服务器、日志转发和状态查询。

| 命令 | 触发词 | 说明 | 优先级 |
|------|--------|------|--------|
| `mc` | `/mc`, `/minecraft` | 管理员执行 Minecraft RCON 命令或查询状态 | Bot 管理员 |
| `mcconnect` | `/mcconnect`, `/mc连接` | 连接服务器 | 1 |
| `mcdisconnect` | `/mcdisconnect`, `/mc断开` | 断开连接 | 1 |

#### 功能特性

- **RCON 协议**: 标准 Minecraft RCON 通信
- **双向聊天**: QQ ↔ MC 实时消息同步
- **多服务器**: 不同私聊管理员可连接不同服务器
- **日志监控**: 自动读取服务器 `latest.log`；以有界 tail、批量摘要、每服务器跨轮 token bucket、单 action 字符/字节上限和全局每 tick action 上限防止日志洪泛，摘要会写明折叠/跳过数量

#### 使用示例

```
/mc help                # 显示帮助
/mc connect default     # 使用 plugins/minecraft/config.json 与 secrets.json 中的 default profile
/mc status             # 查看连接状态
/mc list               # 查看在线玩家
/mc time set day       # 发送命令到服务器
/mc say 大家好         # 向所有在线玩家广播消息
/mc tell Steve 你好    # 向指定玩家发送私信
/mcdisconnect          # 断开连接
```

`say`、`tell` 和 `tellraw` 是 Minecraft 自身的服务器命令，插件会把 `/mc` 后的完整内容通过
RCON 发送。不能省略 `say` 直接写 `/mc 大家好`，否则 Minecraft 会将“大家好”视为未知命令。
日志监控启用后，玩家聊天、加入、离开、死亡和进度事件会转发到发起连接的管理员 QQ 私聊。

> [!NOTE]
> `plugins/minecraft/config.json` 只保存 `host`、`port` 和可选 `log_file`；同名 RCON
> 密码必须放在 `config/secrets.json -> plugins.minecraft.<配置名>`，不能作为聊天参数传递。
> Source RCON 不加密，建议服务端仅监听 `127.0.0.1`，跨主机时使用 SSH 隧道，并禁止把
> RCON 端口暴露给不可信网络。若整块响应在等待续包时超时，回复会明确提示可能不完整。

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

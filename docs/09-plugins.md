# 插件功能介绍

本文档详细介绍 XiaoQing 中所有可用插件的功能、命令和配置说明。

## 目录

- [插件功能介绍](#插件功能介绍)
  - [目录](#目录)
  - [核心插件](#核心插件)
    - [bot_core - 核心命令](#bot_core---核心命令)
    - [pendo - 个人时间与信息管理中枢](#pendo---个人时间与信息管理中枢)
      - [命令列表](#命令列表)
      - [使用示例](#使用示例)
      - [定时任务](#定时任务-8)
      - [注意事项](#注意事项-2)
    - [echo - 回显示例](#echo---回显示例)
  - [聊天插件](#聊天插件)
    - [xiaoqing\_chat - 小青拟人聊天](#xiaoqing_chat---小青拟人聊天)
      - [核心特性](#核心特性)
      - [命令列表](#命令列表-1)
      - [配置项](#配置项)
      - [使用说明](#使用说明)
    - [smalltalk - 闲聊插件](#smalltalk---闲聊插件)
      - [命令列表](#命令列表-2)
      - [使用示例](#使用示例-1)
      - [配置说明](#配置说明)
    - [chat - AI 对话](#chat---ai-对话)
      - [使用示例](#使用示例-2)
    - [voice - 语音功能](#voice---语音功能)
      - [功能特性](#功能特性)
      - [使用示例](#使用示例-3)
  - [天文科学](#天文科学)
    - [apod - 每日天文图](#apod---每日天文图)
      - [定时任务](#定时任务)
    - [arxiv\_filter - arXiv 论文筛选](#arxiv_filter---arxiv-论文筛选)
      - [定时任务](#定时任务-1)
      - [技术说明](#技术说明)
    - [chime - FRB 重复暴监测](#chime---frb-重复暴监测)
      - [定时任务](#定时任务-2)
    - [dict - 天文学词典](#dict---天文学词典)
      - [使用示例](#使用示例-4)
    - [ads\_paper - 论文与文献管理](#ads_paper---论文与文献管理)
      - [使用示例](#使用示例-14)
      - [注意事项](#注意事项-1)
    - [astro\_tools - 天文计算工具箱](#astro_tools---天文计算工具箱)
      - [使用示例](#使用示例-15)
    - [color - 颜色查询](#color---颜色查询)
      - [参数选项](#参数选项)
      - [使用示例](#使用示例-5)
  - [实用工具](#实用工具)
    - [choice - 随机选择](#choice---随机选择)
      - [使用示例](#使用示例-6)
    - [memo - 笔记管理](#memo---笔记管理)
      - [使用示例](#使用示例-7)
    - [wolframalpha - 万能计算器](#wolframalpha---万能计算器)
      - [参数选项](#参数选项-1)
      - [使用示例](#使用示例-8)
    - [shell - 终端命令](#shell---终端命令)
      - [使用示例](#使用示例-9)
    - [url\_parser - 链接解析](#url_parser---链接解析)
    - [jupyter - 代码执行](#jupyter---代码执行)
      - [使用示例](#使用示例-16)
    - [adnmb - A岛匿名版](#adnmb---a岛匿名版)
      - [使用示例](#使用示例-17)
    - [qingssh - SSH 远程控制](#qingssh---ssh-远程控制)
      - [功能特性](#功能特性-2)
      - [使用示例](#使用示例-13)
      - [注意事项](#注意事项)
  - [外部服务](#外部服务)
    - [github - GitHub Trending](#github---github-trending)
      - [参数选项](#参数选项-2)
      - [定时任务](#定时任务-3)
      - [使用示例](#使用示例-10)
    - [earthquake - 地震快讯](#earthquake---地震快讯)
      - [定时任务](#定时任务-4)
    - [signin - 自动签到](#signin---自动签到)
      - [支持平台](#支持平台)
      - [定时任务](#定时任务-5)
      - [使用示例](#使用示例-11)
    - [twitter - Twitter 图片](#twitter---twitter-图片)
      - [定时任务](#定时任务-6)
  - [娱乐游戏](#娱乐游戏)
    - [qingpet - QQ群宠物养成系统](#qingpet---qq群宠物养成系统)
      - [核心特性](#核心特性)
      - [命令列表](#命令列表-1)
      - [定时任务](#定时任务-9)
      - [使用示例](#使用示例-18)
    - [guess\_number - 猜数字游戏](#guess_number---猜数字游戏)
      - [游戏流程](#游戏流程)
    - [minecraft - MC 服务器通信](#minecraft---mc-服务器通信)
      - [功能特性](#功能特性-1)
      - [使用示例](#使用示例-12)
      - [定时任务](#定时任务-7)
  - [插件统计](#插件统计)
  - [另请参阅](#另请参阅)

---

## 核心插件

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

强大的个人时间与信息管理插件，支持日程管理、待办事项、笔记、日记等功能。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **AI 智能解析** | 日程添加自动识别时间、地点、提醒设置 |
| **多轮对话** | 支持会话式交互，自然流畅的操作体验 |
| **隐私保护** | 支持群聊隐私模式，敏感内容转私聊 |
| **智能提醒** | 支持单次、重复、提前多种提醒方式 |
| **定时简报** | 每日/晚间简报，日记提醒自动推送 |
| **Markdown 导入导出** | 支持数据备份和迁移 |
| **撤销功能** | 支持短时间内的操作撤销 |
| **全文搜索** | 跨模块搜索日程、待办、笔记、日记 |

#### 命令列表

**日程管理 (Event)**

| 命令 | 说明 |
|------|------|
| `/pendo event add <内容>` | 添加日程（AI 解析） |
| `/pendo event list [范围]` | 查看日程 |
| `/pendo event delete <id>` | 删除日程 |
| `/pendo event edit <id> <内容>` | 编辑日程 |
| `/pendo event reminders [id\|范围]` | 查看提醒 |

**待办事项 (Todo/Task)**

| 命令 | 说明 |
|------|------|
| `/pendo todo add <内容> [cat:分类] [p:1-4]` | 添加待办 |
| `/pendo todo list [分类] [done/undone]` | 查看待办 |
| `/pendo todo done <id>` | 完成待办 |
| `/pendo todo undone <id>` | 重开待办 |
| `/pendo todo delete <id\|cat:分类>` | 删除待办 |
| `/pendo todo edit <id> <内容>` | 编辑待办 |

**笔记 (Note)**

| 命令 | 说明 |
|------|------|
| `/pendo note add <内容> [cat:分类] [#标签]` | 记录笔记 |
| `/pendo note list [cat:分类] [#标签]` | 查看笔记 |
| `/pendo note view <id>` | 查看笔记详情 |
| `/pendo note delete <id\|cat:分类>` | 删除笔记 |

**日记 (Diary)**

| 命令 | 说明 |
|------|------|
| `/pendo diary add [日期] <内容>` | 写日记 |
| `/pendo diary list [范围]` | 查看日记列表 |
| `/pendo diary view <日期>` | 查看日记详情 |
| `/pendo diary template` | 查看所有模板 |
| `/pendo diary <模板ID>` | 使用模板写日记 |
| `/pendo diary delete <日期>` | 删除日记 |

**搜索 (Search)**

| 命令 | 说明 |
|------|------|
| `/pendo search <关键词>` | 全文搜索 |
| `/pendo search <关键词> type=event/task/note/diary` | 按类型搜索 |
| `/pendo search <关键词> range=last7d/2026-01` | 按时间范围搜索 |

**提醒操作**

| 命令 | 说明 |
|------|------|
| `/pendo confirm <id>` | 确认提醒 |
| `/pendo snooze <id> <时间>` | 延后提醒（10m, 1h, 19:00） |

**导入导出**

| 命令 | 说明 |
|------|------|
| `/pendo export md [range] [type]` | 导出 Markdown |
| `/pendo import md` | 导入 Markdown |
| `/pendo import md preview` | 预览导入 |

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
/pendo event add 3月8日下午两点，国自然截止，提前一周和一天提醒
/pendo event add 每月18号上午十点，公积金提取，重复7个月
/pendo event list today        # 查看今日日程
/pendo event list 2026-03     # 查看三月日程
/pendo event list last7d      # 查看最近7天
```

**2. 待办管理**

```
/pendo todo add 完成论文初稿 p:1              # 紧急待办
/pendo todo add 整理数据 cat:工作 p:2         # 工作分类高优先级
/pendo todo list today                        # 今日待办
/pendo todo list 工作 done                    # 工作分类已完成
/pendo todo done 1                            # 完成第1个待办
```

**3. 笔记管理**

```
/pendo note add 直接折叠找脉冲星 cat:工作 #文章
/pendo note list cat:工作                    # 查看工作笔记
/pendo note list #文章                       # 查看带文章标签的笔记
/pendo note view 1                           # 查看详情
```

**4. 日记管理**

```
/pendo diary add 今天完成了论文初稿        # 写今天日记
/pendo diary add 2026-02-01 昨天很充实    # 补写日记
/pendo diary list week                       # 查看本周日记
/pendo diary view 2026-02-01                 # 查看详情
/pendo diary template                        # 查看模板
```

**5. 搜索功能**

```
/pendo search 脉冲星                         # 全文搜索
/pendo search 脉冲星 type=note              # 只搜笔记
/pendo search 论文 range=last7d              # 最近7天
```

**6. 提醒操作**

```
/pendo confirm 1                              # 确认提醒
/pendo snooze 1 10m                          # 延后10分钟
/pendo snooze 1 19:00                        # 延后到19点
```

**7. 数据导入导出**

```
/pendo export md                             # 导出所有数据
/pendo export md last7d                      # 导出最近7天
/pendo import md                             # 导入 Markdown
/pendo import md preview                     # 预览导入
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

#### 定时任务

- **每分钟** - 检查提醒
- **每天 8:00** - 每日简报
- **每天 21:00** - 晚间简报
- **每天 21:30** - 日记提醒

#### 注意事项

- 日程添加需要配置 LLM API 以使用 AI 解析功能
- 群聊中长消息会自动转为私聊以保护隐私
- 支持会话式交互，使用"退出"或"q"结束会话
- 所有数据存储在 `plugins/pendo/data/` 目录

---

### echo - 回显示例

简单的示例插件，用于测试和调试。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `echo` | `/echo`, `/回显` | 复读输入的文本 |
| `hello` | `/hello`, `/你好` | 打招呼 |

---

## 聊天插件

### xiaoqing_chat - 小青拟人聊天

基于 MaiBot 项目设计理念深度重构的拟人聊天插件，实现高度拟人化的对话体验。采用向量记忆检索、情绪系统、表达学习等先进特性，让对话更加自然和有趣。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **语义记忆检索** | ✅ 基于向量数据库的语义记忆检索，拥有长期记忆能力 |
| **行为规划** | LLM 智能判断是否需要回复，懂得在合适的时间说话 |
| **频率控制** | 防止刷屏，支持最小间隔、每分钟上限、连续回复冷却 |
| **表达学习** | 从对话中学习表达风格和黑话，不断进化 |
| **情绪系统** | 多维情绪（快乐、能量、好奇、耐心），影响回复风格 |
| **记忆系统** | 对话历史、事实记忆、对话摘要、情绪持久化 |
| **性能优化** | 可选安装 `faiss-cpu` 加速向量检索，未安装则使用 numpy 实现 |
| **上下文感知** | 记住之前的对话内容，能够进行连贯的多轮对话 |

#### 命令列表

| 命令 | 触发词 | 说明 | 管理员 |
|------|--------|------|--------|
| `chat_config` | `/小青配置` | 配置聊天参数 | ✅ |
| `chat_memory` | `/小青记忆` | 管理对话记忆 | ❌ |
| `chat_expression` | `/小青表达` | 查看学到的表达方式 | ❌ |
| `chat_stats` | `/小青统计` | 查看聊天统计信息 | ❌ |

#### 配置项

在 `plugins/xiaoqing_chat/data/config.json` 中配置：

```json
{
  "enable_planner": true,           // 启用 LLM 规划
  "enable_memory_retrieval": true,  // 启用向量记忆检索
  "enable_expression_learning": true, // 启用表达学习
  "enable_emotion_system": true,    // 启用情绪系统
  "reply_probability_base": 0.6,    // 群聊基础回复概率
  "reply_probability_private": 0.95, // 私聊回复概率
  "temperature": 0.8,               // LLM 温度参数
  "min_interval_seconds": 5,        // 最小回复间隔（秒）
  "max_replies_per_minute": 8,      // 每分钟最大回复数
  "consecutive_reply_cooldown": 15  // 连续回复后冷却时间（秒）
}
```

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

**3. 情绪记忆**
- 持久化情绪状态
- 情绪会影响回复风格
- 随对话动态变化

#### 使用说明

- 作为 `smalltalk_provider` 使用时，会接管所有闲聊消息
- 插件内部有完整的频率控制，不依赖全局 `random_reply_rate`
- 支持 @ 机器人触发回复
- 私聊中回复概率更高，对话更连贯

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
  "random_reply_rate": 0.05,         // 随机回复概率（仅 smalltalk 生效）
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

基于 Azure Cognitive Services 的语音插件，支持 TTS 和 STT。

#### 功能特性

- **文字转语音 (TTS)**: 支持 SSML，可自定义语音、风格、角色
- **语音转文字 (STT)**: 详细的语音识别
- **音频缓存**: 基于内容哈希的缓存机制

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `tts` | `/语音`, `/念`, `/tts` | 文字转语音 |

#### 使用示例

```
/语音 你好，我是小青
/tts Hello World
```

---

## 天文科学

### apod - 每日天文图

获取 NASA 每日天文图（Astronomy Picture of the Day）。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `apod` | `/apod`, `/每日一天文图` | 获取今日天文图 |

#### 定时任务

- 每天 **13:30** 自动推送到配置的群

---

### arxiv_filter - arXiv 论文筛选

基于 BERT 模型的 arXiv 论文智能筛选插件。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `arxiv` | `/arxiv`, `/论文` | 获取今日推荐论文 |

#### 定时任务

- 周一至周五 **11:00** 自动推送

#### 技术说明

使用预训练的 BERT 模型对当日 arXiv 论文进行相关性评分和筛选。

---

### chime - FRB 重复暴监测

监测 CHIME 望远镜发现的快速射电暴（FRB）重复暴。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `chime` | `/chime`, `/frb` | 查看最新 FRB 重复暴 |

#### 定时任务

- 每天 **9:00** 和 **21:00** 自动检测并推送新发现

---

### dict - 天文学词典

天文学专业术语词典查询。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `dict` | `/dict`, `/词典`, `/字典` | 查询天文术语 |

#### 使用示例

```
/dict galaxy           # 查询"galaxy"
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

全面的天文计算工具集，支持时间转换、坐标转换、天体查询、单位转换、公式速查等功能。

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

#### 使用示例

```
/颜色 -n 天青           # 查询中国传统色"天青"
/color -r 255,128,0    # 查询 RGB 颜色
/色彩 -s G2V           # 查询太阳光谱型颜色
```

---

## 实用工具

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

#### 使用示例

```
/选择 吃啥 火锅 烤肉 披萨
/决定 去不去 去 不去
/choice 抽奖 小明 小红 小张 -n 3    # 选择3个
/choice 问题 选项1 选项2 -u          # 去重选择
/choice 问题 选项1 选项1 选项2       # 加权选择（选项1权重更高）
```

---

### memo - 笔记管理

个人笔记管理，支持分类、搜索、删除。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `memo` | `/memo`, `/笔记`, `/note` | 笔记操作 |

#### 使用示例

```
/memo help             # 查看用法
/memo add 买菜         # 添加笔记
/memo list             # 列出所有笔记
/memo search 买        # 搜索笔记
/memo del 1            # 删除笔记
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

### shell - 终端命令

在服务器上执行终端命令。

| 命令 | 触发词 | 说明 |
|------|--------|------|
| `shell` | `/shell`, `/sh`, `/exec` | 执行命令 |
| `shell list` | `/shell list`, `/shell 列表` | 查看白名单 |

#### 功能特性

- **命令白名单**：仅允许执行白名单中的命令（可配置）
- **执行超时**：默认 30 秒超时
- **输出限制**：输出最大 4000 字符
- **安全防护**：禁止命令链接符（&&, ||, ;, |）除非在白名单

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

#### 使用示例

```
/sh ls -la
/sh python --version
/sh ping -c 3 google.com
/sh help                    # 显示帮助
/sh list                    # 查看白名单
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

---

### qingssh - SSH 远程控制

强大的 SSH 远程控制插件，支持交互式会话、命令执行和配置管理。

**核心特性**:
- **环境保持**: 支持 `cd` 切换目录和 `export` 环境变量
- **流式输出**: 实时推送长命令的执行结果
- **用户隔离**: 支持多用户、多群组同时与不同服务器交互
- **配置管理**: 支持导入 `~/.ssh/config`，支持密钥和密码认证
- **用户名支持**: ✅ 支持 `user@server` 格式指定连接用户名

#### 连接管理逻辑（核心机制）

本插件采用严格的 **用户 + 群组 + 服务器** 三维隔离机制，确保连接的安全性和独立性：

1.  **连接隔离**：
    - 连接标识符 (Key) = `用户ID : 群ID : 服务器名`
    - **这意味着**：你在群 A 连接了服务器，去群 B 是**无法**直接使用的（需要重新连接）。
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
| `ssh断开` | `/ssh断开`, `/disconnect` | 断开当前会话的连接 |
| `ssh列表` | `/ssh列表`, `/list` | 查看已保存的服务器 |
| `ssh状态` | `/ssh状态`, `/status` | 查看当前活跃的连接数和详情 |
| `ssh添加` | `/ssh添加`, `/add` | 添加服务器配置 |
| `ssh删除` | `/ssh删除`, `/remove` | 删除服务器配置 |
| `ssh导入` | `/ssh导入`, `/import` | 从 ~/.ssh/config 导入 |
| `sshconfig` | `/sshconfig` | 查看 ~/.ssh/config |
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

## 外部服务

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
      
---

### jupyter - 代码执行

强大的 Python 代码执行环境，支持绘图。
      
| 命令 | 触发词 | 说明 |
|------|--------|------|
| `jupyter` | `/jupyter`, `/py` | 执行 Python 代码 |
| `jupyter_kernel` | `/jupyter_kernel`, `/kernel` | 管理运行内核 |
      
#### 功能特性
      
- **代码执行**: 支持异步、并发执行 Python 代码
- **绘图支持**: matplotlib 绘图自动转换为图片发送
- **持久内核**: 变量状态在会话间保留
- **自动管理**: 空闲自动关闭，按需自动启动
      
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

## 娱乐游戏

### qingpet - QQ群宠物养成系统

完整的虚拟宠物养成游戏，支持领养、喂养、互动、装扮、交易等丰富功能。

#### 核心特性

| 特性 | 说明 |
|------|------|
| **宠物养成** | 领养、喂养、清洁、玩耍、睡眠、训练 |
| **状态系统** | 饱食度、心情、清洁度、健康、体力、经验 |
| **成长进化** | 宠物随时间成长，等级提升 |
| **物品系统** | 食物、玩具、药品、装扮等丰富道具 |
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
- **日志监控**: 自动读取服务器日志

#### 使用示例

```
/mc help                # 显示帮助
/mcconnect 127.0.0.1:25575 password          # 连接服务器
/mcconnect 127.0.0.1:25575 password /path/to/log # 连接服务器并指定日志路径
/mc status             # 查看连接状态
/mc list               # 查看在线玩家
/mc time set day       # 发送命令到服务器
/mc 大家好             # 向服务器发送消息
/mcdisconnect          # 断开连接
```

#### 定时任务

- 每 **5 秒** 检查一次服务器日志

---

## 插件统计

| 分类 | 数量 | 插件 |
|------|------|------|
| **核心** | 3 | bot_core, echo, pendo |
| **聊天** | 4 | xiaoqing_chat, smalltalk, chat, voice |
| **天文科学** | 7 | apod, arxiv_filter, chime, dict, ads_paper, astro_tools, color |
| **实用工具** | 8 | choice, memo, wolframalpha, shell, url_parser, jupyter, adnmb, qingssh |
| **外部服务** | 4 | github, earthquake, signin, twitter |
| **娱乐游戏** | 3 | qingpet, guess_number, minecraft |
| **总计** | **29** | |

---

## 另请参阅

- [插件开发指南](03-plugin-development.md)
- [配置说明](06-configuration.md)
- [核心模块](04-core-modules.md)

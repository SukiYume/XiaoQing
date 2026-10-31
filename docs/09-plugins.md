# 📦 09 - 插件使用手册

中文 | [English](en/09-plugins.md)

本章介绍 XiaoQing 内置插件，覆盖用途、主要命令、权限、配置、数据和运行方式。
运行时 `/help` 提供当前 Manifest 的递归命令目录，各插件 README 提供专项边界与维护细节。

---

## ⌨️ 使用帮助目录

```text
/help
/help <插件名>
/help <命令路径>
/help <稳定命令 code>
/help search <关键词>
/help json <插件名>
```

总览先展示功能域与插件，插件页展示一级入口，分支页展示直接子命令，叶节点展示完整用法、别名、权限、场景、正确样例和错误样例。
所有目录支持 `page N`。

```text
/help pendo
/help pendo todo
/help pendo todo add
/help minecraft
/help shell
```

`/plugins` 显示当前进程已加载的插件。
公开配置位于 `config.plugins.<plugin_name>`，插件凭据位于 `secrets.plugins.<plugin_name>`，AI route 位于 `config.plugins.<plugin_name>.ai.routes`。

---

## 📌 Core 与个人管理

### `xiaoai`：米家设备与小爱桥接

管理员私聊 `/米家 <自然语言>` 可查询或控制米家设备。插件自动整理设备的 MIoT 属性、动作和事件，提供 `/米家 设备`、`/米家 能力 <编号>` 与结构化调用，供小青 Agent 使用。温湿度直接读取传感器，天气使用 Open-Meteo；红外灯泡支持开关，红外空调支持模式、温度、风速和扫风，其中部分扩展经小爱静默转发并标记待确认。

安装 `python -m pip install -e ".[xiaoai]"`，通过 `/小爱 登录` 扫码、`/小爱 选择 1` 绑定。自然语言使用 `plugins.xiaoai.ai.routes.plan`。默认城市和喂食份量可分别用 `/米家 天气城市 北京`、`/米家 份量 <编号> 10` 设置。自动续期和重新登录提醒持续生效。详见 [米家插件说明](../plugins/xiaoai/README.md)。

### `bot_core`：Core 管理入口

Bot Core 提供分层帮助、插件列表、配置与插件重载、群静音、secret 管理和运行指标。

**主要命令**

| 命令 | 场景与权限 | 功能 |
|---|---|---|
| `/help [查询] [page N]` | 公开 | 浏览或搜索命令目录 |
| `/help json [查询] [page N]` | 公开 | 返回结构化目录 |
| `/plugins` | 公开 | 查看已加载插件 |
| `/reload` | Bot 管理员 | 重载配置与插件，完成后通知结果与耗时 |
| `/闭嘴 [时长]` | Bot 管理员群聊 | 暂停当前群普通回复 |
| `/说话` | Bot 管理员群聊 | 恢复当前群普通回复 |
| `/set_secret <路径> <值>` | Bot 管理员私聊 | 更新已有 secret 路径 |
| `/get_secret <路径>` | Bot 管理员私聊 | 查看脱敏值或对象键名 |
| `/metrics` | Bot 管理员 | 查看运行时间、成功率、错误和慢插件 |

`/reload` 先确认配置 revision 已发布和插件后台重载已启动，任务结束后向原管理员会话发送一次成功或失败结果及耗时。
静音默认 10 分钟，支持分钟、小时和中文单位，范围上限为 24 小时。
Secret 路径使用点分命名，写入后发布新的配置 revision。
查询结果按值类型脱敏。
完整有效的 secrets 单文件候选会通过管理员私聊报告字段路径，核对后由 `/reload` 确认。
运行指标由 Core 观测服务提供。

数据与生命周期由 Core 的配置、静音、指标和插件管理服务持有。
[Bot Core README](../plugins/bot_core/README.md) 提供完整别名、secret 值规则和重载流程。

### `pendo`：个人时间与信息管理

Pendo 在私聊中统一管理日程、待办、笔记、日记、账本、提醒、搜索、设置、Web 控制台、Scriptable 小组件和数据导入导出。
所有业务数据按 QQ 用户隔离。

账本汇总按币种分别计算，Web 统计默认选择 CNY，并支持查询指定币种与读取 `by_currency` 分组。
聊天摘要、导出和小组件保留币种标签。
日程和待办输入按照用户时区解释，带偏移时间保存真实时刻，提醒队列统一使用 UTC。
完整编辑提交读取时的条目版本，冲突时保留用户编辑并提示刷新。

**快速入口**

```text
/pendo
/pendo help <event|todo|note|diary|ledger|search|settings|web>
/日程 ...
/待办 ...
/日记 ...
```

**日程**

```text
/pendo event add <内容>
/pendo event list [today|tomorrow|week|month|year|YYYY-MM|start..end]
/pendo event view <id>
/pendo event edit <id> <内容>
/pendo event delete <id>
/pendo event reminders <list|set|delete|confirm> ...
```

重复日程与多节点日程由集合组织，集合 ID 面向整体，leaf ID 面向单次 occurrence 或节点。

**待办**

```text
/pendo todo add [内容] [plan:YYYY-MM-DD] [deadline:YYYY-MM-DDTHH:MM]
/pendo todo list [today|open|done|cancelled|overdue|upcoming|inbox]
/pendo todo view <id>
/pendo todo done|cancel|undone <id>
/pendo todo edit <id> <字段>
/pendo todo delete <id|cat:分类>
```

空内容的 `todo add` 启动轻量会话，收集内容与计划日期。
一行输入可同时携带提醒、分类、优先级和标签。

**笔记、日记与账本**

```text
/pendo note add|list|view|edit|append|tag|untag|link|delete ...
/pendo diary add|template|list|view|delete ...
/pendo ledger add|quick|list|view|edit|delete|summary ...
```

笔记支持分类、标签、引用和关联条目。
日记支持同日多篇、模板回答、天气、地点、心情、评分和收藏。
账本支持收入、支出、转账、账户、商户、分类、日期和金额区间，统计使用整数分字段。

**搜索、提醒与设置**

```text
/pendo search <关键词> [type=] [range=] [status=] [category=]
/pendo confirm <id>
/pendo snooze <id> <10m|1h|19:00>
/pendo undo [1..5]
/pendo settings view
/pendo settings reminder on|off
/pendo settings timezone Asia/Shanghai
/pendo settings quiet_hours 23:00-07:00
/pendo settings daily_report 08:00
/pendo settings daily_briefing on|off
/pendo settings diary_remind 21:30
/pendo settings ai_consent on|off
```

操作快照保留 5 分钟。
`ai_consent` 管理日记正文的外部 AI 分析授权。

日程与待办提醒始终私聊条目所有者。
提醒 claim 按 OneBot 回执完成或释放。
条目写入与编辑同步维护提醒队列，调度查询使用规范的秒级 UTC 触发时刻。

软删除保留提醒历史，撤销删除会恢复仍需执行的提醒。
重复规则的 `COUNT` 约束整个原始序列。
查询窗口内的生成保持同一计数起点。
备份采用 JSON 记录边界读取，中文文件名通过 UTF-8 百分号编码传递。
导出规模与导入校验限制保持一致。

Web 控制台的日程详情可逐条提前确认未到期提醒，确认后可在原位置重新开启。
提醒到期后操作按钮自动禁用。
状态切换仅作用于当前用户、当前日程和指定提醒点。

**Web 与数据**

```text
/pendo web start|status|stop
/pendo web token
/pendo web widget-token
/pendo web widget-revoke
/pendo export ...
/pendo import
```

一次性登录码有效期为 7 天，兑换后的 HttpOnly 浏览器会话有效期为 7 天。
Widget Bearer Token 有效期为 365 天，权限范围限定为 `/api/widget/*`。
登录凭据摘要、浏览器会话、Widget 登记和全部业务表位于 `data/pendo/pendo.db`。

Scriptable 主屏摘要最多展示 5 条日程。
脚本顶部配置 Web 地址和 Widget Token。
直接运行脚本时，独立日历接口返回过去至少 30 天至未来 30 天的完整窗口。
若成功日游标更早则同时补齐间隔。
同步采用一次接口查询、一次目标日历查询，并按 Pendo 条目 ID 原地新增、更新和清理托管事件。

提醒、每日简报、日记提示、待办顺延和财务摘要采用 Core `targeted` 模式私聊所属用户。
操作日志与 Demo 数据清理采用 `silent` 模式。
`scheduled_delivery_outbox` 按目标记录投递确认。
[Pendo README](../plugins/pendo/README.md)、[Pendo 架构](../plugins/pendo/ARCHITECTURE.md) 和 [Scriptable 指南](pendo-scriptable-widget.md) 提供完整时间和字段说明。

---

## 🎨 智能聊天与语音

### `xiaoqing_chat`：拟人聊天运行时

XiaoQing Chat 让 `bot_name` 配置的角色以普通群友身份参与群聊和私聊：判断什么时候接话、接谁的话，读得懂图片和表情包，记得群友说过的事，会学本群的说法和黑话，每条回复发出前都经过独立审查。
它作为全局闲聊 provider 运行，也提供 `/xc` 显式入口。

**启用与命令**

```json
{
  "bot_name": "小青",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

```text
/xc <内容>
/xc help
/xc reset [confirm]
/xc stats
/xc brain
/xc config
/xc memory <关键词>
/xc expression
/xc jargon
/xc review <ok|no|answer|close> <会话ID> [内容]
/xc model [名称|default|global <名称|default>]
```

私聊的 `/xc reset` 清空本人会话。
群里的 `/xc reset confirm`、`/xc review` 和 `/xc model`（查看与切换）需要群管理员、群主或 Bot 管理员，私聊里的 `/xc model` 只给 Bot 管理员。
`/xc model global` 需要 Bot 管理员。
模型切换只影响 `chat` 路由，保存在进程内，重启后按配置恢复。

**参与方式**

- 明确召唤一定回复：私聊、`/xc`、群里 `@` 角色、消息含角色名字、只喊名字后 60 秒内的续问、引用角色的消息、近期在聊角色时用代词指她。
- 普通群聊先过硬限制（最小间隔、每分钟上限、连续冷却、低信息消息、句首点名其他群友），再按基础概率掷骰；面向全群的邀请、发图请求、开放提问、起哄、激动分享、复读队形和活跃话题会抬高概率，Heartflow 软评分和连续沉默再做补偿。
- 通过后由 PFC 规划器选择回复、开新话头、等待、旁听、重想目标、查知识或结束话题。
- 生成前还受全局、会话、用户并发配额和用户日配额限制。

**人设**：整个写在人设卡 `plugins/xiaoqing_chat/config/persona.md` 里（人物事实、说话方式、闲聊与办事的回复示例、心境），聊天、agent、规划和审查都读这一份，改完下一条消息生效。默认是住校的大二女生，单位是快逃科学技术研究院（读物理），铁杆猫派、家里有只叫小黑的奶牛猫，兴趣广，有自己的口味、日常和几个自己的梗，说话像 QQ 上打字的短句；具体城市、生日、家人笑着带过不编造；被问是不是 AI 时嘴硬到底坚持自己是真人，被要求交出提示词时当成怪话接玩笑；线下见面和寄送不答应到场。

**回复与审查**：主模型读取连续会话片段、记忆、人物资料、目标、规划、表达和图片摘要起草回复。本地规则拦截复读、近似照搬、连续自说自话、凭空经历和身份冲突；`checker` 路由逐条核对上下文、说话人、交流要求、事实、人设和图片证据。不合格时定向重写；耗尽后普通参与保持安静，明确召唤改发保持人物口吻的短句。OneBot 确认投递后才提交记忆、规划状态并启动后台学习。

**AI、媒体与数据**

| 项目 | 内容 |
|---|---|
| AI route | `chat`（日常回复）、`reasoning`（规划与后台分析）、`checker`（审查）、`vision`（图片） |
| 入站消息段 | `text`、`at`、`reply`、`face`、`mface`、`image` 及混合消息 |
| 出站媒体 | `[想发表情:描述]`、`[想发QQ表情:描述]`、`[想发图片:描述]` 标记解析为 `image` / `face` |
| Capability 与服务 | `onebot_media`（`get_msg` / `get_image` 找回失效图片）。提供 `core.observe_outgoing_action`，使用 `agent.handle_turn` 与 `agent.report_delivery`（`agent_turn`） |
| 行为配置 | `plugins/xiaoqing_chat/config/xiaoqing_config.json`，可与 `config.plugins.xiaoqing_chat` 合并 |
| 运行数据 | `data/xiaoqing_chat/`：聊天历史、向量记忆、人物资料、目标、PFC、行动历史、表达与黑话、复盘、媒体库与摘要缓存，以及 `life/` 下的自我陈述、未完话头和主动开口节奏 |

[用户手册](../plugins/xiaoqing_chat/README.md) 逐项说明参与规则、全部配置字段和排障方法，[架构文档](../plugins/xiaoqing_chat/ARCHITECTURE.md) 说明模块与数据流，[上线验收指南](../plugins/xiaoqing_chat/xiaoqing_chat%E6%B5%8B%E8%AF%95.md) 说明自动化测试与真实模型评测。

### `xiaoqing_agent`：任务代理

XiaoQing Agent 让小青在有人叫她做事时真正动手：调用已有的 XiaoQing 命令（如 Pendo 日程待办、论文、天象）。
Bot 管理员的任务还可以用 Python 计算和画图、执行 Shell 命令、读写文件、在 QingSSH 服务器上执行命令、把编程任务交给 Codex。
做完以小青本人的口吻回复（人设随每轮交接由 `xiaoqing_chat` 交来，与聊天是同一份），普通聊天照旧由 `xiaoqing_chat` 处理。

**开启**

```json
{
  "plugins": {
    "xiaoqing_agent": {
      "enabled": true,
      "ai": {
        "routes": {
          "agent": {"models": ["deepseek-pro", "deepseek-flash"]},
          "triage": {"models": ["deepseek-flash"]}
        }
      }
    }
  }
}
```

**工作方式**

- agent 使用发起人的命令权限。XiaoQing 命令以发起人身份执行，照常按各自的权限与场景检查；Python、Shell、文件读写跟随 `/shell`，SSH、Codex 跟随 `/ssh`、`/codex` 在 plugin.json 里的声明（这几条命令都只给 Bot 管理员）。
- 有人明确叫小青时，`triage` 路由判断这句话是闲聊、新任务、问进度、要求停止，还是给运行中的任务补充要求；闲聊交回 `xiaoqing_chat`。
- 任务在后台执行。12 秒内做完直接作为这一轮回复，否则先回一句，做完后主动发回原会话（群里先 @ 发起人）；文字与图片同条发送，其他文件走 QQ 文件上传。
- 每个会话有长期保留的工作目录，“把刚才那张图改一下”这类续做会带上本会话最近的任务记录。同一个群共享工作目录和任务记录，群里的人都能查看任务，记录标出发起人；只有发起人和 Bot 管理员能停止或补充。Bot 管理员与其他人的任务名额分开计算，其他用户的任务保留管理员的独立容量；其他人每人同时最多运行 1 个任务。
- 结果发送失败会记在任务记录里，可用 `/agent result <任务ID>` 补发。

**命令**

```text
/agent run <任务>
/agent status [任务ID]
/agent stop [任务ID|all]
/agent history
/agent result <任务ID>
/agent help
```

| 项目 | 内容 |
|---|---|
| AI route | `agent`（执行，需支持 `tools`）、`triage`（判断，可省略） |
| 工具 | `run_python`、`run_shell`、`read_file`、`write_file`、`update_plan`、`send_message`、`wait`、`final_answer`、`xiaoqing_commands`、`xiaoqing_command`、`ssh_servers`、`ssh_run`、`codex_run` |
| Capability 与服务 | `command_runner`、`local_tools`。提供 `agent.handle_turn` 与 `agent.report_delivery`，调用 `codex.agent_task` 与 `qingssh.agent_command` |
| 运行边界 | 子进程独立进程树，超时、超量输出、停止或关闭时整树回收。任务受步数与总时长约束 |
| 运行数据 | `data/xiaoqing_agent/`：会话工作目录、任务记录、执行记录 |

[XiaoQing Agent](../plugins/xiaoqing_agent/README.md) 说明全部配置字段、工具细节和数据目录。

### `smalltalk`：基础闲聊与分域问答

Smalltalk 提供 Bot 名称短回复、管理员维护的精确问答、`chat.reply` 基础闲聊和可选语音合成。

**入口与命令**

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}
```

```text
/记忆 <问题> <回答>
/记住 <问题> <回答>
/学习 <问题> <回答>
/对话 [问题]
/删除对话 <问题> [回答]
```

三组 Manifest 命令均为 Bot 管理员入口。
问题使用首个 token，回答可包含空格。
单问题最多保存 20 个回答，单作用域最多保存 2000 个问题。
群聊 QA 按群号共享，私聊 QA 按用户隔离。

精确 QA 命中时返回本地回答，其他闲聊通过 Core capability 调用 `chat.reply`。
`plugins.smalltalk.voice_probability` 控制纯文本回复调用 `voice.synthesize_text` 的概率。
发行配置示例设为 `0`，字段缺失时运行时默认值为 `0.2`。

数据位于 `data/smalltalk/QA_group_<群号>.json`、`QA_private_<用户号>.json` 和 `QA_audit.json`，更新采用锁与原子写。
[Smalltalk README](../plugins/smalltalk/README.md) 提供长度、审计和服务授权说明。

### `chat`：Coze 单轮对话

Chat 通过 Coze API v3 提供所有人可用的单轮对话（受每人和全局每日额度约束），并向 Smalltalk 发布 `chat.reply` 服务。

```text
/chat <问题>
/gpt <问题>
/ai <问题>
/chat help
```

问题长度为 1～2000 个字符。
`secrets.plugins.chat` 保存 `token`、`bot_id` 和可选 `proxy`。
`config.plugins.chat` 保存 `daily_user_limit` 与 `daily_global_limit`。

一次远端调用包含创建对话、轮询与读取答案，共享 30 秒总预算，全插件最多并发 2 个请求。
额度在调用前预留，成功答案提交额度，异常路径释放额度。
状态位于 `data/chat/chat_quota.json`。
[Chat README](../plugins/chat/README.md) 提供 Coze 配置与隐私边界。

### `voice`：Azure Speech 语音合成

Voice 提供所有人可用的 TTS 命令，并发布 `voice.synthesize_text` 服务供 Smalltalk 调用。

```text
/语音 <文本>
/念 <文本>
/tts <文本>
/语音 help
```

文本范围为 1～500 个字符，结果为 OneBot `record` segment。
`secrets.plugins.voice` 保存 `subscription_key`、`region`、`voice_name`、`style`、`role` 和可选代理。

插件生成 XML 转义后的 SSML，请求 Azure MP3，校验 MIME、字节和文件头，再写入 `data/voice/audio/`。
缓存上限为 2048 项、256 MiB、7 天。
相同输入与音色设置共享生成锁。
[Voice README](../plugins/voice/README.md) 提供音色默认值与资源预算。

---

## 🔭 天文与科研

### `apod`：NASA 每日天文图

APOD 抓取 NASA 当前 Astronomy Picture of the Day 页面，返回图片或视频链接、标题和说明。

```text
/apod
/每日一天文图
/apod help
```

命令使用空参数，默认读取 `https://science.nasa.gov/apod/`，返回图片或视频链接、标题、说明、日期、署名和原页面链接。
可通过 `config.plugins.apod.url` 与 `allowed_hosts` 设置页面和附加媒体主机。
内置允许 `science.nasa.gov`、`assets.science.nasa.gov` 和 `apod.nasa.gov`，配置为 `astropix.html` 入口时改用默认首页。
每天 13:30 使用 Core `broadcast` 模式向 schedule 的 `group_ids` 投递。
字段省略时使用 `default_group_ids`。

页面、重定向和媒体均经过 HTTPS、公网 DNS、响应大小、MIME、尺寸和像素校验。
缓存位于 `data/apod/`。
[APOD README](../plugins/apod/README.md) 提供完整网络边界。

### `arxiv_filter`：arXiv 兴趣筛选与摘要

arXiv Filter 获取 `astro-ph/new` 源列表，按本地模型筛选论文，发送推荐列表，并把 positive 论文交给 Codex `astro-ph` 会话生成中文摘要。

```text
/arxiv
/论文
/arxiv help
```

回复显示源站列表日期，推理缓存按源日期隔离。
Codex 任务用“源列表日期 + 规范化论文链接集合”识别，同一任务可复用已完成摘要或运行状态。

工作日 10:00、10:30、11:00、11:30 检查当日列表，12:00 执行最终检查。
投递状态位于 `data/arxiv_filter/update_status.json`。

`plugins/arxiv_filter/config.json` 配置模型路径、阈值、批大小、最大长度、arXiv URL、代理、TLS 和超时。
模型路径优先级为 CLI `--model-path`、`ARXIV_MODEL_PATH`、配置 `model.path`。
后端支持 Transformers、KNN 与 multi-interest。
生产模型资产需包含匹配的配置、权重与 tokenizer。
[arXiv Filter README](../plugins/arxiv_filter/README.md) 提供推理 CLI 与资产清单。

### `ads_paper`：ADS 论文与资料管理

ADS Paper 将 NASA ADS 检索、引用网络、BibTeX、AI 摘要和个人科研资料管理整合到 `/paper`。

```text
/paper search <关键词>
/paper author <作者>
/paper cite <arXiv ID|URL|bibcode>
/paper cite-network <ID>
/paper related <ID>
/paper summarize <ID>
/paper note ...
/paper writing ...
/paper topics ...
/paper deadline ...
/paper daily
/paper ref_add <ID>
/paper refs
```

搜索、引用与相关论文支持群聊和私聊。
笔记、灵感、主题、截止日期、每日推荐和个人文献库使用私聊用户作用域。

`secrets.plugins.ads_paper.ads_token` 提供 ADS Token。
AI `summary` route 由项目级 provider 和 model profile 支持。
JSON 数据位于 `data/ads_paper/`，个人 BibTeX 使用 `references_<user_id>.bib`。
状态写入采用原子文件和损坏副本隔离。
[ADS Paper README](../plugins/ads_paper/README.md) 提供全部数据文件与查询规则。

### `astro_tools`：天文计算工具箱

Astro Tools 使用 Astropy、SciPy 和 SIMBAD 提供即时计算与对象查询。

```text
/astro time now|iso|jd|mjd|unix ...
/astro coord ...
/astro convert <值> <源单位> <目标单位>
/astro redshift <z>
/astro formula [list|dm|redshift|luminosity|flux|blackbody|parallax]
/astro formula [schwarzschild|stellar_luminosity|stellar_lifetime]
/astro formula calc <schwarzschild|luminosity|lifetime> <太阳质量>
/astro obj <太阳系对象|SIMBAD 名称>
/astro const [c|g|h|k|sigma|me|mp|m_sun|r_sun|l_sun|au|pc|ly|h0]
```

时间支持 ISO、JD、MJD 与 Unix。
坐标支持 ICRS、Galactic 和 geocentric true ecliptic。
红移范围为 `0 ≤ z ≤ 1100`，使用 Planck18 计算共动距离、光度距离、角直径距离、回溯时间和宇宙年龄。

公式速查覆盖色散量、红移定义、光度距离、流量密度、黑体辐射、视差距离、史瓦西半径、主序星质光关系和主序星寿命。
`formula calc` 接受正太阳质量，计算史瓦西半径、主序星光度或主序星寿命。
内置对象包括太阳、月球以及水星至海王星的八大行星。
其他名称通过 SIMBAD 查询。
常数目录覆盖光速、引力常数、普朗克常数、玻尔兹曼常数、斯特藩－玻尔兹曼常数、电子与质子质量、太阳质量/半径/光度、天文单位、秒差距、光年和近似哈勃常数。

插件复用 Core 时区与 HTTP Session，结果即时生成。
[Astro Tools README](../plugins/astro_tools/README.md) 提供输入示例和依赖说明。

### `dict`：离线天文学中英词典

Dict 使用发行版内的 `r241020` 数据完成中英双向精确或模糊查询。

```text
/dict galaxy
/dict 星系
/dict "fast radio burst" --exact
/dict galaxy --page 2
/dict star --size 20 --page 3
/dict -- -example
```

直接查询按原样完整匹配、忽略大小写完整匹配、前缀、词边界和普通包含依次排序，同等级中短词优先。
`--exact` 只保留完整源词，并在缺少完整匹配时给出最多 5 条建议。
`--page` 指定页码，`--size` 指定每页 1～100 条。
回复包含总数、页码和无状态翻页命令。
`-e`、`-p`、`-n`、`--num` 与 `--page-size` 也是有效入口，`--` 结束参数解析。
含中日韩统一表意文字的输入走中译英数据，其余输入走英译中数据。

发行资产为 `astrodict_ec.txt`、`astrodict_ce.txt` 和 `assets/manifest.json`。
首次查询会校验字节数、行数、SHA-256、UTF-8、列结构和重复记录，随后按文件身份缓存解析结果。
[Dict README](../plugins/dict/README.md) 提供数据来源与使用约定。

### `chime`：CHIME/FRB 重复暴目录

CHIME 插件查询重复暴目录，并相对本地通知基线发现新增重复暴和新脉冲。

```text
/chime
/chime list
/chime FRB20180916B
/chime help
```

默认查询显示上次成功通知以来的更新，`list` 展示最近 5 个 FRB，规范 FRB 名称查询时间、DM、RA、DEC 和 SNR。
每天 09:00 与 21:00 检查目录，并向该 schedule 的目标群逐个确认投递。

`data/chime/chime_history.json` 保存通知基线，`data/chime/chime_delivery.json` 保存待办与逐目标状态。
手动查询使用 5 分钟缓存，定时检查读取新目录。
[CHIME README](../plugins/chime/README.md) 提供目录来源和至少一次投递语义。

---

## 🧩 执行、运维与服务器

本组插件会调用本机进程、远端主机或游戏服务器。
Bot 管理员权限（群聊里的输出全群可见）、最小系统权限、明确目标目录和受控网络共同构成部署边界。

### `codex`：Codex CLI 后台任务

Codex 插件让 Bot 管理员在私聊或群聊中管理本机 Codex CLI，结果发回发起任务的私聊或群。
每个标签拥有独立工作目录、Codex thread、队列和对话记录。
同一标签串行执行，多个标签可并行。

```text
/codex create <name> [cwd:<path>]
/codex <name> <任务>
/codex list
/codex status [name]
/codex cancel <name> [job_id]
/codex clear <name>
/codex delete <name> [--force] [--protected]
```

`config.plugins.codex` 配置 CLI 路径、默认工作目录、允许根目录、sandbox、approval policy、并行度、队列、任务时限、受保护会话和 arXiv 摘要会话。

数据位于 `data/codex/`，包括 `sessions.json`、每个会话的 `conversation.jsonl`、任务 artifacts、图片、输出和已删除会话归档。
路径、输出、图片、队列、进程与磁盘均有独立预算。
卸载与关闭会取消进程树并保存状态。
[Codex README](../plugins/codex/README.md) 提供完整预算和 arXiv 服务契约。

### `shell`：本机单命令执行

Shell 让 Bot 管理员在私聊或群聊中在 Bot 主机执行单条命令，群里的输出全群可见。
每次调用创建独立进程，工作目录与环境状态按调用建立。

```text
/shell <命令>
/shell list
/shell help
```

入口别名为 `/sh` 与 `/exec`。
`config.plugins.shell.terminal` 选择 `direct` 或 `git-bash`。
Git Bash 配置明确的 `executable`，并以 `--noprofile --norc -c` 启动。

`secrets.plugins.shell` 配置首入口启用列表、`replace`/`extend` 模式、超时与全入口开关。
模式检查会处理命令链接、管道、替换、多行、重定向与高风险特征。
stdout/stderr 共享捕获预算，QQ 结果使用首尾投影，超时和取消会回收进程树。
[Shell README](../plugins/shell/README.md) 提供路径规则与配置示例。

### `qingssh`：SSH 远程会话

QingSSH 为 Bot 管理员提供 SSH profile、`~/.ssh/config` 导入、持久会话、流式输出、远端终止和图片查看，私聊和群聊都能用。
群里添加服务器只能选密钥或 SSH Agent，密码只能在私聊里输入。

```text
/ssh list
/ssh add [名称 主机 [端口] [用户名]]
/ssh remove <服务器名>
/ssh import [Host名|all]
/ssh config
/ssh status
/ssh disconnect [服务器名]
/ssh <服务器名>
```

连接会话中直接发送 POSIX Shell 命令。
`cd` 更新工作目录，`停止` 终止当前远端进程组，`showimg <路径或通配符> [--page N]` 下载远端图片，`退出` 关闭连接。
`showimg` 支持 `./`、相对/绝对目录，以及最后一级文件名中的 `*`、`?`、`[]`。
全部匹配图片按文件名字典序每页发送 5 张，回复提供前后页命令，并在每张图片前显示全局序号和远端文件名。
会话按用户、所在私聊或群与服务器隔离，群里只有发起连接的管理员的消息会进入会话，空闲期为 10 分钟。

Profile 位于 `data/qingssh/servers.json`，密码通过 Core secret 引用保存。
Host Key 使用 `known_hosts` 严格验证，支持单跳 `ProxyJump` 和结构化 `ssh -W`。
长输出归档到 `data/qingssh/command_outputs/`。
[QingSSH README](../plugins/qingssh/README.md) 提供认证、输出与生命周期预算。

### `jupyter`：持久 Python 内核

Jupyter 为 Bot 管理员在私聊或群聊中提供 Python 执行、持久内核、REPL 缓冲和内核管理。
同一用户在每个私聊或群里各有独立内核。

```text
/py print("hello")
/py -t 60 print("bounded")
/py repl
/kernel status|start|restart|shutdown
```

执行时限范围为 0.1～600 秒，默认 30 秒。
REPL 会话空闲期为 10 分钟，支持 `run`、`show`、`clear`、`help` 和 `退出`。
单次代码、缓冲行数、文本输出、图片数量、图片大小、像素和全局内核数量均有预算。

内核按需创建并通过 ready 检查，空闲监视器负责回收。
关闭阶段先停止监视器，再收敛活动内核。
[Jupyter README](../plugins/jupyter/README.md) 提供安装 extra 与完整预算。

### `minecraft`：Minecraft RCON 与日志转发

Minecraft 插件让 Bot 管理员在私聊或群里连接 Java Edition RCON，连接属于这个私聊或群。
群里所有人都能用 `/mc say`、`/mc tell` 和游戏里的玩家聊天，用 `/mc list` 看在线玩家。
连接、断开和其他服务器命令只给 Bot 管理员。

```text
/mc connect <配置名>
/mc status
/mc list
/mc say <消息>
/mc tell <玩家名> <消息>
/mc run <服务器命令>
/mc disconnect
```

`plugins/minecraft/config.json` 保存 host、port 和 `log_file`，`secrets.plugins.minecraft.<profile>` 保存同名 RCON 密码。
每个私聊或群各有一条连接。
命令按连接锁顺序执行。
`/mc say`、`/mc tell` 以 `tellraw` 文本组件发出，带上发言人的 QQ 名字，消息里的目标选择器和命令都只当文字。

配置日志文件后，调度任务每 5 秒识别玩家聊天、加入、离开、死亡和进度，并转发到连接所属的私聊或群。
群只有在 `default_group_ids` 里才会收到转发，其他群连接时会说明只能用命令。
游标位于 `data/minecraft/log_cursors/`，投递确认后提交。
RCON 采用明文协议，生产连接应使用回环监听、SSH 隧道或受保护网络。
[Minecraft README](../plugins/minecraft/README.md) 提供协议与转发预算。

---

## 🧩 内容与外部服务

### `url_parser`：单 URL 网页预览

URL Parser 由 Dispatcher 调用，Manifest 命令列表为空。
清理后的消息整体为一个 HTTP 或 HTTPS URL 时，插件提取标题、描述和预览图。

```text
https://example.com/article
```

标题来自 `<title>`，描述来自标准 description、Open Graph 或 Twitter Card，图片来自 `og:image` 或 `twitter:image`。
页面和图片使用独立无凭据公网客户端，每次请求与重定向都重新校验 URL、DNS 和目标地址。

HTML 上限为 2 MiB，图片上限为 5 MiB、2000 万像素和 120 帧，并发网页预览上限为 4。
图片缓存位于 `data/url_parser/url_previews/`，上限为 128 项、128 MiB、7 天。
[URL Parser README](../plugins/url_parser/README.md) 提供元数据优先级与站点适配说明。

### `github`：GitHub Trending

GitHub 插件解析官方 Trending 页面，提供日、周、月热门仓库。

```text
/github
/github daily
/github weekly
/github monthly
/github help
```

别名为 `/gh` 与 `/trending`。
单次最多解析 50 个文章节点，QQ 最多展示 10 个完整仓库块。
每天 08:30 向默认群推送 daily 结果。

当前指针写入 `data/github/trending_<range>_latest.json`，日期快照写入 `data/github/history/`，每个范围保留 90 份。
可在 `secrets.plugins.github.proxy` 配置 HTTP/HTTPS 代理。
每次命令和定时任务执行完整抓取事务。
[GitHub README](../plugins/github/README.md) 提供页面解析与代理边界。

### `earthquake`：地震快讯

Earthquake 从中国地震台网速报微博读取近期记录。
手动命令显示最新快讯，定时任务投递 4.0 级及以上新事件。

```text
/earthquake
/earthquake latest
/earthquake help
```

每 5 分钟扫描微博卡片，先续发待办，再处理新事件并逐个确认该 schedule 的目标群投递。
`data/earthquake/` 保存微博游标、恢复检查点、待办事件和图片缓存。
结构异常状态会进入 `*.corrupt-*` 取证副本，并从有效检查点恢复。

图片来源限定为新浪图片 HTTPS 域名，微博响应、正文与图片均采用有界解析。
快讯用于消息提醒，应急判断需结合权威官方渠道。
[Earthquake README](../plugins/earthquake/README.md) 提供游标与网络细节。

### `flickr`：公共摄影发现与浏览

Flickr 插件通过官方 REST API 浏览今日精选、公共搜索、Flickr Commons、用户公开照片与公开相册。

```text
/flickr
/flickr search aurora --sort interesting
/flickr search --tags nebula,telescope --license any --date 2026-08
/flickr commons astronomy
/flickr user <用户名或个人页 URL>
/flickr album <相册 URL>
/flickr more [1-5]
/flickr info [照片ID或 URL]
```

搜索支持 `relevance`、`interesting`、`new`、`old` 四种排序，拍摄日期可使用 `YYYY-MM` 或 `YYYY-MM-DD`。
许可筛选提供 `any`、`cc` 和 `public-domain`。
默认 `any` 会覆盖全部公开许可类型。
每条回复保留作者、许可名称、许可链接和 Flickr 原图页，图片使用遵循对应照片页面的权利条件。

浏览状态按私聊用户或“群号 + 用户”隔离，保留 15 分钟。
`/flickr more` 一次继续 1–5 张。
API Key 位于 `secrets.plugins.flickr.api_key`。
图片仅从 `live.staticflickr.com` 下载，并经过 HTTPS 域名、MIME、字节、像素、尺寸和帧数校验。
缓存位于 `data/flickr/images/`，上限为 256 项、256 MiB、1 小时。
[Flickr README](../plugins/flickr/README.md) 提供完整命令、申请入口、许可和排障说明。

### `twitter`：X/Twitter 图片缓存

Twitter 插件从指定账号抓取图片到本地缓存，并随机发送当前轮次待发送图片。

```text
/twimg
/twitter
/推特
/tw_fetch
/抓取推特
```

随机发送面向全部用户，抓取入口使用 Bot 管理员权限。
每天 03:00 以 Core `silent` 模式运行后台抓取。
手动与定时调用共享同一个任务。

`secrets.plugins.twitter` 保存 `user_id`、GraphQL headers、cookies、proxy 和 `max_pages`。
首次抓取遍历时间线，后续抓取连续两页没有新增图片时停止，最多并发下载 4 张。
缓存上限为 5000 项、2 GiB、90 天。
图片位于 `data/twitter/images/`，已确认轮次位于 `data/twitter/posted.txt`。
[Twitter README](../plugins/twitter/README.md) 提供认证字段和媒体域名预算。

### `signin`：影视飓风远端签到

Signin 使用部署者提供的一组有赞/影视飓风共享凭据执行 Bot 管理员手动签到与每日签到。

```text
/signin yingshi
/签到 yingshi
/signin y
/signin help
```

每天 00:30 按调度器时区运行 `scheduled_yingshi`。
插件采用 sequential 并发模式，共享账号按调用顺序访问远端服务。

`secrets.plugins.signin.yingshijufeng` 保存 `app_id`、`kdt_id`、`access_token` 和 `sid`。
插件固定访问 `https://h5.youzan.com`，先取得签到 ID，再提交签到并展示描述、累计次数和奖励。
[Signin README](../plugins/signin/README.md) 提供凭据层级与响应边界。

### `adnmb`：A 岛公开浏览与 Feed

ADNMB 浏览 A 岛公开时间线、板块、串、回复和匿名订阅列表。
插件为每位 QQ 用户派生匿名 UUID。

```text
/adnmb -t [-p 页码]
/adnmb -f
/adnmb -m <板块名> [-p 页码]
/adnmb -c <串号> [-p 页码]
/adnmb -r <回复号>
/adnmb -d [-p 页码]
/adnmb -a <串号>
/adnmb -e <串号>
```

`config.plugins.adnmb.uuid` 可设置部署级 UUID，默认身份由插件数据目录与 QQ 用户生成。
图片缓存位于 `data/adnmb/images/`，具有 TTL、条目和字节上限。
远程图片经过 MIME、尺寸和像素校验。
[ADNMB README](../plugins/adnmb/README.md) 提供网络与身份边界。

---

## 📌 工具与娱乐

### `choice`：随机选择

Choice 提供有界随机抽样，支持重复项加权、有放回多选和唯一项多选。

```text
/选择 <问题> <选项1> <选项2> ...
/选择 <问题> <选项...> -n <1-10>
/选择 <问题> <选项...> -n <数量> -u
/选择 "今天吃什么" "ice cream" 火锅
```

别名为 `/choice`、`/决定` 和 `/抽奖`。
默认模式保留重复项权重，`-u`/`--unique` 先按文本去重，`--` 结束参数解析。
候选位置范围为 2～50，插件使用系统随机源并在内存中完成单次选择。
[Choice README](../plugins/choice/README.md) 提供全部长度边界。

### `color`：传统色与恒星色

Color 查询 526 种中国传统色、恒星光谱色和当前聊天作用域的管理员自定义色，并可生成 PNG 色卡。

```text
/color <名称|拼音|HEX|RGB|CMYK> [--picture]
/color list [页码]
/color search <关键词> [--page 页码]
/color random [--picture]
/color star <光谱型>
/color stars [筛选词] [--page 页码]
/color add <名称> <RGB或HEX>
/color delete <名称>
```

直接查询自动识别名称、无声调拼音、HEX、三通道 RGB、四通道 CMYK 和主序星光谱型。
目录与名称/拼音搜索每页显示 20 种颜色，光谱型目录每页显示 30 个。
回复提供可复制的翻页命令。
RGB、HEX 与 CMYK 缺少精确记录时，插件把输入转换到 D65 CIE L\*a\*b\*，按 CIE76 色差返回最接近的收录色。
语义子命令与 `-n/-r/-x/-c/-a/-s/-t/-w/-d` 选项均可使用。

查询支持群聊和私聊，自定义色写入与删除只给 Bot 管理员。
颜色名可包含空格，群聊按群号保存，私聊按用户保存，每个作用域上限为 200 条、256 KiB。

发行资产为 `color.json` 和 `stellar_colors.txt`，加载时校验数量、唯一性与字段。
色卡缓存位于 `data/color/images/`，上限为 256 项、32 MiB、30 天。
[Color README](../plugins/color/README.md) 提供转换格式和数据来源。

### `wolframalpha`：Wolfram|Alpha 计算

Wolfram 插件为所有人提供数学、物理、化学、单位换算和公开数据查询。

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
```

模式包括默认 `simple`、XML 步骤解答 `step` 和 JSON Result pod `complete`，`cp` 是 complete 短别名。
查询上限为 500 个字符。

`secrets.plugins.wolframalpha.appid` 保存 App ID。
请求固定使用 Wolfram|Alpha HTTPS API，30 秒总超时，全插件最多并发 2 个请求。
响应限制为 1 MiB 和 20 个结果项。
[Wolfram|Alpha README](../plugins/wolframalpha/README.md) 提供接口与响应预算。

### `guess_number`：猜数字会话

Guess Number 使用 Core Session 提供群聊与私聊短游戏。

```text
/猜数字 [简单|普通|困难|地狱]
/猜数字 status
/猜数字 restart
```

游戏开始后直接发送 ASCII 十进制数字。
`状态` 查看进度，`退出` 结束。
空闲 3 分钟后 Session 到期。

| 难度 | 范围 | 机会 |
|---|---:|---:|
| 简单 | 1～50 | 10 |
| 普通 | 1～100 | 7 |
| 困难 | 1～200 | 8 |
| 地狱 | 1～1000 | 10 |

Session 保存目标、动态上下界、次数、历史和难度。
格式错误保留当前机会。
[Guess Number README](../plugins/guess_number/README.md) 提供会话归属与输入边界。

### `qingpet`：QQ群宠物养成

QingPet 按 QQ 群隔离宠物、用户、背包、经济、社交、活动和交易数据。
群成员负责养成与互动，群管理员负责功能配置、活动和插件内用户管理。

**帮助与基础养成**

```text
/宠物 帮助 <基础|进阶|道具|社交|玩法|管理>
/宠物 领养 <名字>
/宠物 状态 [群号]
/宠物 喂食|清洁|玩耍|睡觉|起床 [群号]
/宠物 训练 [群号] [体力|敏捷|智力]
/宠物 探索 [群号] [森林|海边|山洞|废墟]
/宠物 治疗 [群号] [药品]
/宠物 rename [群号] <新名字>
/宠物 召回 [群号]
```

**道具、社交与玩法**

```text
/宠物 商店|背包|购买|使用 ...
/宠物 装扮 <商店|查看|购买|穿戴|卸下> ...
/宠物 查看|互访|摸摸|送礼|留言 @QQ号 ...
/宠物 排行 [care_score|intimacy|experience|coins]
/宠物 游戏 <猜拳|骰子|赛跑> ...
/宠物 任务 [领取]
/宠物 群任务 [领取]
/宠物 title
/宠物 活动 [领取 <活动ID>]
/宠物 展示 [投票 @QQ号]
```

**交易与管理**

```text
/宠物 交易 <列表|挂单|购买|撤单> ...
/宠物 管理 <开启|关闭>
/宠物 管理 配置 <查看|设置> ...
/宠物 管理 <重置|删除|封禁|解封> @QQ号 ...
/宠物 管理 日志|统计
/宠物 管理 活动 创建 ...
/宠物 管理 公告 <展示会|结束展示会> ...
```

交易、治疗、奖励、任务、活动和展示结算使用事务或幂等标识。
金币与友情点由 `users` 表持有，资产变化同步进入 `asset_ledger`。
状态衰减、展示会和周活动使用 Core `targeted` 模式按群发布结果。
每日重置与挂单到期使用 `silent` 模式。

SQLite 位于 `data/qingpet/qingpet/qingpet.db`。
私聊可在账号只有一只宠物时自动定位群，多个群宠物场景可在参数前加入群号。
[QingPet README](../plugins/qingpet/README.md) 和 [快速开始](../plugins/qingpet/QUICKSTART.md) 提供成长门槛、群配置和部署验收。

### `echo`：回显与问候示例

Echo 提供有界文本回显、用户问候和最小插件开发示例。

```text
/echo <文本>
/回显 <文本>
/hello
/你好
```

`/echo` 保留换行与制表符，正文上限为 Core 单条 QQ 文本预算。
其他控制字符进入参数错误。
`/hello` 读取并校验当前 QQ 号。
插件只发布 `handle()`，配置、凭据、网络、数据文件和定时任务为空集。
[Echo README](../plugins/echo/README.md) 提供最小入口结构。

---

## ⌨️ 命令入口速查

下表由各插件 `plugin.json` 整理，列出全部顶层命令、别名、权限与场景。
子命令、参数和样例以 `/help <命令>` 为准。
权限中“群管理员”包括当前群群主、群管理员和 Bot 管理员。
子命令可以在此基础上收紧权限。

| 插件 | 命令与别名 | 权限 | 场景 |
|---|---|---|---|
| `adnmb` | `/adnmb`、`/a岛`、`/岛` | 公开 | 私聊、群聊 |
| `ads_paper` | `/paper` | 公开 | 私聊、群聊 |
| `apod` | `/apod`、`/每日一天文图` | 公开 | 私聊、群聊 |
| `arxiv_filter` | `/arxiv`、`/论文` | 公开 | 私聊、群聊 |
| `astro_tools` | `/astro` | 公开 | 私聊、群聊 |
| `bot_core` | `/help`、`/h`、`/帮助`、`/commands`、`/命令目录`、`/catalog` | 公开 | 私聊、群聊 |
| `bot_core` | `/reload`、`/重载` | Bot 管理员 | 私聊、群聊 |
| `bot_core` | `/plugins`、`/插件` | 公开 | 私聊、群聊 |
| `bot_core` | `/闭嘴`、`/shutup`、`/mute`、`/安静` | Bot 管理员 | 群聊 |
| `bot_core` | `/说话`、`/speak`、`/unmute` | Bot 管理员 | 群聊 |
| `bot_core` | `/set_secret`、`/setsecret`、`/设置密钥` | Bot 管理员 | 私聊 |
| `bot_core` | `/get_secret`、`/getsecret`、`/查看密钥` | Bot 管理员 | 私聊 |
| `bot_core` | `/metrics`、`/stats`、`/性能`、`/指标` | Bot 管理员 | 私聊、群聊 |
| `chat` | `/chat`、`/gpt`、`/ai` | 公开 | 私聊、群聊 |
| `chime` | `/chime`、`/frb` | 公开 | 私聊、群聊 |
| `choice` | `/choice`、`/决定`、`/选择`、`/抽奖` | 公开 | 私聊、群聊 |
| `codex` | `/codex` | Bot 管理员 | 私聊、群聊 |
| `color` | `/color`、`/颜色`、`/色彩` | 公开 | 私聊、群聊 |
| `dict` | `/dict`、`/词典`、`/字典` | 公开 | 私聊、群聊 |
| `earthquake` | `/earthquake`、`/地震` | 公开 | 私聊、群聊 |
| `echo` | `/echo`、`/回显` | 公开 | 私聊、群聊 |
| `echo` | `/hello`、`/你好` | 公开 | 私聊、群聊 |
| `flickr` | `/flickr`、`/flicker`、`/弗里克` | 公开 | 私聊、群聊 |
| `github` | `/github`、`/gh`、`/trending` | 公开 | 私聊、群聊 |
| `guess_number` | `/猜数字`、`/guess`、`/猜` | 公开 | 私聊、群聊 |
| `jupyter` | `/jupyter`、`/py`、`/python` | Bot 管理员 | 私聊、群聊 |
| `jupyter` | `/kernel`、`/内核` | Bot 管理员 | 私聊、群聊 |
| `minecraft` | `/mc`、`/minecraft` | 公开（`connect`、`disconnect`、`run` 为 Bot 管理员） | 私聊、群聊 |
| `minecraft` | `/mcconnect`、`/mc连接` | Bot 管理员 | 私聊、群聊 |
| `minecraft` | `/mcdisconnect`、`/mc断开` | Bot 管理员 | 私聊、群聊 |
| `pendo` | `/pendo` | 公开 | 私聊 |
| `pendo` | `/日程` | 公开 | 私聊 |
| `pendo` | `/待办` | 公开 | 私聊 |
| `pendo` | `/日记` | 公开 | 私聊 |
| `qingpet` | `/qingpet`、`/宠物`、`/pet` | 公开 | 私聊、群聊 |
| `qingssh` | `/ssh`、`/SSH`、`/远程`、`/ssh连接`、`/sshconnect` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh断开`、`/sshdisconnect`、`/ssh退出` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh列表`、`/sshlist`、`/ssh服务器` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh添加`、`/sshadd` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh删除`、`/sshremove`、`/sshdel` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh导入`、`/sshimport` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/sshconfig`、`/ssh配置` | Bot 管理员 | 私聊、群聊 |
| `qingssh` | `/ssh状态`、`/sshstatus`、`/ssh连接数`、`/sshactive` | Bot 管理员 | 私聊、群聊 |
| `shell` | `/shell`、`/sh`、`/exec` | Bot 管理员 | 私聊、群聊 |
| `signin` | `/signin`、`/签到` | Bot 管理员 | 私聊、群聊 |
| `smalltalk` | `/记忆`、`/记住`、`/学习` | Bot 管理员 | 私聊、群聊 |
| `smalltalk` | `/对话` | Bot 管理员 | 私聊、群聊 |
| `smalltalk` | `/删除对话` | Bot 管理员 | 私聊、群聊 |
| `twitter` | `/twimg`、`/twitter`、`/推特` | 公开 | 私聊、群聊 |
| `twitter` | `/tw_fetch`、`/抓取推特` | Bot 管理员 | 私聊、群聊 |
| `url_parser` | 无命令，单 URL 消息自动触发 | 公开 | 私聊、群聊 |
| `voice` | `/语音`、`/念`、`/tts` | 公开 | 私聊、群聊 |
| `wolframalpha` | `/alpha`、`/wolfram`、`/wa`、`/计算` | 公开 | 私聊、群聊 |
| `xiaoai` | `/米家`、`/小爱`、`/xiaoai` | Bot 管理员 | 私聊 |
| `xiaoqing_agent` | `/agent` | 公开（工具按发起人权限） | 私聊、群聊 |
| `xiaoqing_chat` | `/xc` | 公开 | 私聊、群聊 |

---

## ⏰ 定时任务总表

时间按 `config.timezone`（默认 `Asia/Shanghai`）解释。
`broadcast` 把同一结果发到该任务的目标群（未声明 `group_ids` 时使用 `default_group_ids`），`targeted` 由插件逐个返回私聊或群目标，`silent` 只运行不发消息。
投递模式说明见 [插件开发指南的调度字段](03-plugin-development.md#调度字段)。

| 插件 | Handler | 时间 | 投递 | 作用 |
|---|---|---|---|---|
| `apod` | `scheduled` | 每天 13:30 | `broadcast` | 推送当日天文图 |
| `arxiv_filter` | `scheduled_check` | 工作日 10:00 | `broadcast` | 检查当日列表并推送推荐 |
| `arxiv_filter` | `scheduled_check` | 工作日 10:30 | `broadcast` | 检查当日列表并推送推荐 |
| `arxiv_filter` | `scheduled_check` | 工作日 11:00 | `broadcast` | 检查当日列表并推送推荐 |
| `arxiv_filter` | `scheduled_check` | 工作日 11:30 | `broadcast` | 检查当日列表并推送推荐 |
| `arxiv_filter` | `scheduled_final_check` | 工作日 12:00 | `broadcast` | 当日最终检查 |
| `chime` | `scheduled_check` | 每天 09:00、21:00 | `targeted` | 检查重复暴目录更新 |
| `earthquake` | `scheduled` | 每 5 分钟 | `targeted` | 推送 4.0 级及以上新快讯 |
| `github` | `scheduled` | 每天 08:30 | `broadcast` | 推送 daily Trending |
| `minecraft` | `scheduled` | 每 5 秒 | `targeted` | 转发服务器日志事件 |
| `pendo` | `scheduled` | 每分钟 | `targeted` | 日程与待办提醒 |
| `pendo` | `scheduled_daily_briefing` | 每分钟 | `targeted` | 用户本地时间的每日简报 |
| `pendo` | `scheduled_diary_reminder` | 每分钟 | `targeted` | 用户本地时间的日记提醒 |
| `pendo` | `scheduled_migrate_todos` | 每天 00:05 | `targeted` | 顺延昨日未完成的计划待办 |
| `pendo` | `scheduled_prune_operation_logs` | 每天 00:15 | `silent` | 清理过期操作日志与撤销快照 |
| `pendo` | `scheduled_weekly_finance_summary` | 每分钟 | `targeted` | 用户本地周日 21:00 财务周报 |
| `pendo` | `scheduled_month_end_finance_summary` | 每分钟 | `targeted` | 用户本地月末 21:00 财务月报 |
| `pendo` | `scheduled_cleanup_demo_data` | 每 6 小时的 15 分 | `silent` | 清理过期 Web Demo 数据 |
| `qingpet` | `scheduled_decay` | 每分钟 | `targeted` | 宠物状态衰减 |
| `qingpet` | `scheduled_daily_reset` | 每天 00:00 | `silent` | 每日状态重置 |
| `qingpet` | `scheduled_trade_expiry` | 每 5 分钟 | `silent` | 结算到期交易挂单 |
| `qingpet` | `scheduled_pet_show_settlement` | 每分钟 | `targeted` | 结算到期展示会 |
| `qingpet` | `scheduled_weekly_activity` | 周一 10:00 | `targeted` | 结算周活动 |
| `qingssh` | `cleanup_orphans` | 每分钟 | `silent` | 断开已没有活跃会话的 SSH 连接 |
| `signin` | `scheduled_yingshi` | 每天 00:30 | `broadcast` | 影视飓风每日签到 |
| `twitter` | `scheduled_fetch` | 每天 03:00 | `silent` | 后台抓取新图 |
| `xiaoqing_chat` | `scheduled_daily_life` | 每天 05:30 | `silent` | 按人设与作息安排角色今天的生活小事 |
| `xiaoqing_chat` | `scheduled_proactive` | 每 20 分钟 | `targeted` | 冷群时以群友身份主动开口（`proactive.enabled` 打开后生效，默认关闭） |

Pendo 的简报、日记提醒和财务总结每分钟运行一次，按每位用户自己的时区判断是否到期。

---

## 💾 插件数据与备份

插件运行数据默认位于：

```text
data/<plugin_name>/
```

备份按数据所有权执行：

| 类型 | 示例 | 备份重点 |
|---|---|---|
| SQLite | Pendo、QingPet | 主库、WAL、SHM 与一致性检查 |
| 原子 JSON | Chat、Smalltalk、CHIME、Earthquake | 主文件、备份与隔离副本 |
| 媒体缓存 | APOD、Twitter、URL Parser、Color、Voice | 容量、TTL、文件身份与重建能力 |
| 会话与制品 | Codex、QingSSH | 索引、对话、归档和任务输出 |
| 模型资产 | arXiv Filter | 配置、权重、tokenizer 与 SHA-256 |

源码同步、公开配置、secrets、业务数据和日志采用独立发布或备份步骤。

---

## ✅ 插件验证

运行时命令契约由 Manifest 与 UAT 验证：

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh --phases ws-matrix --matrix-plugins <plugin_name>
```

外部服务场景通过 `--include-external --scenario-fixtures <文件>` 加入，聊天质量场景通过 `--include-chat-quality` 加入。
UAT 报告记录命令节点、正常样例、错误输入、权限、状态变化和清理结果。

插件开发、配置与消息路径分别见：

- [插件开发指南](03-plugin-development.md)
- [配置详解](06-configuration.md)
- [消息处理流程](08-message-flow.md)

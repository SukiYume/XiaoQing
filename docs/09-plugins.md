# 📦 09 - 插件使用手册

本章完整介绍 XiaoQing 内置的 29 个插件，覆盖用途、主要命令、权限、配置、数据和运行方式。运行时 `/help` 提供当前 Manifest 的递归命令目录，各插件 README 提供专项边界与维护细节。

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

总览先展示功能域与插件，插件页展示一级入口，分支页展示直接子命令，叶节点展示完整用法、别名、权限、场景、正确样例和错误样例。所有目录支持 `page N`。

```text
/help pendo
/help pendo todo
/help pendo todo add
/help minecraft
/help shell
```

`/plugins` 显示当前进程已加载的插件。公开配置位于 `config.plugins.<plugin_name>`，插件凭据位于 `secrets.plugins.<plugin_name>`，AI route 位于 `config.plugins.<plugin_name>.ai.routes`。

---

## 📌 Core 与个人管理

### `bot_core`：Core 管理入口

Bot Core 提供分层帮助、插件列表、配置与插件重载、群静音、secret 管理和运行指标。

**主要命令**

| 命令 | 场景与权限 | 功能 |
|---|---|---|
| `/help [查询] [page N]` | 公开 | 浏览或搜索命令目录 |
| `/help json [查询] [page N]` | 公开 | 返回结构化目录 |
| `/plugins` | 公开 | 查看已加载插件 |
| `/reload [config]` | Bot 管理员 | 重载配置与插件 |
| `/闭嘴 [时长]` | Bot 管理员群聊 | 暂停当前群普通回复 |
| `/说话` | Bot 管理员群聊 | 恢复当前群普通回复 |
| `/set_secret <路径> <值>` | 全局管理员私聊 | 更新已有 secret 路径 |
| `/get_secret <路径>` | 全局管理员私聊 | 查看脱敏值或对象键名 |
| `/metrics` | Bot 管理员 | 查看运行时间、成功率、错误和慢插件 |

静音默认 10 分钟，支持分钟、小时和中文单位，范围上限为 24 小时。Secret 路径使用点分命名，写入后发布新的配置 revision；查询结果按值类型脱敏。运行指标由 Core 观测服务提供。

数据与生命周期由 Core 的配置、静音、指标和插件管理服务持有。[Bot Core README](../plugins/bot_core/README.md) 提供完整别名、secret 值规则和重载流程。

### `pendo`：个人时间与信息管理

Pendo 在私聊中统一管理日程、待办、笔记、日记、账本、提醒、搜索、设置、Web 控制台、Scriptable 小组件和数据迁移。所有业务数据按 QQ 用户隔离。

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

空内容的 `todo add` 启动轻量会话，收集内容与计划日期；一行输入可同时携带提醒、分类、优先级和标签。

**笔记、日记与账本**

```text
/pendo note add|list|view|edit|append|tag|untag|link|delete ...
/pendo diary add|template|list|view|delete ...
/pendo ledger add|quick|list|view|edit|delete|summary ...
```

笔记支持分类、标签、引用和关联条目。日记支持同日多篇、模板回答、天气、地点、心情、评分和收藏。账本支持收入、支出、转账、账户、商户、分类、日期和金额区间，统计使用整数分字段。

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

操作快照保留 5 分钟。`ai_consent` 管理日记正文的外部 AI 分析授权。

**Web 与数据**

```text
/pendo web start|status|stop
/pendo web token
/pendo web widget-token
/pendo web widget-revoke
/pendo export ...
/pendo import
```

一次性登录码有效期为 7 天，兑换后的 HttpOnly 浏览器会话有效期为 7 天。Widget Bearer Token 有效期为 365 天，权限范围限定为 `/api/widget/*`。登录凭据摘要、浏览器会话、Widget 登记和全部业务表位于 `data/pendo/pendo.db`。

提醒、每日简报与日记提示每分钟检查各用户设置；待办顺延每天 00:05 执行，操作日志每天 00:15 清理，财务周报每周日 21:00 生成，财务月报在每月最后一天 21:00 生成，Demo 数据每 6 小时的第 15 分钟清理。`scheduled_delivery_outbox` 按目标记录投递确认。[Pendo README](../plugins/pendo/README.md)、[Pendo 架构](../plugins/pendo/ARCHITECTURE.md) 和 [Scriptable 指南](pendo-scriptable-widget.md) 提供完整字段与页面说明。

---

## 🎨 智能聊天与语音

### `xiaoqing_chat`：拟人聊天运行时

XiaoQing Chat 结合召唤判定、普通群聊参与、PFC 规划、长期记忆、人物资料、多模态、表达学习和独立回复检查。它可作为全局闲聊 provider，也提供 `/xc` 显式入口。

**启用与命令**

```json
{
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

群会话重置和模型切换使用群管理员、群主或 Bot 管理员权限；全局模型切换使用 Bot 管理员权限。运行时模型覆盖在进程重启后按配置恢复。

**参与与回复**

私聊、显式命令、群聊 `@`、Bot 名称、名称续问、reply-to-bot 和带近期锚点的共指进入明确召唤。普通群聊依次应用回复间隔、每分钟上限、连续冷却、参与概率、开放话题信号、活跃目标、Heartflow 和 PFC 行动。

主模型读取连续会话片段、相关记忆、人物资料、Goal、PFC、表达和媒体上下文。回复通过确定性规则和 `checker` route；OneBot 投递确认后提交助手记忆、规划状态和后台学习任务。

**AI、媒体与数据**

插件使用 `chat`、`reasoning`、`checker` 和 `vision` 四条 AI route。入站支持 `text`、`at`、`reply`、`face`、`mface`、`image` 和混合 segment。出站支持 `[想发表情:hint]`、`[想发QQ表情:hint]` 与 `[想发图片:hint]`。

行为配置位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`，运行数据位于 `data/xiaoqing_chat/`。[用户手册](../plugins/xiaoqing_chat/README.md)、[架构文档](../plugins/xiaoqing_chat/ARCHITECTURE.md) 和 [上线验收指南](../plugins/xiaoqing_chat/xiaoqing_chat%E6%B5%8B%E8%AF%95.md) 提供完整流程。

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

三组 Manifest 命令均为 Bot 管理员入口。问题使用首个 token，回答可包含空格；单问题最多保存 20 个回答，单作用域最多保存 2000 个问题。群聊 QA 按群号共享，私聊 QA 按用户隔离。

精确 QA 命中时返回本地回答，其他闲聊通过 Core capability 调用 `chat.reply`。`plugins.smalltalk.voice_probability` 控制纯文本回复调用 `voice.synthesize_text` 的概率；发行配置示例设为 `0`，字段缺失时运行时默认值为 `0.2`。

数据位于 `data/smalltalk/QA_group_<群号>.json`、`QA_private_<用户号>.json` 和 `QA_audit.json`，更新采用锁与原子写。[Smalltalk README](../plugins/smalltalk/README.md) 提供长度、审计和服务授权说明。

### `chat`：Coze 单轮对话

Chat 通过 Coze API v3 提供 Bot 管理员单轮对话，并向 Smalltalk 发布 `chat.reply` 服务。

```text
/chat <问题>
/gpt <问题>
/ai <问题>
/chat help
```

问题长度为 1～2000 个字符。`secrets.plugins.chat` 保存 `token`、`bot_id` 和可选 `proxy`；`config.plugins.chat` 保存 `daily_user_limit` 与 `daily_global_limit`。

一次远端调用包含创建对话、轮询与读取答案，共享 30 秒总预算，全插件最多并发 2 个请求。额度在调用前预留，成功答案提交额度，异常路径释放额度。状态位于 `data/chat/chat_quota.json`。[Chat README](../plugins/chat/README.md) 提供 Coze 配置与隐私边界。

### `voice`：Azure Speech 语音合成

Voice 提供 Bot 管理员 TTS 命令，并发布 `voice.synthesize_text` 服务供 Smalltalk 调用。

```text
/语音 <文本>
/念 <文本>
/tts <文本>
/语音 help
```

文本范围为 1～500 个字符，结果为 OneBot `record` segment。`secrets.plugins.voice` 保存 `subscription_key`、`region`、`voice_name`、`style`、`role` 和可选代理。

插件生成 XML 转义后的 SSML，请求 Azure MP3，校验 MIME、字节和文件头，再写入 `data/voice/audio/`。缓存上限为 2048 项、256 MiB、7 天；相同输入与音色设置共享生成锁。[Voice README](../plugins/voice/README.md) 提供音色默认值与资源预算。

---

## 🔭 天文与科研

### `apod`：NASA 每日天文图

APOD 抓取 NASA 当前 Astronomy Picture of the Day 页面，返回图片或视频链接、标题和说明。

```text
/apod
/每日一天文图
/apod help
```

命令使用空参数并固定读取 `https://apod.nasa.gov/apod/astropix.html`。可通过 `config.plugins.apod.url` 与 `allowed_hosts` 设置页面和媒体主机。每天 13:30 向 `default_group_ids` 投递。

页面、重定向和媒体均经过 HTTPS、公网 DNS、响应大小、MIME、尺寸和像素校验；缓存位于 `data/apod/`。[APOD README](../plugins/apod/README.md) 提供完整网络边界。

### `arxiv_filter`：arXiv 兴趣筛选与摘要

arXiv Filter 获取 `astro-ph/new` 源列表，按本地模型筛选论文，发送推荐列表，并把 positive 论文交给 Codex `astro-ph` 会话生成中文摘要。

```text
/arxiv
/论文
/arxiv help
```

回复显示源站列表日期，推理缓存按源日期隔离。Codex 任务用“源列表日期 + 规范化论文链接集合”识别，同一任务可复用已完成摘要或运行状态。

工作日 10:00、10:30、11:00、11:30 检查当日列表，12:00 执行最终检查。投递状态位于 `data/arxiv_filter/update_status.json`。

`plugins/arxiv_filter/config.json` 配置模型路径、阈值、批大小、最大长度、arXiv URL、代理、TLS 和超时。模型路径优先级为 CLI `--model-path`、`ARXIV_MODEL_PATH`、配置 `model.path`。后端支持 Transformers、KNN 与 multi-interest；生产模型资产需包含匹配的配置、权重与 tokenizer。[arXiv Filter README](../plugins/arxiv_filter/README.md) 提供推理 CLI 与资产清单。

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

搜索、引用与相关论文支持群聊和私聊；笔记、灵感、主题、截止日期、每日推荐和个人文献库使用私聊用户作用域。

`secrets.plugins.ads_paper.ads_token` 提供 ADS Token。AI `summary` route 由项目级 provider 和 model profile 支持。JSON 数据位于 `data/ads_paper/`，个人 BibTeX 使用 `references_<user_id>.bib`；状态写入采用原子文件和损坏副本隔离。[ADS Paper README](../plugins/ads_paper/README.md) 提供全部数据文件与查询规则。

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

时间支持 ISO、JD、MJD 与 Unix。坐标支持 ICRS、Galactic 和 geocentric true ecliptic。红移范围为 `0 ≤ z ≤ 1100`，使用 Planck18 计算共动距离、光度距离、角直径距离、回溯时间和宇宙年龄。

公式速查覆盖色散量、红移定义、光度距离、流量密度、黑体辐射、视差距离、史瓦西半径、主序星质光关系和主序星寿命。`formula calc` 接受正太阳质量，计算史瓦西半径、主序星光度或主序星寿命。内置对象包括太阳、月球以及水星至海王星的八大行星；其他名称通过 SIMBAD 查询。常数目录覆盖光速、引力常数、普朗克常数、玻尔兹曼常数、斯特藩－玻尔兹曼常数、电子与质子质量、太阳质量/半径/光度、天文单位、秒差距、光年和近似哈勃常数。

插件复用 Core 时区与 HTTP Session，结果即时生成。[Astro Tools README](../plugins/astro_tools/README.md) 提供输入示例和依赖说明。

### `dict`：离线天文学中英词典

Dict 使用发行版内的 `r241020` 数据完成中英双向精确或模糊查询。

```text
/dict galaxy
/dict 星系
/dict -e "fast radio burst"
/dict -n 20 star
/dict -- -example
```

`-e`/`--exact` 开启完整源词匹配，`-n`/`--num` 设置 1～100 条结果，`--` 结束参数解析。含中日韩统一表意文字的输入走中译英数据，其余输入走英译中数据。

发行资产为 `astrodict_ec.txt`、`astrodict_ce.txt` 和 `assets/manifest.json`。首次查询会校验字节数、行数、SHA-256、UTF-8、列结构和重复记录，随后按文件身份缓存解析结果。[Dict README](../plugins/dict/README.md) 提供数据来源与使用约定。

### `chime`：CHIME/FRB 重复暴目录

CHIME 插件查询重复暴目录，并相对本地通知基线发现新增重复暴和新脉冲。

```text
/chime
/chime list
/chime FRB20180916B
/chime help
```

默认查询显示上次成功通知以来的更新，`list` 展示最近 5 个 FRB，规范 FRB 名称查询时间、DM、RA、DEC 和 SNR。每天 09:00 与 21:00 检查目录，并向默认群逐个确认投递。

`data/chime/chime_history.json` 保存通知基线，`data/chime/chime_delivery.json` 保存待办与逐目标状态。手动查询使用 5 分钟缓存，定时检查读取新目录。[CHIME README](../plugins/chime/README.md) 提供目录来源和至少一次投递语义。

---

## 🧩 执行、运维与服务器

本组插件会调用本机进程、远端主机或游戏服务器。管理员私聊、最小系统权限、明确目标目录和受控网络共同构成部署边界。

### `codex`：Codex CLI 后台任务

Codex 插件通过 Bot 管理员私聊管理本机 Codex CLI。每个标签拥有独立工作目录、Codex thread、队列和对话记录；同一标签串行执行，多个标签可并行。

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

数据位于 `data/codex/`，包括 `sessions.json`、每个会话的 `conversation.jsonl`、任务 artifacts、图片、输出和已删除会话归档。路径、输出、图片、队列、进程与磁盘均有独立预算；卸载与关闭会取消进程树并保存状态。[Codex README](../plugins/codex/README.md) 提供完整预算和 arXiv 服务契约。

### `shell`：本机单命令执行

Shell 让 Bot 管理员通过私聊在 Bot 主机执行单条命令。每次调用创建独立进程，工作目录与环境状态按调用建立。

```text
/shell <命令>
/shell list
/shell help
```

入口别名为 `/sh` 与 `/exec`。`config.plugins.shell.terminal` 选择 `direct` 或 `git-bash`；Git Bash 配置明确的 `executable`，并以 `--noprofile --norc -c` 启动。

`secrets.plugins.shell` 配置首入口启用列表、`replace`/`extend` 模式、超时与全入口开关。模式检查会处理命令链接、管道、替换、多行、重定向与高风险特征。stdout/stderr 共享捕获预算，QQ 结果使用首尾投影，超时和取消会回收进程树。[Shell README](../plugins/shell/README.md) 提供路径规则与配置示例。

### `qingssh`：SSH 远程会话

QingSSH 为 Bot 管理员提供私聊 SSH profile、`~/.ssh/config` 导入、持久会话、流式输出、远端终止和图片查看。

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

连接会话中直接发送 POSIX Shell 命令；`cd` 更新工作目录，`停止` 终止当前远端进程组，`showimg` 下载远端图片，`退出` 关闭连接。会话按私聊用户与服务器隔离，空闲期为 10 分钟。

Profile 位于 `data/qingssh/servers.json`，密码通过 Core secret 引用保存。Host Key 使用 `known_hosts` 严格验证，支持单跳 `ProxyJump` 和结构化 `ssh -W`。长输出归档到 `data/qingssh/command_outputs/`。[QingSSH README](../plugins/qingssh/README.md) 提供认证、输出与生命周期预算。

### `jupyter`：持久 Python 内核

Jupyter 为 Bot 管理员私聊提供 Python 执行、持久内核、REPL 缓冲和内核管理。每个私聊用户拥有独立内核所有者键。

```text
/py print("hello")
/py -t 60 print("bounded")
/py repl
/kernel status|start|restart|shutdown
```

执行时限范围为 0.1～600 秒，默认 30 秒。REPL 会话空闲期为 10 分钟，支持 `run`、`show`、`clear`、`help` 和 `退出`。单次代码、缓冲行数、文本输出、图片数量、图片大小、像素和全局内核数量均有预算。

内核按需创建并通过 ready 检查，空闲监视器负责回收；关闭阶段先停止监视器，再收敛活动内核。[Jupyter README](../plugins/jupyter/README.md) 提供安装 extra 与完整预算。

### `minecraft`：Minecraft RCON 与日志转发

Minecraft 插件让 Bot 管理员在私聊中连接 Java Edition RCON，执行服务器命令，并接收服务器日志事件。

```text
/mc connect <配置名>
/mc status
/mc list
/mc say <消息>
/mc tell <玩家名> <消息>
/mc <服务器命令>
/mc disconnect
```

`plugins/minecraft/config.json` 保存 host、port 和 `log_file`，`secrets.plugins.minecraft.<profile>` 保存同名 RCON 密码。每名管理员拥有独立连接；命令按连接锁顺序执行。

配置日志文件后，调度任务每 5 秒识别玩家聊天、加入、离开、死亡和进度，并转发到发起连接的私聊。游标位于 `data/minecraft/log_cursors/`，投递确认后提交。RCON 采用明文协议，生产连接应使用回环监听、SSH 隧道或受保护网络。[Minecraft README](../plugins/minecraft/README.md) 提供协议与转发预算。

---

## 🧩 内容与外部服务

### `url_parser`：单 URL 网页预览

URL Parser 由 Dispatcher 调用，Manifest 命令列表为空。清理后的消息整体为一个 HTTP 或 HTTPS URL 时，插件提取标题、描述和预览图。

```text
https://example.com/article
```

标题来自 `<title>`，描述来自标准 description、Open Graph 或 Twitter Card，图片来自 `og:image` 或 `twitter:image`。页面和图片使用独立无凭据公网客户端，每次请求与重定向都重新校验 URL、DNS 和目标地址。

HTML 上限为 2 MiB，图片上限为 5 MiB、2000 万像素和 120 帧，并发网页预览上限为 4。图片缓存位于 `data/url_parser/url_previews/`，上限为 128 项、128 MiB、7 天。[URL Parser README](../plugins/url_parser/README.md) 提供元数据优先级与网站兼容性。

### `github`：GitHub Trending

GitHub 插件解析官方 Trending 页面，提供日、周、月热门仓库。

```text
/github
/github daily
/github weekly
/github monthly
/github help
```

别名为 `/gh` 与 `/trending`。单次最多解析 50 个文章节点，QQ 最多展示 10 个完整仓库块。每天 08:30 向默认群推送 daily 结果。

当前指针写入 `data/github/trending_<range>_latest.json`，日期快照写入 `data/github/history/`，每个范围保留 90 份。可在 `secrets.plugins.github.proxy` 配置 HTTP/HTTPS 代理；每次命令和定时任务执行完整抓取事务。[GitHub README](../plugins/github/README.md) 提供页面解析与代理边界。

### `earthquake`：地震快讯

Earthquake 从中国地震台网速报微博读取近期记录。手动命令显示最新快讯，定时任务投递 4.0 级及以上新事件。

```text
/earthquake
/earthquake latest
/earthquake help
```

每 5 分钟扫描微博卡片，先续发待办，再处理新事件并逐个确认默认群投递。`data/earthquake/` 保存微博游标、恢复检查点、待办事件和图片缓存。结构异常状态会进入 `*.corrupt-*` 取证副本，并从有效检查点恢复。

图片来源限定为新浪图片 HTTPS 域名，微博响应、正文与图片均采用有界解析。快讯用于消息提醒，应急判断需结合权威官方渠道。[Earthquake README](../plugins/earthquake/README.md) 提供游标与网络细节。

### `twitter`：X/Twitter 图片缓存

Twitter 插件从指定账号抓取图片到本地缓存，并随机发送当前轮次待发送图片。

```text
/twimg
/twitter
/推特
/tw_fetch
/抓取推特
```

随机发送面向全部用户，抓取入口使用 Bot 管理员权限。每天 03:00 运行后台抓取；手动与定时调用共享同一个任务。

`secrets.plugins.twitter` 保存 `user_id`、GraphQL headers、cookies、proxy 和 `max_pages`。单轮最多新增 100 张，最多并发下载 4 张；缓存上限为 5000 项、512 MiB、90 天。图片位于 `data/twitter/images/`，已确认轮次位于 `data/twitter/posted.txt`。[Twitter README](../plugins/twitter/README.md) 提供认证字段和媒体域名预算。

### `signin`：影视飓风远端签到

Signin 使用部署者提供的一组有赞/影视飓风共享凭据执行 Bot 管理员手动签到与每日签到。

```text
/signin yingshi
/签到 yingshi
/signin y
/signin help
```

每天 00:30 按调度器时区运行 `scheduled_yingshi`。插件采用 sequential 并发模式，共享账号按调用顺序访问远端服务。

`secrets.plugins.signin.yingshijufeng` 保存 `app_id`、`kdt_id`、`access_token` 和 `sid`。插件固定访问 `https://h5.youzan.com`，先取得签到 ID，再提交签到并展示描述、累计次数和奖励。[Signin README](../plugins/signin/README.md) 提供凭据层级与响应边界。

### `adnmb`：A 岛公开浏览与 Feed

ADNMB 浏览 A 岛公开时间线、板块、串、回复和匿名订阅列表。插件为每位 QQ 用户派生匿名 UUID。

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

`config.plugins.adnmb.uuid` 可设置部署级 UUID，默认身份由插件数据目录与 QQ 用户生成。图片缓存位于 `data/adnmb/images/`，具有 TTL、条目和字节上限；远程图片经过 MIME、尺寸和像素校验。[ADNMB README](../plugins/adnmb/README.md) 提供网络与身份边界。

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

别名为 `/choice`、`/决定` 和 `/抽奖`。默认模式保留重复项权重，`-u`/`--unique` 先按文本去重，`--` 结束参数解析。候选位置范围为 2～50，插件使用系统随机源并在内存中完成本轮选择。[Choice README](../plugins/choice/README.md) 提供全部长度边界。

### `color`：传统色与恒星色

Color 查询 526 种中国传统色、恒星光谱色和当前聊天作用域的管理员自定义色，并可生成 PNG 色卡。

```text
/color -n <名称> [-p]
/color -r <R,G,B> [-p]
/color -x <HEX> [-p]
/color -c <C,M,Y,K> [-p]
/color -a <关键词>
/color -s <光谱型>
/color -t [前缀]
/color -w <名称> <RGB或HEX>
/color -d <名称>
```

查询支持群聊和私聊，自定义色写入与删除使用 Bot 全局管理员权限。群聊按群号保存，私聊按用户保存，每个作用域上限为 200 条、256 KiB。

发行资产为 `color.json` 和 `stellar_colors.txt`，加载时校验数量、唯一性与字段。色卡缓存位于 `data/color/images/`，上限为 256 项、32 MiB、30 天。[Color README](../plugins/color/README.md) 提供转换格式和数据来源。

### `wolframalpha`：Wolfram|Alpha 计算

Wolfram 插件为 Bot 管理员提供数学、物理、化学、单位换算和公开数据查询。

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
```

模式包括默认 `simple`、XML 步骤解答 `step` 和 JSON Result pod `complete`，`cp` 是 complete 短别名。查询上限为 500 个字符。

`secrets.plugins.wolframalpha.appid` 保存 App ID。请求固定使用 Wolfram|Alpha HTTPS API，30 秒总超时，全插件最多并发 2 个请求；响应限制为 1 MiB 和 20 个结果项。[Wolfram|Alpha README](../plugins/wolframalpha/README.md) 提供接口与响应预算。

### `guess_number`：猜数字会话

Guess Number 使用 Core Session 提供群聊与私聊短游戏。

```text
/猜数字 [简单|普通|困难|地狱]
/猜数字 status
/猜数字 restart
```

游戏开始后直接发送 ASCII 十进制数字；`状态` 查看进度，`退出` 结束。空闲 3 分钟后 Session 到期。

| 难度 | 范围 | 机会 |
|---|---:|---:|
| 简单 | 1～50 | 10 |
| 普通 | 1～100 | 7 |
| 困难 | 1～200 | 8 |
| 地狱 | 1～1000 | 10 |

Session 保存目标、动态上下界、次数、历史和难度；格式错误保留当前机会。[Guess Number README](../plugins/guess_number/README.md) 提供会话归属与输入边界。

### `qingpet`：QQ群宠物养成

QingPet 按 QQ 群隔离宠物、用户、背包、经济、社交、活动和交易数据。群成员负责养成与互动，群管理员负责功能配置、活动和插件内用户管理。

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

交易、治疗、奖励、任务、活动和展示结算使用事务或幂等标识。金币与友情点由 `users` 表持有，资产变化同步进入 `asset_ledger`。状态每分钟衰减；每日、每周、挂单到期和展示会结算由调度任务处理。

SQLite 位于 `data/qingpet/qingpet/qingpet.db`。私聊可在账号只有一只宠物时自动定位群，多个群宠物场景可在参数前加入群号。[QingPet README](../plugins/qingpet/README.md) 和 [快速开始](../plugins/qingpet/QUICKSTART.md) 提供成长门槛、群配置和部署验收。

### `echo`：回显与问候示例

Echo 提供有界文本回显、用户问候和最小插件开发示例。

```text
/echo <文本>
/回显 <文本>
/hello
/你好
```

`/echo` 保留换行与制表符，正文上限为 Core 单条 QQ 文本预算；其他控制字符进入参数错误。`/hello` 读取并校验当前 QQ 号。插件只发布 `handle()`，配置、凭据、网络、数据文件和定时任务为空集。[Echo README](../plugins/echo/README.md) 提供最小入口结构。

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

外部服务场景通过 `--include-external --scenario-fixtures <文件>` 加入，聊天质量场景通过 `--include-chat-quality` 加入。UAT 报告记录命令节点、正常样例、错误输入、权限、状态变化和清理结果。

插件开发、配置与消息路径分别见：

- [插件开发指南](03-plugin-development.md)
- [配置详解](06-configuration.md)
- [消息处理流程](08-message-flow.md)

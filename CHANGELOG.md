# XiaoQingBot 更新记录

本文件按时间倒序记录 XiaoQingBot 的版本变化、影响模块和验证结果。日期采用北京时间与 `YYYY-MM-DD` 格式。下一版本内容位于顶部，正式版本内容归入对应版本日期。

## 下一版本

### Pendo UUID 标识统一

- 日程、待办、笔记、日记、账目、日程集合及其 leaf 的新建路径统一生成无连字符的 32 位 UUID；重复 occurrence 和多节点 leaf 改用独立 UUID，日期与节点序号保留在关系字段中。
- 聊天端对新 UUID 统一展示 8 位短标识，Web API 同时返回完整 `id` 与 `display_id`；提醒确认、编辑、删除、引用和集合操作按当前用户解析短 ID。
- 短 ID 多候选时拒绝操作并要求完整 ID；生产存量条目、集合及提醒、审计、引用、FTS 关系已一次性改写为完整 UUID，不建立旧 ID alias，迁移前消息中的旧 ID 自然失效。

### 可配置机器人名称

- `bot_name` 统一注入聊天人格、深度对话、规划器、回复检查、表达学习、历史角色标签和默认上线通知。
- `xiaoqing_chat` 的人物配置保存角色特征与行为边界，普通人格和深度人格统一使用 Core 提供的名称。
- 历史助手消息按角色类型参与重复检查和学习上下文，运行时统一使用当前配置名称展示。

### Core 定时任务投递

- Core 为每个 schedule 独立解析 `group_ids`：字段为列表时使用该列表，字段省略或为 `null` 时使用全局 `default_group_ids`，显式空列表表示该任务没有群投递目标。
- Schedule Manifest 提供 `broadcast`、`targeted` 和 `silent` 三种投递模式，由 Core 统一解释、校验和执行；现有 26 个任务均显式声明模式。
- `scheduled_system` 插件上下文通过 `context.default_groups()` 暴露同一解析结果；Core 拒绝静默任务主动发送，并将目标化群消息限制在该 schedule 的目标群内。
- 新增 `ScheduledDelivery` 目标结果，QingPet 的状态提醒、展示会和周活动结算通过 Core 按群投递；每日重置与到期交易结算保持静默。
- CHIME 清单继承全局默认群，使其逐目标可靠投递与同一 Core 解析结果保持一致。
- 调度、上下文、目标化结果和 Earthquake 可靠投递回归覆盖 Manifest 目标、全局回退、显式空列表和静默发送拒绝。
- 验证：`pytest -q -n 2` 全量通过（6103 passed、2 skipped），Ruff formatter、Ruff lint、367 个生产模块的 Mypy 检查和 `git diff --check` 均通过。

### Secrets 待确认热更新

- 完整有效的 `secrets.json` 单文件外部变更进入待确认候选，当前已确认凭据、管理员视图和 OneBot 控制通道持续工作；候选在 `/reload` 前保持与插件和认证视图隔离。
- Core 使用当前可信代私聊全部管理员，按字段路径报告新增、删除和修改，通知内容仅包含字段路径；相同候选只通知一次。
- `/reload` 重新读取稳定磁盘来源并原子确认候选；缺失、损坏、不可读来源以及公开配置同时变化继续执行 fail-closed 策略。
- 配置事务、watcher、接口和 OneBot 应用边界回归覆盖候选隔离、通知脱敏、重复抑制、管理员权限与连接不轮换。

### Flickr 公共摄影浏览

- 新增 Flickr 公共只读插件，提供今日精选、关键词与标签搜索、Flickr Commons、用户公开照片、公开相册、连续浏览和照片详情。
- 搜索默认覆盖全部公开许可类型，并提供 CC 与公共领域筛选；每条回复展示作者、许可、拍摄日期、标签和 Flickr 原图页。
- API 请求固定使用 Flickr 官方 HTTPS REST 端点，图片限定为 Flickr 静态域名，并落实 JSON、MIME、字节、像素、尺寸、帧数、会话和缓存预算。
- 浏览会话按私聊用户或“群号 + 用户”隔离并保留 15 分钟；图片缓存位于 `data/flickr/images/`，API Key 由 `secrets.plugins.flickr.api_key` 管理。
- 已有 Flickr secret 路径可通过管理员私聊事务更新；新增路径可通过 secrets 单文件待确认候选和 `/reload` 在线应用。

### 发布隐私与部署配置

- 生产同步目标改由 Git 忽略的 `scripts/sync_to_remote.local.sh` 保存，发布仓库只保留可复制的通用配置示例。
- Twitter 抓取目标改为必填配置，示例、测试和默认值仅使用虚构标识；缺少目标时返回明确的配置提示。
- 清理发布历史中的运行数据、真实运行标识和部署环境信息，并轮换受影响的服务凭据。
- GitHub 仓库启用 Secret Scanning 与 Push Protection，发布前检查覆盖当前树、完整 Git 历史和在线文档。

### 在线文档与文档契约

- 根 README 的状态栏和文档导航加入在线项目手册入口 `https://paris.escape.ac.cn/note/XiaoQing/`，GitHub About Website 使用同一地址。
- 逐份复核 30 个插件 README、`docs/` 下 12 份手册和 Pendo 架构文档，统一使用直接描述现行功能、配置、边界和扩展流程的独立文档结构。
- Pendo 面向用户的数据功能统一表述为导入导出与 Bundle 传输；数据库 schema migration 保留为现行存储机制，已移除的一次性迁移脚本不再出现在架构说明中。
- 文档契约、Manifest 一致性、相对链接、标题层级、代码围栏和历史叙事扫描通过验证。

### 发布验证

- Windows 本地发布前全量测试通过：`6103 passed, 2 skipped`。
- Ruff formatter、Ruff lint、Mypy 367 个生产模块、compileall 和 `git diff --check` 全部通过。

### 代码与文档质量

- 精简配置候选状态、Flickr 客户端创建与站点校验、词典翻页命令构造中的重复实现，保持各插件公开命令与响应契约稳定。
- 复核根 README、项目手册和插件文档的现行机制表述，并统一近期代码注释的职责边界与叙述方式。

### Pendo Web 日程提醒开关

- Events 详情为每个未到期提醒提供“提前确认”按钮；确认后该提醒不会进入发送队列，按钮原位切换为“重新开启”。
- 单条提醒切换在数据库即时事务内校验所有者、日程类型、提醒归属和 UTC 触发时刻；到期按钮自动禁用，服务端同时拒绝过期请求。

### APOD 透明代理 DNS 兼容

- APOD 的 NASA 固定 HTTPS 页面与媒体支持 Clash 透明代理使用的保留 fake-IP DNS 结果，请求固定解析地址并验证 TLS 主机名、主机白名单、重定向、MIME 和响应预算。
- 兼容入口限定为 `apod.nasa.gov`；整个重定向链要求 HTTPS，其他非公网 DNS 结果由全局安全 HTTP 层拒绝，安全策略拒绝会返回 DNS、代理和 `allowed_hosts` 排障提示。

### Pendo 私聊提醒与 iOS 日历增量同步

- Pendo 日程与待办提醒固定投递到条目所有者 QQ 私聊；群上下文和默认群配置不参与提醒目标选择，回归测试覆盖携带群事件上下文的调度调用。
- Pendo 提醒键统一使用规范 `fire_at_utc`，`reminder_logs.remind_time` 和 `items.remind_times` 保持秒级 UTC，运行时提醒仓储执行严格 UTC 校验。
- 清理事件图、完整重构、提醒键归一和存量时区归一的一次性迁移实现及专用测试；数据库启动不再扫描或改写提醒业务数据。
- Scriptable 新增完整日历窗口接口，日历同步使用独立于主屏摘要的完整日程集合。首次运行查询过去 30 天至未来 30 天，后续从上次成功运行日续传到新的未来 30 天。
- 同步每次执行一次服务端窗口查询和一次 iOS 目标日历查询，按 Pendo 条目 ID 仅新增缺失事件；最长查询窗口为 3660 天，接口、目标日历或写入失败时保留游标供下次重试。
- Scriptable 脚本顶部恢复 `BASE_URL` 与 `TOKEN` 两项直接配置，401 提示引导用户生成并替换 Widget Token。

### arXiv 定时投递可靠性

- arXiv 日报在传输回执到达后通过独立工作线程提交业务日期状态并清理发送 claim，插件热重载后的调度任务保持单日一次投递。
- 明确成功回执提交当日状态，明确失败回执释放 claim 供后续检查重试，结果未知时采用 at-most-once 语义保护已经提交的消息。
- 回归测试覆盖插件执行作用域结束后收到 OneBot 成功回执的生产时序，并验证状态落盘、claim 清理和后续检查去重。

### Pendo AI 日程时间解析

- Pendo 将 AI 返回的日程起止时间和多节点时间统一解释为用户本地墙钟时间，再由持久化层依据用户或日程 IANA 时区转换为 UTC；模型附带的 `Z`、`UTC` 和数值偏移按本地墙钟格式归一。
- AI 解析来源由实际调用路径固定，日程编辑后按规范化开始时间重算原有提醒；回归测试覆盖生产问题中的 `18:00+00:00`、多节点偏移、UTC 存储、北京时间展示和提醒偏移。

### Color 与 Dict 查询体验

- Color 支持名称、拼音、HEX、RGB、CMYK 与主序星光谱型直接查询，新增分页颜色目录、名称/拼音搜索、随机颜色、光谱型目录和语义化管理子命令；原有短选项继续可用。
- RGB、HEX 与 CMYK 查询在缺少精确记录时使用 D65 CIE L\*a\*b\* 和 CIE76 色差返回最接近的传统色；自定义颜色名称支持空格，目录与搜索回复提供可复制的无状态翻页命令。Color 插件版本升至 `0.5.0`。
- Dict 按完整匹配、前缀、词边界和包含关系稳定排序，新增 `--page`、`--size` 与无状态翻页；精确查询缺少完整结果时返回模糊建议和普通查询入口。Dict 插件版本升至 `0.5.0`。
- Core 新增单 token 安全引用工具，Color、Dict、命令矩阵和场景契约共同验证带空格、连字符、分页、权限及错误输入路径。

### Twitter 媒体代理与失败反馈

- Twitter 时间线与媒体下载统一使用插件配置的 HTTP(S) 代理；媒体请求继续限制为三个 Twitter HTTPS 媒体源，并保留跳转、MIME、字节、像素和帧数边界，GraphQL 认证头与 Cookie 不会进入媒体请求。
- `/tw_fetch` 区分“没有新增图片”与“所有媒体下载失败”；全失败时返回代理排障提示，抓取完成后缓存仍为空时返回明确警告。
- 首次全量抓取取消固定 100 张上限，并忽略增量提前停止条件，持续遍历到时间线结束或 `max_pages`；成功后写入按 `user_id` 绑定的小型完成标记。
- 后续抓取继续采用连续两页无新增即停止的增量策略；首次任务中断、状态损坏、媒体下载失败或账号变化时自动重新全量回填。
- Twitter 图片缓存容量提高到 2 GiB，可容纳当前账号完整历史图片与后续增量；文件数量与保存期限继续限制为 5000 项和 90 天。
- `/twimg` 在本地缓存为空时引导管理员执行 `/tw_fetch` 并等待完成通知。Twitter 插件版本升至 `2.0.3`。

### Pendo Web 手机端布局

- 账本明细在窄屏保持紧凑横排，分类标签轻微下移到账户信息高度；摘要与账户文本在独立区域内安全截断。
- 日记月历与面板建立明确的宽度收缩边界；手机端保持完整七列，并以紧凑色条标记有内容的日期。
- 全部详情弹窗的底部操作区在手机端采用两列响应式布局，四个操作按 `2 × 2` 排列并包含底部安全区。
- 弹窗打开期间，“回到顶部”按钮自动隐藏并退出交互层，详情关闭、管理、编辑和删除操作保持完整可点。

### Bot Core 重载完成通知

- `/reload` 保持非阻塞两阶段流程：配置发布并启动后台任务后立即确认，全部插件完成或中止后再向原管理员会话发送一次最终结果与耗时。
- Core 重载任务返回明确的成功状态；异常、quarantine 中止和普通失败统一进入失败通知，通知投递异常只记录日志，不改变插件重载结果。
- 同一后台任务被重复 `/reload` 复用时只登记一条完成通知，后台消息显式绕过原事件收集器。Bot Core 插件版本升至 `0.5.0`。

### QingSSH 图片帮助与批量发送

- 全局插件帮助、`/ssh help`、`/ssh` 快速说明和连接会话帮助统一展示 `showimg <路径或通配符> [--page N]`，并说明该入口在 SSH 连接会话内使用。
- `showimg` 支持 `./`、相对/绝对目录，以及最后一级文件名中的 `*`、`?`、`[]` 通配符；匹配结果按文件名字典序分页，每页 5 张，回复提供前后页命令并覆盖全部匹配图片。
- 图片消息携带全局顺序、匹配总数和远端文件名，每张保持 10 MiB 上限；无参数、无效/越界页码、目录通配符、部分下载失败和发送异常进入明确响应或清理路径。QingSSH 插件版本升至 `2.1.0`。

### 测试套件分层与维护门禁

- 314 个测试模块按 `core`、`transport`、`scripts`、`tooling`、29 个插件和跨插件契约重新分层；插件测试、fixture 与辅助代码归入明确的领域目录，测试路径统一通过稳定根目录常量解析。
- 清理自建 asyncio fallback、失真的兼容测试、Pendo 历史脚本专用断言和 aiohttp 旧适配层；Codex fake、HTTP response/session double、Pendo 数据库生命周期及 monitor/reply checker 公共支撑统一进入 `tests/helpers/`。
- 历史 review/fixes/regressions 测试桶按实际行为域重新命名；10 个超过 1200 行的测试模块完成职责拆分，当前单文件上限由质量门禁固定为 1200 行。
- 测试质量门禁禁止根目录扁平测试、插件根目录散落测试、仓库深度路径推导、历史桶命名和未登记的平台 skip；CI、命令场景、README 验证路径与新目录保持一致。

### Windows 双击安全停服

- 新增 `scripts/stop-bot.vbs` 双击入口，停服完成后显示结果；再次双击 `scripts/run-bot.vbs` 即可启动新进程。
- `scripts/run-bot-monitor.ps1 -Stop` 复用仓库级互斥量与 PID 状态，先结束自动拉起监控器，再按绝对命令路径回收 Bot 日志泵、`main.py`、NapCat 日志泵和指定 NapCat 进程树。
- 停服流程仅匹配当前仓库与指定 NapCat 路径，支持已停止状态下重复执行；隔离 Windows 进程树测试确认无关 Python 进程持续运行。
- CIM/WMI 命令行身份读取增加三次有界重试；持续不可读且当前桌面令牌未提升时请求一次 UAC，提升后的停止实例重新校验全部路径与进程身份。
- 内部提升标记阻止递归 UAC；取消授权、提升子进程失败或提升后身份仍不可读时保持失败关闭，进程停止始终要求经过命令路径身份验证。
- `scripts/sync_to_remote.sh` 将停服入口纳入生产关键文件完整性与 SHA-256 复核。

### 读者文档统一与移动端版式

- 47 份发布范围 Markdown 统一采用面向新用户、部署者或维护者的当前状态说明。根 README 提供单一路径，`docs/` 按概览、上手、架构、开发、API、配置、高级主题、消息流和插件目录分工。
- 29 个插件 README 统一说明使用条件、命令、配置、数据边界、排障和验证；Pendo 与 XiaoQing Chat 的架构文档聚焦内部服务边界和数据流。
- 插件开发指南覆盖 Manifest 全字段、命令继承与冲突、入口顺序、Context、受控 HTTP、外部内容、AI、持久化、服务、capability、错误与敏感审计，并通过 Python 示例语法和运行时 schema 契约测试。
- 配置详解覆盖发行示例的全部 Core 字段、插件命名空间、secrets、字段范围、接纳容量、AI fallback、热重载与重启矩阵；`secrets.json.example` 补充可选 `onebot_token`，文档链接锚点与当前插件 README 对齐。
- `xiaoqing_chat` 上线验收指南覆盖自动化、OneBot、命令、消息段、群聊参与、模型、多模态、记忆、安全、并发和关闭流程，并提供统一报告模板。
- 一级、二级标题采用一致的主题图标和章节分隔线，长文档保持原有信息与链接结构；装饰归一化后的内容指纹 47/47 一致，482 个标题和 435 个章节分隔通过结构审计。
- Markdown 台账、代码审查记录和远端同步记录作为本地维护资料由 `.gitignore` 管理，发布索引保持纯净。

### 全仓代码清理与内部服务边界收敛

- 按 Core、29 个插件、脚本和测试支撑的顺序完成全仓清理。调用链核验覆盖死接口、空生命周期入口、测试专用生产 API、动态入口和公共契约。
- `core/app.py` 从约 2025 行收敛为 343 行；`app_config_apply.py`、`app_plugin_context.py`、`app_lifecycle.py` 分别承接配置发布、插件能力签发和生命周期。`XiaoQingApp` 保持同一进程稳定门面与既有启动方式。
- Pendo 保持一个 `Database`、一个 `pendo.db` 和统一事务语义。`db_schema.py`、`db_auth.py`、`db_reminders.py` 分别承接 schema、Web 认证和提醒仓储；`event_editing.py`、`event_views.py` 分别承接日程编辑解析和详情展示。清理已替代的元数据解析器、通用 JWT、测试包装和 Help 顺序常量。
- XiaoQing Chat 清理 Heartflow 同步层和 `http_session` 透传，统一 JSON 根扫描、动态动作返回与投递提交契约；本地存储按字段收窄畸形值，AI、媒体、后台任务和生命周期采用明确降级边界。
- 清除全部 45 个 Mypy 文件级排除项，`python -m mypy core plugins` 实际检查 369 个生产模块。统一 48 个格式漂移文件，707 个 Python 文件通过 Ruff formatter 和 lint。
- 新增发行资源静态门禁，要求 Git 跟踪的插件资源与 setuptools package-data 展开结果完全一致。wheel/sdist 包含 67 个插件资源；训练、实验目录和 Minecraft 本地配置归入外部资产。arXiv 大模型由 `sync_to_remote.sh` 检查并复核 SHA-256。
- 清理阶段验证：串行全量测试 5959 passed、10 skipped；`pytest -n 2` 为 5973 passed、2 skipped；共收集 5975 项。Ruff、Mypy、compileall、38 个 JSON 严格解析/重复键、26 个 JavaScript、Bash/PowerShell/VBS、`pip check`、实包检查和 `git diff --check` 全部通过；缓存清理完成。

### 完整 UAT 运行隔离与发布验证

- `scripts/run_full_uat.py` 在隔离运行期间启用 Inbound Server，并关闭外连 WebSocket 与插件文件监视；验收结束后按原始字节恢复 `config.json`，生产配置可直接用于本地完整 UAT。
- `python main.py` 实际启动验证加载 29 个插件，Pendo Web 正常监听并完成优雅关闭；WebSocket 与 HTTP 命令矩阵、Core 压测、compileall、Ruff、Mypy、pytest 和双 diff 门禁均通过。
- 完整 UAT 最终结果为 5978 passed、2 skipped，覆盖率 80.69%；配置与 secrets 哈希保持一致，端口 12000/12001 正常释放，隔离数据、锁文件和子进程全部完成清理。发布前按 GitHub Actions 参数复测同为 5978 passed、2 skipped，覆盖率 80.68%。
- CI 文档格式门禁覆盖 Markdown 中的 Python 示例；Windows smoke 将 POSIX 进程组专属用例登记为精确的平台预期项，skip 策略继续联合校验节点、原因和运行平台；向量检索候选索引使用显式 NumPy `int64` 数组类型，兼容干净安装中的新版类型定义。
- 可选 Torch 训练用例按节点登记依赖条件，基础安装保持轻量；发行资源门禁使用 NUL 分隔且关闭 Git 路径转义，中文文件名在 Windows 与 Linux 上采用同一真实路径语义。

### 生产代码与 arXiv 模型一体同步

- `scripts/sync_to_remote.sh` 将 `plugins/arxiv_filter/best_model/` 作为生产发布资源，同步前检查模型配置、权重、tokenizer 和训练配置，同步后复核完整性。
- 远端主机和目标目录集中保存在 Git 忽略的 `scripts/sync_to_remote.local.sh`，部署者可直接编辑本机配置。
- 同步复用 `.gitignore` 处理本地缓存和训练产物，并通过 rsync 排除规则保护生产配置、Minecraft 连接配置、日志、数据库、插件缓存、备份和导出。
- 增加 checksum 比对、延迟替换和断点保留；正式同步前校验常规文件，完成同步后逐文件核对关键代码、启动链和 arXiv 运行权重的 SHA-256。
- 脚本默认执行 dry-run，实际写入要求 `--apply --confirm-delete`。进程停服与启动由部署者控制，脚本帮助提供预览、应用和校验顺序。

### 手机端分层命令帮助

- `/help` 文本目录采用渐进式下钻：总览分页显示插件，插件页显示一级入口，命令路径页显示直接子命令，叶节点页显示用法、别名、权限、场景和样例；所有插件共享同一格式。
- 文本菜单与搜索结果采用约 34 个手机显示宽度，在参数边界换行，并提供继续查看、上下页和返回入口。稳定 code、权限和样例集中在叶节点详情。
- `/help json [查询] [page N]` 保持完整字段、扁平目录和分页契约；`bot_core` 插件版本升至 `0.4.0`。

### Minecraft 消息帮助补全

- `/mc help` 现在明确展示 `/mc say <消息>` 全服广播和 `/mc tell <玩家名> <消息>` 定向私信，并说明日志监控启用后玩家聊天、加入和离开等事件会转发到发起连接的 QQ 私聊。
- Minecraft 消息示例采用 `/mc say 大家好`；`say`、`tell`、`tellraw` 通过 RCON 原样执行。
- manifest 的统一帮助示例同步补入广播与私信用法，Minecraft 插件版本升至 `4.0.1`。

### arXiv 源列表与 Codex 摘要身份修复

- `/arxiv` 回复显示 arXiv 源站列表的实际发布日期，推理缓存按源日期隔离。源日期校验异常时返回论文列表并跳过 Codex 投递。
- Codex arXiv 摘要使用“源列表日期 + 规范化论文链接集合”识别历史结果和在途任务；同日列表内容变化会创建新任务，链接顺序、版本号、PDF 后缀和查询参数归入同一规范化集合。
- 新增缓存代际切换、同日多列表任务、运行中任务和规范化集合重发回归测试；`arxiv_filter` 升至 `0.2.0`，`codex` 升至 `1.1.1`。
- Linux 并行发布门禁通过同目录临时文件和原子替换发布 PID 标记，监控器清理测试读取完整快照。

### Shell Git Bash 终端与 Windows 错误提示

- 新增 `config.plugins.shell.terminal` 公开配置：默认终端为跨平台 `direct`，Windows 部署可通过明确的 `executable` 选择 `git-bash`；Git Bash 使用 `--noprofile --norc -c` 启动，配置校验采用 fail-closed 语义和明确路径。
- 命令文本进入 Git Bash 前执行危险模式和首入口启用校验；`/shell list` 按当前终端查询可用入口，`/shell help` 按配置终端展示示例。Shell 插件版本升至 `2.1.0`。
- Windows 生产环境中的 `/shell ls`、`/shell pwd` 支持 Git Bash；程序路径解析错误会转换为清晰的用户提示。
- `/shell list` 分组显示 Bot PATH 可解析入口和缺失入口；启用列表承担命令授权边界，工具安装由部署环境管理。
- `/shell help` 按运行平台和终端展示示例：Windows direct 使用 `cmd /c dir`、`cmd /c cd`，Git Bash 与 Linux/macOS 使用 `ls`、`pwd`。环境和终端路径继续由部署者准备。

### Windows 生产启动链

- 修复 `scripts/run-bot-monitor.ps1` 在 Windows PowerShell 5.1 中通过 `-File`/`run-bot.vbs` 启动时的脚本目录解析问题；相对默认路径在参数绑定完成后解析，脚本目录校验采用 fail-closed 语义。
- 新增源码契约与真实 Windows PowerShell 回归测试，覆盖省略 `-BotRoot` 的默认启动路径以及配置驱动的 Bot 环境变量作用域；监控器测试文件 31 项全部通过。
- 默认路径回归测试使用隔离配置；Windows smoke 固定使用两个 pytest worker，并为 PowerShell 进程启动提供充足的 CI 时间预算。
- `scripts/run-bot-monitor.ps1` 按部署配置、路径解析、进程识别、启动和监控顺序组织。Conda、虚拟环境和 Python 路径由部署者准备，脚本调用 `PATH` 中的 `python`。
- NapCat QQ 账号由 `config/config.json` 的 `napcat_account` 提供，启动器将其作为 NapCat 的第一个位置参数传入。
- 可选的 `mkl_threading_layer` 配置在 Bot 日志泵进程树创建期间设置 `MKL_THREADING_LAYER`，进程创建后立即恢复父进程环境，用于协调 MKL/OpenMP 运行库。
- PID 文件在内容损坏、目标进程结束或 PID 命令行身份变化时清理；CIM/WMI 查询异常采用 fail-closed 语义。监控脚本采用 Windows PowerShell 5.1 所需的 UTF-8 BOM，并由双宿主解析回归覆盖。

### Pendo Web 登录凭据

- `/pendo web token` 的私聊凭据消息包含原始登录 Code。Code 可兑换一次，有效期为 7 天。
- 浏览器 Cookie 会话有效期为 7 天；一次性 Code 和浏览器会话持久化在现有 `pendo.db` 中，凭据正文以摘要登记，Bot 与 Pendo Web 重启后继续按绝对到期时间校验。
- `/pendo web widget-token` 的默认有效期为 365 天，支持按用户持久化登记和 `/pendo web widget-revoke` 主动吊销。
- 登录 Code、浏览器会话和 Widget JWT 的期限参数统一使用秒，并共享同一绝对到期时间计算；浏览器 Cookie/CSRF 与只读 Widget Bearer 的权限边界继续分离。
- 同步更新 Pendo 登录页、命令帮助、配置说明和 Scriptable 文档，并新增登录 Code 原始投递、期限参数和 NapCat 配置启动参数回归测试。
- 验证：`pytest -q -n 2` 全量通过（5941 passed、5 skipped，覆盖率 80.76%），Ruff 全仓检查、307 个生产源文件的 mypy 检查、compileall 与 `git diff --check` 均通过。

## 2026-08-05 (v4.2.0)

### v4.2.0 发布

- 发布 `v4.2.0`，收录全量代码审查整改、运行时服务边界、安全默认、统一 AI/VLM 路由、全插件命令 UAT、Pendo 与 XiaoQing Chat 改进，以及发布与运维工具整理后的当前主线。
- 使用生产环境现存 `plugins/*/data/` 与日志快照完成发布验收：Pendo、QingPet 的 WAL 数据库在布局迁移前通过 `quick_check` 与完整 `integrity_check`，29 个插件在隔离数据根中完成数据布局升级，并连续三次正常启动、优雅停机和释放端口。
- 完整 UAT 的 WebSocket/HTTP 命令矩阵、动态 CRUD/清理场景、Core 压测、compileall、Ruff、mypy、pytest 与双 diff 门禁全部通过；全量测试为 5914 passed、1 skipped，覆盖率 80.79%，配置逐字节恢复且 secrets 哈希一致。外部依赖与真实付费模型质量测试采用显式 opt-in 阶段。
- 补齐干净环境暴露的可选功能依赖契约：源码 checkout 的完整依赖加入 arXiv 数据与训练层所需的 pandas、scikit-learn，`astro` extra 与 Astro Tools 清单加入宇宙学积分所需的 SciPy；同时固定 Shell 的 Windows 路径规范化语义，并让 Windows 监控器测试探针使用跨平台临时目录 API。

### 全插件命令 WS 联调、Pendo 示例修复与文档同步

- 新增统一 `bash scripts/run_full_uat.sh` 上线验收入口：隔离插件数据、真实启动/优雅停机、HTTP/WS 命令矩阵、动态业务场景、Core 压测及 CI 门禁统一产出可续查报告；外部依赖和付费聊天质量显式选择，插件/命令/用例类型可定点复测。
- UAT 与 Windows 监控器调用当前 `PATH` 中的 `python`；Conda、venv、解释器路径和依赖由部署环境管理。
- UAT 文本子进程显式使用 UTF-8 和 `errors="replace"`，Git Bash 直接运行 `pytest -n 2` 可通过跨平台编码门禁。
- 通过 Inbound WebSocket（`/ws`）联调 Core 与全部 28 个命令插件：370 个命令节点、404 条 `invalid_examples` 和全部 `examples` 均完成实测；外部服务连接异常与凭据过期场景进入插件降级路径。
- Pendo 快捷 token 示例统一采用 `plan:2026-08-01` 与 `deadline:2026-08-01T18:00`，与解析器的 `plan:YYYY-MM-DD`、`deadline:YYYY-MM-DDTHH:MM` 契约一致。
- Dispatcher 文档统一采用 A–G 线性流程：处理门控 → URL → 只喊名字 → 会话 → 命令 → 未知命令 → 闲聊回落。URL 解析位于门控与静音阶段之后；观察入口接收普通聊天候选消息。
- README 与 Core 模块表统一采用真实组件名称，并覆盖 `ai`、`plugin_execution`、`delivery`、`safe_http`、`bounded_http`、`atomic_store`、`bounded_file_cache`、`durable_fanout`、`async_keyed_lock`、`auth`、`inbound_policy`、`lifecycle`、`public_errors`、`sensitive_audit` 和 `version`。
- `docs/03-plugin-development.md` 提供五步插件教程，覆盖子命令、`command_invocation`、`data_dir`、加载与测试；README 提供教程入口。
- 验证：Git Bash `pytest -n 2` 收集 5915 项并以退出码 0 完成（5914 passed、1 skipped）；UAT runner 与编码门禁聚焦回归 8 passed，Ruff 与 `git diff --check` 通过；对修正后的 `plan:`/`deadline:` 快捷 token 和真实 WS 命令路径均已实测。

### 统一 AI/VLM 模型注册表与路由

- 新增 `core/ai.py`，为 OpenAI API 兼容服务提供统一受控路径：公开连接信息位于 `config.ai.providers`，模型 profile 与模态位于 `config.ai.models`，插件在 `config.plugins.<plugin>.ai.routes` 声明有序模型链与调用预算，API Key 位于 `secrets.ai.providers`。
- `XiaoQingApp` 为每个插件构造绑定插件名的窄 `AIService`。调用方提供 route、messages 和任务级参数；provider 密钥与配置命名空间由 Core 管理。每次 `complete()`/`list_models()` 读取最新配置快照。
- route 首个 profile 为主模型，并对可恢复错误按序 fallback；认证与参数错误直接返回，整条链受 `total_timeout_seconds` 约束。管理员可通过 `pinned_model` 指定 profile。响应用 `AICompletionResult` 返回内容、路由和尝试信息。
- 同步 `docs/04/06` 的统一 AI/VLM 注册表说明。

### 运行时版本解析

- 新增 `core/version.py`，从 `pyproject.toml` 或已安装 wheel 元数据解析运行时 `VERSION`（当前 `4.2.0`）；`/health`、Inbound Server 等统一读取该值。

### 崩溃安全的定时通知扇出、投递回执与有界文件缓存

- 新增 `core/durable_fanout.py`：为定时通知按目标记录崩溃安全进度，进程重启后从持久检查点继续投递。
- 新增 `core/delivery.py`：提供进程内投递回执，支持“先确认送达再提交插件状态”（commit-after-ack），HTTP 响应/WS Action 写入失败会回滚已附加的回执。
- 新增 `core/bounded_file_cache.py`：提供 TTL、LRU、条目与字节上限的崩溃安全磁盘缓存，供插件统一复用。

### 打包、CI 与仓库行尾统一

- 引入 `.gitattributes` 行尾策略：仓库文本统一以 LF 存储与检出（`* text=auto`，`.py/.toml/.json/.yml/.md` 等 `eol=lf`），Windows `cmd/vbs` 使用 CRLF；本次大体量 diff 来自行尾归一化。
- 精简 `Dockerfile` 与 `.dockerignore`，新增 `MANIFEST.in` 规范打包内容，新增 `.github/dependabot.yml`；精简 `tests.yml` 工作流。
- 删除 `dependency-locks` 工作流与 `requirements/` 锁文件目录。仓库通过根目录 `requirements.txt` 维护直接依赖，由 pip 按当前 Python 和平台解析。

### 补充清理（代码审查闭环之外）

- 清理代码审查过程文件、覆盖率制品、运行时路径清单和已替代的发布脚本。
- 清理插件过程文档与已替代的 AI 客户端；Pendo AI 调用统一进入 `core/ai.py`，XiaoQing Chat PFC 阶段由类型化协调器承接。

### 全量代码审查整改闭环

- 完成 285 项代码审查整改：统一高权限边界、网络与输出预算、事务/幂等、隐私隔离、取消清理、发布制品和测试可信度门禁。
- 删除 Sony 签到死实现、ADNMB 死模块、QingPet 已替代 API、手工 pytest runner 与已跟踪的 `coverage.json`；生成型 coverage 文件统一由 `.gitignore` 管理。
- 将 Xiaoqing Chat 回复生成、PFC 和 Codex runner 拆成有类型的阶段协调器，并用 AST 回归门禁限制重新膨胀。
- QingPet 群统计采用常数次聚合查询，`users.coins` 作为余额权威来源，并加入基于持久检查点与 `asset_ledger` 净变动的差异检测。
- 同步插件开发/API/高级用法、Voice/Signin 示例、Pendo 依赖说明和真实 GitHub 项目链接。

### 远端同步脚本交付

- 将 `scripts/sync_to_remote.sh`、固定根目录哨兵、运维说明和安全契约测试纳入版本化项目资产，干净 checkout 可直接预演同步。
- Shell 脚本采用 LF，`scripts/sync_to_remote.sh` 在 Git 索引中保留可执行位，默认模式为 dry-run。

### Pendo Web 细节优化与 Token 有效期延长

- Pendo 插件配置中的 `WEB_TOKEN_EXPIRE_HOURS` 设为 168 小时（1 周）。
- 统一 Pendo Web 中（如 `notes.js`、`tasks.js`、`transfer.js` 等）的所有日期输入框为 `YYYY-MM-DD` 的文本输入形式，去除自带的日历弹出，向账本快速记账对齐，方便直接键盘录入。
- `ledger.js` 的“自定义时段”日期框采用独立整行布局，完整展示两个日期输入框。
- 统一日期输入框样式：去除了默认的数字加粗，取消了输入框获取焦点时的底色填充，保持各页面一致的视觉体验。

### Pendo 待办轻量创建

- 简化 `/pendo todo add` 空参数交互流程：默认询问待办内容和计划日期，计划日期确认后立即创建待办；截止时间、提醒、分类、优先级和标签通过单行快捷参数提供，新增回执聚焦用户填写的字段。
- `/pendo todo add <内容> ...` 支持 `plan:`、`deadline:`、`remind:`、`cat:`、`p:` 和 `#标签` 等单行高级参数；后续 session 步骤承担交互式扩展字段。
- 同步 Pendo README、聊天帮助和插件文档，让默认交互与高级参数的分层更清楚。
- 已执行 `python -m pytest tests/plugins/test_pendo.py -q`、`python -m pytest tests/plugins/test_pendo_review_regressions.py tests/plugins/test_pendo_fixes.py -q`、`python -m compileall plugins/pendo/main.py plugins/pendo/handlers/task.py -q` 和 `git diff --check`。

### arXiv 筛选自动摘要

- `/arxiv` 和工作日定时筛选先发送论文列表，再把所有 positive 论文链接后台投递给 Codex `astro-ph` 会话生成 Markdown 摘要；论文列表提交与摘要任务采用独立消息链路和错误边界。
- Codex `astro-ph` 会话按需自动创建，初次使用会先投递静默初始化任务，再投递当天链接；同日成功结果可直接重发，其他状态会创建摘要任务。
- 摘要 prompt 引导 Codex 读取工作目录下的 `arxiv-summary-methodology.md`，方法正文由工作区文件统一维护。
- `astro-ph` 默认受保护，删除需要 `/codex delete astro-ph --force --protected`，历史目录会归档到 `plugins/codex/data/deleted_sessions/`。
- 将 arXiv 摘要业务收拢到 `plugins/codex/arxiv_summary.py` 和 `plugins/arxiv_filter/codex_summary.py`，Codex 主队列仅保留通用 metadata、队列、归档和发送能力。
- 同步根 README、`docs/00-09` 中涉及 `arxiv_filter` 和 `codex` 的配置、消息流、运行时数据和插件手册说明。
- 已执行 `python -m pytest tests/plugins/test_arxiv_filter.py tests/plugins/test_codex.py -q`、`git diff --check` 和新增/改动文件尾随空白检查。

## 2026-05-24

### Pendo 群聊隐私会话续写

- Pendo 隐私模式将群聊多轮命令的提示与 session 一并建立在私聊作用域，并记录来源 `group_id`。
- Pendo 回复作用域辅助逻辑根据隐私配置选择私聊或群聊 session，后续消息沿同一作用域继续处理。
- 将该逻辑接入待办交互添加、账本交互记账、日记模板和日程补充信息/冲突确认会话，并更新 Pendo README 的群聊隐私模式说明。
- 补充 Pendo session 回归测试，覆盖群聊隐私模式下的私聊续写，以及隐私模式关闭时继续使用群聊 session。
- 已执行 `python -m pytest tests/plugins/test_pendo.py -q`、`python -m compileall plugins/pendo/main.py plugins/pendo/utils/session_utils.py plugins/pendo/handlers/task.py plugins/pendo/handlers/ledger.py plugins/pendo/handlers/diary.py plugins/pendo/handlers/event.py` 和 `git diff --check`。

## 2026-05-18

### Pendo 待办交互添加

- `/pendo todo add` 的空参数形式进入多轮交互，依次收集待办内容、计划日期、截止时间、提醒时间、分类、优先级和标签。
- 保留 `/pendo todo add <内容> ...` 的单行快捷添加方式，并继续支持 `plan:`、`deadline:`、`remind:`、`cat:`、`p:` 和 `#标签` 等参数。
- 交互式添加的扩展字段可输入 `0` 采用默认设置，包括默认计划日期、开放截止时间、提醒关闭、默认分类、优先级 3 和空标签。
- 补充 Pendo todo 回归测试，覆盖空参数 session、带参数快捷添加、默认值流程和计划日期校验提示。
- 已执行 `python -m pytest tests/plugins/test_pendo.py -q`、`python -m pytest @files -q`（`files` 为 `tests/plugins/test_pendo*.py`）、`python -m compileall plugins/pendo -q` 和 `git diff --check`。

## 2026-05-15

### Pendo Web 账本筛选样式

- 统一 Ledger 页面“筛选范围”中金额上下限输入框与时段、账户、分类筛选框的高度、内边距、边框、圆角、背景和字重。
- 金额筛选输入框的 hover/focus 状态与同区域 custom select 采用一致视觉规则；快捷记账金额框和自定义日期框保留各自样式。
- 已执行 `node --check plugins/pendo/web/static/js/pages/ledger.js`、`git diff --check`，并通过 Pendo Web demo 页面检查关键 computed style。

## 2026-05-12

### Codex 图片结果透传

- 调整 `plugins/codex` 的运行时数据布局，将每个 Codex 标签的对话记录、图片副本和任务 artifacts 收拢到 `plugins/codex/data/session/<label>/`。
- 每次 Codex 任务自动追加图片输出约定：生成图片保存在本任务 artifacts 目录，并在最终回复中标出路径。
- 任务完成后解析最终文本和 artifacts 目录，并兜底扫描 `$CODEX_HOME/generated_images/` 中本次任务期间新增的图片，把本地图片复制到会话图片目录，并通过 QQ image 消息段随文字结果回发；长文本会先分段再发送图片。

## 2026-05-10

### Codex 后台任务插件

- 新增 `plugins/codex`，通过 `/codex create <name> [cwd:<path>]` 创建 Codex 会话标签，通过 `/codex <name> <任务>` 向指定会话投递任务。
- Codex 任务使用插件内部独立队列，框架 Session 专注聊天交互；同一标签内串行执行，各标签按 `max_parallel_jobs` 并行执行，完成后主动回发 `[codex:<label> #<job_id>]` 结果。
- 支持 `/codex list`、`/codex status`、`/codex cancel`/`stop`、`/codex clear` 和 `/codex delete`，并支持创建会话时指定工作目录。
- 新增 Codex 路径归一化和允许目录校验。默认工作目录为 `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex`，用户可在 QQ 中统一使用 `/` 斜杠输入路径。
- 将 Codex 会话索引和每个标签的对话 JSONL 保存到 `plugins/codex/data/`，运行时数据继续由 `.gitignore` 排除。

### Shell 路径解析改进

- 改进 `plugins/shell` 的参数拆分和路径识别，Windows 反斜杠在参数中完整保留。
- 用户可在 QQ 中统一输入 `/` 斜杠路径，插件按 Bot 所在系统归一化；URL、`cmd /c`、`xcopy /Y` 等参数由独立规则分类。
- Windows 的 `copy`、`del`、`type` 等 Shell 内建命令通过 `cmd /c` 执行；`cp`、`xcopy`、`robocopy` 等外部程序可直接执行。

### Pendo 账本录入

- `/pendo ledger add` 的空参数形式进入交互式记账，金额和描述使用文本输入，交易类型、账户和分类使用数字选择。
- 保留 `/pendo ledger add <金额> <描述> ...` 的单行快捷记账方式，并继续支持 `cat:`、`in/out/transfer`、`account:`、`to:`、`merchant:`、`date:` 和 `remark:` 等参数。
- 补充 Pendo ledger 和主入口测试，覆盖交互步骤、默认值、转账校验和快捷录入。

### 文档同步

- 更新根 README、`docs/00-09`、`docs/README.md`、Pendo README、Shell README 和 Codex README，让项目手册描述当前项目结构、命令、配置、后台队列和路径规则。
- 补充 `PluginContext.send_action()`、后台任务队列、Codex 配置、Shell 配置、Pendo ledger 交互记账和插件统计说明。
- 检查 README 和 `docs/*.md` 的文档口吻，保持为项目说明和使用手册；更新流水账只保留在本 CHANGELOG 中。

### 验证

- 已执行 `git diff --check`。
- 已在维护者本地 Python 环境执行 `python -m pytest tests/plugins/test_codex.py tests/plugins/test_shell_plugin.py tests/plugins/test_pendo.py -q`，结果为 `180 passed`。

## 2026-05-08

### Pendo Web 端口整理

- 将 Pendo Web 默认端口从 `8765` 调整为 `12001`，避开 Windows TCP excluded port range 导致的 `WinError 10013` 绑定失败。
- 统一本地端口说明：OneBot/NapCat 保持 `11000`/`11001`，XiaoQing inbound 使用 `12000`，Pendo Web 使用相邻的 `12001`，备用示例使用 `12002`/`12003`。
- 更新 README、Pendo 文档、配置文档、Scriptable 小组件说明、nginx 反向代理示例和 Pendo 测试说明中的默认地址。
- 已在生产 Windows 主机上验证 `127.0.0.1:12001` 和 `127.0.0.1:12003` 可绑定；已执行 `pytest tests/test_config.py tests/test_server.py tests/plugins/test_pendo.py tests/plugins/test_pendo_web_widget.py` 验证相关配置、inbound server 和 Pendo 回归覆盖。

## 2026-05-07

### xiaoqing_chat 拟人对话自然度改进

- 新增 `humanize` 配置，支持按输入阅读量和输出长度计算拟人化打字等待，并在多段回复之间加入可配置的间隔。
- 调整人格状态刷新策略，让当前 mood 在有效期内保持稳定，长时间空闲后重新抽取，减少每轮都换状态导致的语气漂移。
- 收紧 prompt 和 reply checker 的语气约束，降低“哈哈”“笑死”“啊这”等填充开头的连续复用概率；陌生话题优先表达置信度或提出澄清问题，内容生成以完整语义为准。
- 表情包注入逻辑让达到配置阈值的高频学习表达进入上下文，低频表达进入审核流程。
- 重写媒体分析提示词，让图片/表情包描述先保持客观；新增可选的梗背景提取、缓存和上下文标记，用于降低只读到图中文字就强行接梗的情况。
- 补充 reply checker、表达注入、拟人打字延迟和 mood 生命周期测试；已执行 `python -m compileall -q plugins/xiaoqing_chat`、`git diff --check`、`python -m pytest tests/plugins -k "xiaoqing or reply_checker" -q` 验证。

## 2026-05-03

### Pendo Web 待办与账本界面修复

- 修复 Pendo Web 待办概览中“今天与滞后”任务最多只返回 6 条的问题，让今天和已滞后的待办完整展示。
- 调整待办列表页的展示方式，减少多层圆角容器嵌套，改用更扁平的分区和行式任务列表。
- 优化 Pendo Web 账本页的快速记账和筛选范围表单布局，统一字段网格、标签和金额筛选输入框宽度。
- 已执行 `pytest tests/plugins/test_pendo_web_items.py tests/plugins/test_pendo_web_tasks.py tests/plugins/test_pendo_web_widget.py -q` 验证相关 Web 回归覆盖。

## 2026-05-01 (v4.1.0)

### 文档体系整理

- 重写根目录 `README.md`，把 XiaoQingBot 描述为可长期运行的 QQ 机器人项目，并补充核心能力、代码结构、消息处理概览、部署、插件、测试和排障入口。
- 重写 `docs/README.md`，把 `docs/00-09` 的阅读顺序、目标读者、插件文档入口和维护约定整理成清晰目录。
- 系统更新 `docs/00-overview.md` 到 `docs/09-plugins.md`，让这些文档成为当前项目状态的项目手册。文档覆盖项目定位、快速开始、系统架构、插件开发、核心模块、API、配置、高级用法、消息链路和内置插件。
- 调整文档语气，减少流水账、问句、夸张表达和硬邦邦的说明。保留技术准确性，同时让阅读体验更自然。
- 更新 `docs/pendo-scriptable-widget.md`，说明 Pendo iPhone Scriptable 小组件的只读接口、配置方式和日常使用场景。

### Pendo 文档与测试资料整理

- 为 `plugins/pendo` 保留插件级 `README.md` 和 `ARCHITECTURE.md` 两个入口。README 面向日常使用和部署，ARCHITECTURE 面向维护者理解数据模型、命令、Web API、调度和导入导出。
- 删除与 README 高度重复的 `plugins/pendo/Pendo个人时间与信息管理中枢.md`，由 `plugins/pendo/README.md` 统一承载使用说明。
- Pendo 的正式验证入口统一为仓库内自动化测试；本地黑盒测试说明和执行记录由 Git 忽略规则管理。
- 清理 Pendo test reports，只保留 final report、关键截图、迁移样例、完整测试脚本和有复盘价值的报告。中间过程文件和一次性结果文件已移出版本控制。

### xiaoqing_chat 文档与测试资料整理

- 为 `plugins/xiaoqing_chat` 新建插件级 `README.md` 和 `ARCHITECTURE.md`。README 说明拟人聊天、多模态收发、attention gate、频控、记忆、planner、reply checker、表情包和测试命令。ARCHITECTURE 说明主链路、上下文构建、should_reply、planner、回复生成、多模态发送、记忆和实验框架。
- 调整 `plugins/xiaoqing_chat/xiaoqing_chat测试.md`，让它成为完备测试 prompt。测试要求先读取或创建 `CURRENT_RUN_ID`，再从 `xiaoqing-test-results.jsonl` 重建进度，并按 case_id 矩阵继续执行。
- 把最新拟人测试框架写入 xiaoqing_chat 测试 prompt。测试覆盖大群聊天、文本、QQ face、NapCat mface、图片、表情包、reply 引用、上下文共指、频控、planner、reply checker 和多轮对话。
- 保留 xiaoqing_chat 中有价值的 test reports，例如 3000 轮对话信息、最终分析报告和关键数据。清理中间状态文件，减少仓库噪声。

### xiaoqing_chat 回复决策改进

- 改进 should_reply 主路径，把 `@`、bot name、reply-to-bot、上下文共指和群聊插话统一放到 attention gate 中处理。
- 共指识别覆盖最近上下文、Bot 相关代词和显式 mention，提升“小青”相关指代场景的召回率。
- 清理 `heartflow.weight_mentioned` 等脱离主回复决策的重复权重逻辑，收敛 planner、frequency control 和 attention gate 的职责。
- 调整频率限制设计，让基础冷却、群聊随机插话、强触发和上下文触发分层执行；强触发直接进入对应回复路径。
- 补充 xiaoqing_chat 单元测试和拟人实验测试，覆盖 attention gate、频控、多模态消息接收、媒体回复和上下文触发。

### 仓库维护

- 整理 `.gitignore`，忽略插件实验输出、测试中间产物、运行日志、缓存文件和本地状态文件。
- 检查 `plugins/xiaoqing_chat/experiments` 的归档价值。实验代码和可复现脚本保留，批量输出和临时数据进入忽略规则。
- 新增根目录 `CHANGELOG.md`，作为项目级更新记录入口，后续维护时按日期记录每次改动的内容、影响范围和验证结果。

### Pendo 账本录入简化

- 简化 Pendo 账本添加流程，减少日常记账时需要输入的参数。
- 调整账本 handler、主入口和相关文档，保证新流程在聊天命令和插件说明中保持一致。
- 扩展 `tests/plugins/test_pendo.py`，覆盖账本录入的常见参数组合和回归场景。

### Pendo 文档刷新

- 刷新 Pendo README、架构说明、项目 README、docs 目录和插件清单中的 Pendo 相关说明。
- 梳理 Pendo 数据模型、Web 控制台、Scriptable 小组件、迁移、备份和排障信息。
- 减少重复文档内容，让插件说明更集中地落在 README 和 ARCHITECTURE 中。

### Pendo 带时区提醒解析

- 提醒解析支持带时区时间。
- `plugins/pendo/utils/validators.py` 统一处理带时区的既有提醒记录，迁移和提醒加载采用明确的时间规则。
- `tests/plugins/test_pendo_event_migration.py` 覆盖带时区提醒的数据升级路径。

### xiaoqing_chat 测试规范更新

- 大幅整理 `plugins/xiaoqing_chat/xiaoqing_chat测试.md`，把测试 prompt 从临时记录改成可复用的完整测试规程。
- 测试运行基于 `CURRENT_RUN_ID`、状态文件和 case_id 矩阵恢复，压缩后的任务上下文可从逐项进度继续执行。
- 加入多模态、拟人程度、群聊插话、planner 和 reply checker 的测试要求。

### 插件测试资料纳入版本控制

- 将 Pendo 和 xiaoqing_chat 的测试 prompt 纳入仓库，作为插件级测试入口。
- 更新 `.gitignore`，忽略测试报告中的批量中间文件。
- 删除大体积、一次性、难以复用的历史测试输出，让仓库保留更有用的最终报告和可复现脚本。

### Pendo redesign 覆盖扩展

- 扩展 Pendo redesign 的命令、Web、迁移和回归测试。
- 增加 dashboard、settings、transfer 等 Web 表面的覆盖。
- 修复 scheduled command、transfer bundle、settings API 和 dashboard overview 中发现的回归问题。

## 2026-04-30

### Pendo redesign 流程加固

- 加固 Pendo 事件、待办、笔记、日记、账本、搜索、设置、导入导出和 Web API 的核心流程。
- 调整 command router、handler、service、model、Web API 和前端页面，让 redesign 后的数据模型在聊天命令和 Web 控制台中保持一致。
- 增加多代理测试报告、黑盒测试脚本、浏览器截图、迁移样例和 pytest 覆盖。
- Windows PowerShell 测试命令先枚举 pytest 文件，再把明确路径交给 pytest。

### Pendo 测试输出忽略

- 更新 `.gitignore`，将 Pendo redesign 的测试中间报告、浏览器运行输出和生成数据排除出普通版本控制。
- 仓库保留 final report、脚本和关键截图；一次性测试文件由 Git 忽略规则管理。

## 2026-04-29

### Pendo 事件引用和子节点校验

- 修复 Pendo note reference 编辑时引用信息丢失的问题。
- Pendo event collection children 增加合法性校验，事件树仅接纳有效子节点。
- 合并 pendo redesign 分支期间同步 master 上的修复。

### xiaoqing_chat 多模态回复改进

- 改进 xiaoqing_chat 的媒体回复链路，让主 LLM、prompt builder、reply checker 和 marker resolver 可以更稳定地处理图片、表情包和 QQ face 标记。
- 将 xiaoqing_chat 媒体资源存放到 data 目录，减少插件代码目录和运行数据混在一起的问题。
- 在收到 emoji 时保留可见文本，让 LLM 和 reply checker 能理解表情上下文。
- 修正文档中的媒体库路径，减少部署时的路径歧义。

## 2026-04-28

### xiaoqing_chat 主链路简化

- 重构 xiaoqing_chat 回复链路，媒体回复与 planner 逻辑归入统一阶段协调器。
- 引入统一的 media marker resolver，让图片、表情包、QQ face 和 mface 的发送标记由一个模块解析。
- 调整 frequency control、context builder、reply generator、reply checker、memory retrieval 和 runtime state，让主链路更清楚。
- 更新 README、docs 配置说明和高级用法，记录新的插件配置和多模态发送方式。
- 增加 xiaoqing_chat runtime hardening、review regression、prompt builder、media 和 reply checker 测试。

## 2026-04-25

### Pendo 笔记流程改进

- 改进 Pendo note workflow，优化笔记创建、引用、更新和展示的体验。
- 单个事件详情中的操作按钮采用明确标签。
- Pendo event fixture 与当前数据模型保持一致。

### 核心与 LLM 兼容修复

- 修复 inbound manager status provider 的绑定问题，保证运行状态查询能拿到正确 provider。
- 透传 vision LLM extra payload，让多模态模型调用可以收到额外参数。
- 修复 xiaoqing planner 和 checker 的边界情况，减少规划和检查阶段的误判。

## 2026-04-24

### Pendo event graph 对齐

- Pendo event graph 统一承载事件叶节点路径。
- 对齐 Pendo markdown export 与 event graph，保证导出结构能反映多节点事件关系。
- 对齐 Pendo Web 表面与 event graph，让 Web API 和前端页面围绕同一套事件模型工作。

### xiaoqing_chat 和 Pendo 小组件加固

- 改进 xiaoqing_chat 的回复流和模型处理，减少多模态上下文和模型调用之间的边界问题。
- 加固 Pendo Scriptable 小组件的数据拉取流程，降低移动端小组件读取失败时的影响。

## 2026-04-23

### Pendo 调度和提醒修复

- 改进 Pendo 调度任务与提醒发送逻辑，让提醒、日程和后台任务之间的行为更稳定。
- 收紧提醒状态更新和调度触发条件，提醒去重与确认语义由明确状态驱动。

### xiaoqing_chat 媒体链路整理

- 精简 xiaoqing_chat 媒体处理流程，把入站媒体、上下文构建和发送标记之间的职责进一步拆清楚。
- 更新忽略规则，本地 worktree 目录由 `.gitignore` 管理。

## 2026-04-22

### Pendo 里程碑提醒语义修复

- 在 Pendo 提醒中补充 milestone briefings，让复杂事件和阶段性安排能给出更有用的提醒摘要。
- 收窄确认语义，降低普通文本被错误识别为确认操作的概率。

## 2026-04-21

### v4.0.0 发布

- 发布 `v4.0.0`，将 xiaoqing_chat 多模态回复链路、Pendo Web 和文档整理后的状态作为新的主线版本。
- 调整面向最终用户的文档语气，让 README 和插件说明更接近日常使用场景。

### xiaoqing_chat 多模态回复加固

- 修复 xiaoqing_chat 多模态回复中的边界情况，提升图片、表情和文本混合消息下的稳定性。
- 媒体片段与 OneBot emoji 使用统一传输规则，发送端 marker 与消息段保持一致。
- 改进回复流和 emoji 修复流程，让 reply checker、媒体解析和最终发送之间的衔接更稳定。
- 更新 xiaoqing_chat 文档，记录新的媒体回复行为和修复流程。

### arxiv_filter 测试兼容

- arxiv_filter 训练测试根据 pandas 可用状态选择执行，普通测试套件保持独立。

## 2026-04-20

### xiaoqing_chat 媒体回复管线重构

- 改进 xiaoqing_chat 媒体回复，让文字回复、图片回复、表情包和 QQ face 能在同一条回复管线中组合。
- 重构媒体回复 pipeline，分散 helper 归入统一处理阶段和状态模型。
- 为后续 marker resolver、reply checker 和多模态上下文统一打基础。

## 2026-04-19

### xiaoqing_chat 图片对话与表情分析

- 增加 xiaoqing_chat 图片对话能力，让插件可以把图片内容纳入群聊理解和回复生成。
- 更新 xiaoqing_chat `0.2.0` 文档，说明图片对话、多模态配置和使用方式。
- emoji 图片、动画表情和媒体上下文进入语义分析流程，并携带结构化媒体描述。

### Pendo 事件编辑和提醒改进

- 改进 Pendo 事件编辑和提醒行为，强化日程修改、提醒生成和提醒展示之间的一致性。
- 调整 watcher 默认值和插件配置行为，减少默认配置下的意外文件监听或热重载问题。

### 审计修复和文档整理

- 修复核心框架和多个插件中的审计发现，覆盖运行时、插件配置、媒体上下文和测试边界。
- 处理 review follow-up 和 remediation 项，恢复 review checklist，并把临时 review notes 从项目文档中移到本地。
- 更新 runtime、xiaoqing_chat 与 Pendo 的对应文档，使接口说明与实现保持一致。

## 2026-04-17

### Pendo 小组件和笔记标题解析

- 细化 Pendo Scriptable 小组件展示和同步行为。
- 改进 Pendo 笔记标题解析，让快速记录和后续检索更稳定。

## 2026-04-15

### Pendo 日历视图和小组件同步

- 优化 Pendo 日历视图，让事件浏览和时间范围展示更清楚。
- 调整 Scriptable 小组件同步逻辑，改善移动端摘要更新。
- 重构小组件背景处理，支持 iOS 原生动态深色模式切换和透明装饰。

## 2026-04-14

### Pendo Web 小组件和服务流程

- 改进 Pendo Web 小组件相关接口和 Web server 流程。
- 调整小组件读取路径和服务端响应，让 Scriptable 侧的数据获取更稳定。

## 2026-04-12

### Pendo Scriptable 小组件

- 增加 Pendo Scriptable widget 支持，把 Pendo 的日程、待办、提醒和摘要信息放到 iPhone 主屏。
- 为小组件准备只读接口和脚本入口，方便移动端查看当天信息。

## 2026-04-08

### Pendo Web 演示和日期范围

- 改进 Pendo Web demo access，让演示模式更容易启动和验证。
- 优化 Web 端日期范围处理，减少看板、统计和演示数据中的时间边界问题。
- 加固 demo import paths，使演示程序在各运行目录下使用稳定导入路径。

## 2026-04-06

### Pendo 笔记解析和多行输入

- 改进 Pendo 笔记解析与帮助文本，让创建笔记、补充内容和查询笔记的命令更清楚。
- 多行笔记输入完整保留换行内容。

## 2026-04-03

### Pendo 提醒和账本交互

- 改进 Pendo 提醒消息结构，让提醒内容更容易阅读，也更方便后续确认或处理。
- 调整 Pendo 账本添加对话顺序，让交互式记账更接近日常输入习惯。

### xiaoqing_chat 群聊参与度

- 提高 xiaoqing_chat 在群聊中的参与度，让普通群聊插话更积极。
- 为后续 attention gate、频控和拟人程度测试提供更高的基础参与率。

## 2026-04-01

### Pendo Web Token 输入修复

- 修复 Pendo Web 粘贴 token 消息的识别问题。
- 允许用户从聊天端或其他来源复制 token 后直接粘贴使用，减少登录流程中的失败点。

## 2026-03-31

### Pendo Web 细节修复

- Pendo Web 图表范围和 token 投递采用统一规则，各页面共享统计口径。
- 优化 Pendo Web 移动端 UI，让小屏幕下的导航、卡片和表单更可用。
- 改进 Web 搜索结果展示，提升笔记、事件、账本和待办混合查询时的可读性。

## 2026-03-30

### Pendo Web 过滤、统计和导出修复

- Pendo Web filters 和 stats ranges 共享筛选条件与统计区间定义。
- 增强 Pendo exporter、日记分析和笔记 UI，提升数据导出、复盘和浏览体验。
- 支持笔记命令显式传入标题和正文，让聊天端创建结构化笔记更可靠。
- FastAPI 导入、任务显示和对应文档完成统一。

### 测试和依赖稳定性

- 修复 dict 与 earthquake 插件的 pytest 稳定性问题。
- Pendo Web auth 和 transfer 测试根据 PyJWT 可用状态选择执行，基础测试套件保持独立。
- 处理 scheduled shutdown cancellation 和 CI 依赖问题，让测试结束和后台任务清理更稳。

## 2026-03-29

### Pendo Web 传输和分析增强

- 扩展 Pendo Web transfer 能力，改进数据导入、导出和迁移流程。
- 增强 Pendo analytics，让 dashboard 和统计页面能提供更有用的概览。
- 修复调度关闭取消和 CI 依赖相关问题，降低测试环境中的偶发失败。

## 2026-03-28

### Pendo 命令保护和日记心情

- 细化 Pendo 命令 guard，参数完整性与格式在业务处理前统一校验。
- 优化日记心情 UI，让日记记录和展示更直观。

## 2026-03-27

### Pendo Web UI V3.0 合并

- 合并 Pendo Web UI console，形成包含 dashboard、events、tasks、ledger、notes、diary、search、stats 和 settings 的 Web 页面套件。
- 增加 PyJWT、FastAPI、uvicorn 等 Web UI 依赖。
- 修复子路径 nginx 部署下的 API 前缀和相对路径问题，让 `/pendo` 子路径部署可用。
- 加固 Pendo event reminders 和 edits，减少 Web UI 与聊天命令同时操作事件时的状态问题。

### 文档 V3.3.0 刷新

- 更新根 README、`docs/00-overview.md` 和 `docs/09-plugins.md`，突出 Pendo Web UI 能力。
- 对全量文档做 V3.3.0 级别刷新，覆盖架构、插件、部署和使用说明。
- 清理过期的 docs/plans 与 docs/superpowers，并把 pytest 临时目录集中到 `.pytest_cache`。
- 对 README 和 docs 做视觉风格刷新，补充徽章、提示块和更清晰的入口说明。

## 2026-03-26

### Pendo Web UI 视觉重构

- 重新设计 Pendo Web UI，统一页面布局、导航、模块颜色和交互视觉。
- 为后续移动端 polish、过滤栏和页面组件一致性修复打基础。

## 2026-03-25

### Pendo Web UI 从零搭建

- 增加 Pendo Web 配置和 Web 包结构，建立 FastAPI server、依赖注入、API router 和静态资源入口。
- 增加 JWT auth 模块和对应测试，提供 Web 控制台登录基础。
- 增加 auth、items CRUD、dashboard、search、stats、settings、config 等 API endpoint。
- 增加聊天命令入口，用于管理和打开 Pendo Web UI。
- 增加 HTML shell、CSS 样式、JS router、API client、store 和 app bootstrap。

### Pendo Web 页面套件

- 增加 dashboard、events calendar、tasks kanban、ledger quick-add、notes card grid、diary timeline、search、stats 和 settings 页面。
- 增加共享 UI 组件和 Chart.js loader，并在 dashboard 和 stats 中复用图表加载逻辑。
- 重做 select 元素和 filter-bar 视觉，让筛选控件更明显、更一致。
- 重构 ledger 页面体验，修复排序和分页问题。

### Pendo Web 修复和文档计划

- 修复 API 错误处理、chart-loader 路径、Web help、API response format、FAB menu 和多处 UI 数据问题。
- 增加 Pendo Web UI 设计 spec、review 修订和 20 项实现计划。
- 改进 Pendo 列表命令的字段过滤，账本日期范围解析委托给通用时间解析逻辑。

## 2026-03-24

### Pendo 账本能力落地

- 增加 `LedgerItem` 数据模型、字段常量、数据库列、账本类型图标和格式化名称。
- 增加账本配置、分类、会话类型、`LedgerHandler`、CRUD、交互式添加和汇总能力。
- 接入账本 session routing、命令路由和 help text，让聊天端可以完成记账、查询、编辑和统计。
- 增加 `old_bill.csv` 一次性账单导入脚本，脚本位于 `plugins/pendo/scripts`。
- 将账本配置中的“美容”分类改为“服务”，并同步导入脚本。

### Pendo 路由、日记和搜索优化

- 将账本编辑命令改为显式字段语法，降低误解析风险。
- 移除账本 payment method 字段，减少早期模型中的冗余属性。
- 用数据驱动的 `COMMAND_META` 简化 Pendo router。
- 改进日记命令体验，并修复日记模板步骤中的 session 写入方式。
- 改进搜索，支持账本、类型分组和更丰富的展示。
- 修复 settings view 命令并补齐缺失开关。

### Core Session 增强

- 增强核心 `Session`，支持类似 dict 的访问方式。
- 增加插件 session cleanup 能力，方便插件在生命周期结束或流程取消时清理会话状态。

## 2026-03-22

### Pendo 多节点事件整理

- 修复 Pendo 多节点事件排序和提醒展示。
- 重构时间工具模块，让事件、提醒、查询和展示共享更一致的时间处理逻辑。
- 删除过期的计划和设计文档，减少历史草案对当前实现的干扰。

## 2026-03-21

### Pendo 复杂度收敛

- 简化 Pendo reminder、event handler、db 和 ai_parser 的复杂度。
- 让提醒、事件处理、数据库访问和 AI 解析之间的职责更清晰。

### QingPet 训练和探索系统

- 增加 QingPet 改进设计 spec、review 修订和实现计划。
- 互动门控提示展示具体原因，并覆盖 `sleep_pet` 场景。
- 取消 recall 对友情点的要求，让召回宠物更直接。
- 增加训练和探索地点配置常量。
- 改进训练和探索系统，加入类型、地点和性格影响。
- 在宠物卡片中展示剩余旅行时间，并更新 train/explore 帮助文本。

## 2026-03-13

### APOD、arxiv_filter 和 xiaoqing_chat 测试修复

- 修复 APOD 解码问题，减少天文图文内容读取失败。
- 更新 arxiv_filter 并继续整理训练代码。
- 统一 xiaoqing_chat goal derivation 逻辑，修复 mock 测试中的错误。
- 修复相关测试，保证天文、训练和聊天模块在重构后继续可测。

## 2026-03-11

### arxiv_filter 训练和推理结构重构

- 重构 arxiv_filter 训练和推理目录结构，让训练数据准备、模型训练和运行时推理边界更清楚。
- 修复 signin 测试，使插件测试与训练代码保持隔离。

## 2026-03-10

### v3.2.0 发布

- 发布 `v3.2.0`。
- 修复 Pendo 事件列表格式，让多节点事件和普通事件的展示更稳定。
- 修复异步 goal state 处理，减少 xiaoqing_chat 目标状态在异步流程中错位的问题。

## 2026-03-04

### Pendo 事件提醒和查询修复

- 确保每个 Pendo 事件在开始时间拥有对应提醒。
- 修复多节点事件 list 过滤逻辑，只有查询范围内存在节点时才展示该事件。
- 动态 SQL 的列名统一加引号，支持与 SQL 保留字同名的字段。

## 2026-03-03

### 本地运行脚本整理

- `run-bot.vbs` 使用相对路径，支持不同本地项目目录。

## 2026-03-02

### 项目迁移和基础 CI

- 将 XiaoQing 迁移为当前 master 仓库，作为后续开发的基线。
- 修复 CI 单元测试路径，统一使用 `tests/`。
- 修复插件标记测试的 CI job，让 plugin-marked tests 能在插件测试任务中执行。
- 更新 README 和项目文档，为迁移后的仓库补齐基础说明。

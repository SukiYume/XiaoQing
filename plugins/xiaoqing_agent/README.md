# 🛠 XiaoQing Agent

XiaoQing Agent 让小青在有人叫她做事时真正动手：查日程待办、查论文和天象、算数、画图、跑代码和命令、查服务器、让 Codex 写程序，做完再用小青的口吻回复。普通聊天照旧由 `xiaoqing_chat` 处理。

---

## 🔐 权限

**agent 使用发起人直接执行命令时的权限。** 各插件在 `plugin.json` 中声明权限。Core 通过 `core.router.command_access` 统一检查权限。agent 的每项能力都跟随对应命令的声明。

| 能力 | 谁能用 | 对应的现有权限 |
|---|---|---|
| XiaoQing 命令（`xiaoqing_command`） | 所有人 | 以发起人身份执行，每条命令照常按自己声明的权限（公开 / 群管理员 / Bot 管理员）和场景（私聊 / 群聊）检查，与本人直接发送是同一处检查 |
| 运行 Python、Shell，读写本机文件 | Bot 管理员 | 跟随 `/shell` 在 plugin.json 里的声明，由 Core 签发（`local_tools`） |
| SSH（`ssh_servers`、`ssh_run`） | Bot 管理员 | 跟随 `/ssh` 的声明，由 Core 签发（`qingssh.agent_command`） |
| Codex（`codex_run`） | Bot 管理员 | 跟随 `/codex` 的声明，由 Core 签发（`codex.agent_task`） |

- 任何人在私聊或群聊里明确叫小青（私聊、`@`、喊名字、引用小青）时，agent 先判断这句话是不是要做事。Bot 管理员身份每一轮按当前 `secrets.admin_user_ids` 判断。
- 本机工具、SSH、Codex 每次调用都向 Core 复核：任务进行中撤掉 Bot 管理员，同一任务里的下一次调用就会被拒绝，能发出的文件也收窄到这次任务的产出。
- 模型只看得到发起人能用的工具和命令。普通成员任务可以使用已授权的 XiaoQing 命令。Python、Shell、文件读写、SSH 和 Codex 需要 Bot 管理员身份。普通成员任务可发送这次命令返回的文件。需要这些能力时，小青会如实说明只有 Bot 管理员能让她做。
- 能用 `/shell`、`/ssh`、`/codex` 的发起人（这几条命令只给 Bot 管理员）全部工具可用。Python 与 Shell 以 Bot 进程的系统账户直接执行（`/shell` 的命令启用列表不适用于 agent），SSH 继承服务器账号权限，Codex 遵循 `plugins.codex` 的沙箱与目录配置。
- 运行中的任务只有发起人和 Bot 管理员能停止或补充要求。别人说“别跑了”“改成蓝色”会被告知这不是他发起的任务。
- 群里只有发起人本人的话算指令。群里其他人的发言、被引用的消息只作为上下文交给模型，并在提示里标明说话人。
- 群聊里的结果发回该群。涉及敏感信息的任务请在私聊里做（`/pendo` 这类命令本身也只能私聊用）。

`/agent` 命令所有人可用，同样按上面的规则执行。

---

## ⚙️ 开启

在 `config/config.json` 中开启，并为 agent 配一条支持工具调用的模型路由：

```json
{
  "plugins": {
    "xiaoqing_agent": {
      "enabled": true,
      "ai": {
        "routes": {
          "agent": {
            "models": ["chatgpt", "deepseek-pro", "deepseek-flash"],
            "temperature": 0.2,
            "timeout_seconds": 180,
            "total_timeout_seconds": 400,
            "max_retry": 1
          },
          "triage": {
            "models": ["chatgpt", "deepseek-flash", "glm-5.2"],
            "temperature": 0.1,
            "timeout_seconds": 15,
            "total_timeout_seconds": 20
          }
        }
      }
    }
  }
}
```

- `enabled` 默认 `false`。关闭时 `xiaoqing_chat` 的行为与没有安装 agent 时完全一致。改完配置后下一条消息即生效。
- 模型链自动跳过缺少密钥的供应商，整条链均不可用时报告配置错误。
- `chatgpt` profile 使用 OpenAI `chat-latest`，执行和分流均优先选择它，密钥配置在 `config/secrets.json` 的 `ai.providers.openai.api_key`。
- `agent` 路由负责执行，必须支持 OpenAI 兼容的 `tools` 调用。执行与交付检查的温度由 `agent.temperature` 配置，发行值为 `0.2`；分流使用 `triage.temperature`，发行值为 `0.1`。单次与总超时、重试均沿用对应路由。思考模式模型（如 DeepSeek 思考版）可直接使用。
- `triage` 路由只做“聊天还是做事”的判断，建议配一条快速、关闭思考的模型。省略时复用 `agent` 路由。
- 删除整个 `plugins/xiaoqing_agent/` 目录同样安全：`xiaoqing_chat` 调不到服务时自动回到普通聊天。

其余字段均可省略：

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_steps` | `40` | 单个任务执行阶段最多调用模型的轮数（1–200）；命令结果交付另有正文生成和范围筛选 |
| `task_timeout_seconds` | `1800` | 单个任务从开始到交付的总时长（30–86400） |
| `tool_timeout_seconds` | `120` | 工具未指定超时时的默认值 |
| `max_tool_timeout_seconds` | `900` | 模型为单次工具调用可以申请的最长时间（5–7200） |
| `inline_wait_seconds` | `12` | 收到任务后当轮最多等待的秒数（0–25）。做完直接回复；超时后继续后台执行，最终结果主动发回 |
| `max_admin_tasks` | `3` | Bot 管理员的任务同时运行数（1–20） |
| `max_member_tasks` | `4` | 其他人的任务合计同时运行数（0–50）。设为 `0` 时只有 Bot 管理员能让 agent 做事 |
| `max_tasks_per_member` | `1` | 其他人每人同时运行的任务数（1–20），同一个人在不同会话里合计 |
| `max_tool_output_chars` | `12000` | 每次工具输出交给模型的字符上限 |
| `history_tasks` | `5` | 判断与执行时附带的本会话最近任务数，用于“把刚才那张图改一下”这类续做 |
| `python` | Bot 自身解释器 | `run_python` 使用的解释器 |
| `shell` | 自动 | `run_shell` 使用的 shell。Windows 优先 Git Bash，没有时用 PowerShell。Linux/macOS 用 bash |

---

## 💬 使用

直接对小青说要做的事：

```text
小青，2 的 100 次方是多少，各位数字加起来呢
帮我画一张 sin 和 cos 在 0 到 2π 的对比图
把刚才那张图的 sin 改成红色虚线
生成一个 1 到 10 平方的 csv 发给我
帮我在待办里加一条：明天下午三点交周报
登上 paris 看一下系统版本和运行了多久
做到哪了？
换成红色（任务还在跑时，补充要求会交给那个任务）
算了，别跑了
```

一轮的处理过程：

1. `xiaoqing_chat` 完成召唤判断后，把这句话、最近聊天、附带图片（含已下载到本地的文件）、被引用消息的文字和图片，以及小青的人设和说话方式交给 agent，同时照常开始生成聊天回复。
2. agent 使用 `triage` 路由判断消息。判断材料包括从命令目录生成的功能清单。清单展示各入口的用途和用法，并标明当前身份的权限限制。实时数据、个人数据、现成功能、能力介绍和功能用法都属于办事请求。快速模型通常约需 1 秒。普通聊天使用已经开始生成的回复。agent 接手时，聊天侧取消生成并释放会话生成名额。进度、停止和运行中补充要求直接处理。新任务和已完成结果的修改都启动新任务。
3. 任务在后台执行。`inline_wait_seconds` 内做完，结果直接作为这一轮回复。超过等待时间后，当轮安静结束，任务继续运行；完成后把最终结果主动发回原会话，群里会先 @ 发起人。判断阶段只产生路由信息，任务直接保留发起人原话。实际执行中有进展或阻碍时，可发送有内容的进度消息；当轮仍在等待时，纯文字进度合并到最终交付。
4. 文字和图片在同一条消息里发送，其他文件通过 QQ 文件上传。agent 接手的这一轮会写进聊天记录：当轮回复和后台发出的结果都记为小青的发言，并带上发出的消息 ID（长回复拆成几条时每条都记下，太长的文字截短记下），群友引用其中任何一条继续说时，小青能认出是在跟她说话。当轮回复发没发出去，聊天侧会回报给任务记录。
5. 停止时如果有多个你能停的任务在跑、又没说是哪个，小青会列出任务请你指明，不会全部停掉。说错的任务 ID 会直接提示找不到。

### 说话方式

聊天和办事共用 `plugins/xiaoqing_chat/config/persona.md`。聊天侧逐轮交接人物事实和〔办事〕各节，并附上小青最近说过的近况和在这个会话里答应过、还没办完的事，直接 `/agent run` 也读取当前配置的人设。agent 的当轮回复和后台结果发出后，聊天侧会从中记下新的承诺（“弄完告诉你”），并在结果送达时把对应的承诺结掉，之后的聊天和办事都知道自己还欠着什么。自然口吻、长文、代码和清单的表达要求统一在人设卡维护，内容按任务需要完整交付。

同一目录中的 `agent.md` 集中维护提示词：判断管理行动判断；执行保留四条通用原则；交付核对从原请求与工具回报生成正文；交付筛选标记超出请求的原文片段，程序执行删除。生成沿用共享人设，筛选保留自然口吻和必要澄清。核对器指出尚未完成的事项时，程序校验其为发起人原话中的连续片段，并最多返回执行阶段补正一轮；失败状态直接交付。交付调用各最多等待20秒，均受剩余任务时间约束。生成或筛选不可用时交付已有工具事实，附件和业务执行次数保持原结果。每个阶段逐轮读取对应小节。`prompts.py` 负责填入环境、权限、上下文和人设；业务参数、默认值和示例由对应工具提供。

帮助支持完整路径和逐级别名，例如 `xiaoqing_commands(query="pendo 记账 add")`、`/help 米家 接口`。完整命令说明和所有示例按整条命令分页，Agent 可以复用已经取得的帮助。任务记录保留最近四条命令回报（每条最多1200字符），执行上下文附上最近两次任务各两条回报，供追问定位记录；内部编号可以保留在这些记录中。修改账目等已有记录时，根据请求和返回值确定差异，使用对应编辑命令；结果已经包含关键字段时直接核对交付。

### 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| agent 任务管理 | `/agent` | 仅此入口 |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
|---|---|
| `/agent run <任务>` | 跳过判断，直接执行（别名 `执行`） |
| `/agent status [任务ID]` | 查看运行中的任务、计划和当前步骤（别名 `状态`） |
| `/agent stop [任务ID\|all]` | 停止你发起的任务（Bot 管理员可停止任何任务）。不带参数时只在恰好一个时停止它，多个需指明 ID 或用 `all`（别名 `停止`、`cancel`） |
| `/agent history` | 本会话最近的任务记录。结果没能发出的任务会标出来（别名 `历史`） |
| `/agent result <任务ID>` | 重新发送本会话某个已结束任务的结果和附件（别名 `结果`） |
| `/agent help` | 显示用法 |

Bot 管理员指定任务 ID 时，`status` 与 `stop` 可以操作其他会话的任务。其他人只能看到自己所在会话的任务。

### 会话与群聊

- 会话按 `g<群号>` 或 `u<QQ号>` 区分。同一个群共享工作目录、任务记录和 Codex 会话。群里的人都能查看运行中的任务和历史，记录、进度和历史都标出发起人，只有发起人和 Bot 管理员能停止或补充。
- 名额分两份：Bot 管理员的任务最多 `max_admin_tasks` 个，其他人的任务合计最多 `max_member_tasks` 个、每人最多 `max_tasks_per_member` 个。两份各算各的，别人的任务再多也挤不掉管理员。满了会直接说明，不排队。

---

## 🧰 工具

| 工具 | 作用 |
|---|---|
| `run_python`（Bot 管理员） | 在工作目录运行一段 Python 脚本（独立进程，matplotlib 使用 Agg 后端） |
| `run_shell`（Bot 管理员） | 在工作目录执行一条 shell 命令 |
| `read_file` / `write_file`（Bot 管理员） | 读写文本文件 |
| `read_image`（Bot 管理员） | 通过现有 vision 路由读取本地图片与文字；校验格式、大小与像素数 |
| `update_plan` | 记录多步任务的计划，`/agent status` 可见 |
| `send_message` | 发送实际进展或阻碍（每任务最多 8 条，间隔至少 15 秒；当轮等待期间纯文字暂缓） |
| `wait` | 等待一段时间，用于轮询（单次最多 600 秒） |
| `final_answer` | 交付结果文字与要发送的图片、文件 |
| `xiaoqing_commands` / `xiaoqing_command` | 查询并以发起人身份执行已发布的 XiaoQing 命令（如 `/pendo`），只列出发起人能用的命令 |
| `ssh_servers` / `ssh_run`（Bot 管理员） | 列出 QingSSH 服务器，用一次性连接执行远端命令 |
| `codex_run`（Bot 管理员） | 把编程任务交给 Codex CLI，等待文字结果与图片产物 |

- 工具表按发起人的权限和当前能力生成：不是 Bot 管理员、或未加载 `qingssh`、`codex` 时，对应工具不出现。
- `xiaoqing_command` 只走命令阶段，执行结果与发起人直接发送同一条命令一致，群管理员命令沿用这一轮消息里的群身份，命令里显示的名字是发起人在原消息里的群名片或昵称。它不能调用 `/agent` 自身，`/xc` 的聊天入口也不接受这种调用。会开启多轮交互会话的命令（猜数字、`/py repl` 等）执行后会被立即收回。仅限私聊的命令（如 `/pendo`）在群里会明确提示需要私聊。
- 命令里写 `[CQ:at,qq=QQ号]` 会转成 @ 段（如给宠物送礼）。命令返回的图片、语音、视频和文件都会给出本地路径，可以原样转发：图片与文字同条，语音、视频各自单独发送，其他文件走文件上传。
- `ssh_run` 每次新建连接、执行完即断开，不占用 `/ssh` 交互会话。Linux、macOS 和 Windows（OpenSSH + Git Bash）主机都可执行。
- `codex_run` 使用名为 `agent-<会话>` 的 Codex 会话，同一会话的后续任务沿用同一个 Codex thread。工作目录受 Codex 的 `allowed_cwd_roots` 约束，结果由 agent 转述，不会再由 Codex 单独发送。
- `ssh_run` 与 `codex_run` 的长时间等待发生在 QingSSH、Codex 插件的执行通道之外：agent 跑一条很久的远端命令时，其他人的 `/ssh`、`/codex` 照常处理。
- `xiaoqing_command` 在一次性的会话空间里执行：命令看不到发起人正在进行的交互（比如 `/ssh` 会话），也不会替换它。命令停在等待回复的步骤时，会话交给插件的 `close_session` 收尾（和用户发“退出”一样回收连接等资源）再丢弃。`/ssh <服务器>` 交互连接不接受 agent 调用，远端命令请用 `ssh_run`。
- 命令带着发送回执时（比如 `/twimg` 取图会先占住选中的图片），回执随附件真正发出时结算：附件放进回复发出就提交，没发出去、只有文字、任务结束或被停下时都释放占用，之后的取图不会被挡住。每次调用都有独立的操作标识，同一任务里两次独立调用不会被插件当成一次重复提交。

---

## ⏱ 运行边界

- 每个子进程拥有独立进程树（Windows 使用 Job Object，POSIX 使用进程组）。超时、输出超过 2MB、停止任务或进程关闭时，整棵进程树一起回收。命令留下的后台进程同样会被回收。
- 任务受 `max_steps` 与 `task_timeout_seconds` 约束。用完时模型在不调用工具的前提下总结已有结果并说明还差什么。
- 运行中收到补充要求时，模型这一批里还没开始的工具调用不再执行，下一步按新要求重新安排。
- 上下文过长时（正文、推理内容和工具调用参数一起计算），较早的工具输出、调用参数和推理内容压缩成开头预览，最近的保持原文。执行记录里保存的仍是完整往返。
- `read_file` 按段读取，只把请求的那一段读进内存。
- `xiaoqing_commands` 支持完整路径、逐级别名、空格分隔关键词和 `page` 翻页；按输出预算和每页最多 40 条分组，每条命令保留完整说明和示例。
- 图片识别使用现有 vision 路由；识别失败时请求补充文字。安装或升级依赖需要用户明确提出。
- 判断阶段的模型调用失败或 agent 内部异常时，这一轮回到普通聊天，不影响小青原本的回复。任务开始后模型调用失败，任务记为失败，并告诉发起人没能做完。
- 结果发送失败时（后台发送、当轮回复、`/agent run` 直接给出的结果、附件上传都算），任务仍记为完成，交付状态记为失败，`/agent history` 中会标出，可用 `/agent result <任务ID>` 补发。补发同样记录送达与否。任务结果原样保存，补发的内容和当时完全一致。插件重载或进程关闭时，正在执行的任务会停下。已经做完、正在发送结果的任务有一小段时间发完。

---

## 🧩 模块结构

```text
xiaoqing_chat ──agent.handle_turn（decide / act）──► main.py ── triage.py（判断）
      ▲                                                  │
      └──────────── agent.report_delivery ◄──────────────┤
                                                         ▼
                                    runtime.py（名额、登记、当轮或后台交付、停止、关闭）
                                                         │
                                       loop.py（模型 ↔ 工具，补充要求，预算，上下文压缩）
                                                         │
                                       tools.py ─┬─ command_runner ─► Core 以发起人身份执行命令
                                                 ├─ local_tools ────► shells.py → core.process_tree
                                                 ├─ qingssh.agent_command ─► QingSSH
                                                 └─ codex.agent_task ──────► Codex
                                                         │
                         delivery.py（消息与附件）· receipts.py（命令回执结算）· tasks.py（记录）
```

| 模块 | 职责 |
|---|---|
| `main.py` | 插件入口：`agent.handle_turn`（判断与执行分两步，供聊天侧和生成并行）、`agent.report_delivery`、`/agent` 命令 |
| `triage.py` | 用 `triage` 路由判断这句话是闲聊、新任务、问进度、停止还是补充要求 |
| `prompts.py` | 读取共享配置目录中的判断、执行与交付规则，注入当前人设、环境与上下文；按发起人权限提供命令清单 |
| `runtime.py` | 任务登记与名额（管理员、其他人分开）、后台执行、当轮或主动交付、交付回报与补发、停止和关闭收尾 |
| `loop.py` | 执行循环：模型选工具、工具真实执行，运行中补充要求、步数与时长预算、上下文压缩、完整执行记录 |
| `tools.py` | 工具定义与执行。工具表按发起人能力生成，本机工具每次调用前向 Core 复核 |
| `shells.py` | 选出跑命令用的 shell（按配置，或 Windows 优先 Git Bash、退回 PowerShell）。命令的执行、进程树回收和输出预算用 Core 的 `core.process_tree`，和 `/shell` 是同一套 |
| `delivery.py` | 结果变成 QQ 消息：文字与图片同条，语音、视频单独发送，其他文件上传，逐项返回发送结果 |
| `receipts.py` | 命令带回的发送回执：随附件实际发出时结算，没发出或任务结束时释放 |
| `tasks.py` | 任务与会话模型、发起人身份、按会话保存的任务记录和执行记录 |
| `config.py` | 读取 `config.plugins.xiaoqing_agent`，越界或类型错误的字段回退默认值 |

聊天侧的交接在 `plugins/xiaoqing_chat/agent_bridge.py`。Core 侧的能力签发在 `core/app_plugin_context.py`，命令执行器在 `core/dispatcher.py` 的 `run_command_text`。

---

## 📁 数据

```text
data/xiaoqing_agent/
├── workspaces/<会话>/          # 每个会话一个工作目录，文件长期保留，后续任务可继续使用
│   ├── out/<任务ID>/           # 每个任务的产出
│   └── .agent/scripts/         # run_python 实际执行的脚本
├── tasks/<会话>.json           # 每个会话最近 30 条任务记录（含发起人与交付状态）
└── transcripts/<任务ID>.json   # 每个任务完整的模型与工具往返（未压缩），保留最近 200 个
```

进程重启时，上一代没有收尾的任务记为 `interrupted`。工作目录用于组织文件。Bot 管理员任务里的 Python、Shell 能访问 Bot 系统账户可访问的任何文件。

---

## 🧪 测试

```bash
python -m pytest tests/plugins/xiaoqing_agent tests/core/test_app_agent_capabilities.py \
  tests/core/test_process_tree.py tests/plugins/xiaoqing_chat/test_xiaoqing_chat_agent_bridge.py \
  tests/plugins/codex/test_codex_agent_task.py tests/plugins/qingssh/test_qingssh_agent_command.py
```

子进程的超时、取消和进程树回收用真实进程测试（`tests/core/test_process_tree.py`，Windows 上使用 Proactor 事件循环）。

命令通过 `DeliverySegments.command_outcome` 返回 completed、accepted、error 或 incomplete。`result_text` 提供独立于界面导航的结果正文，`next_command` 提供下一页。`xiaoqing_command` 的 `delivery=reply` 交付本次结果，`all_pages` 沿明确导航取齐后交付，`continue` 留给原请求尚需的其他独立操作；同一批调用处理完后收尾。明确返回 error 的收尾操作交付失败状态，参数错误仍可依据用法修正。命令附件自动随结果发送；`final_answer` 省略 files 时沿用本次命令附件，也可显式选择附件或使用空数组交付纯文字。

后台 accepted 回报交付接收状态，完成通知归原插件负责。同一服务连续返回相同内容、连续错误、重复分页或预算耗尽会停止当前路径。不同结果的分页与其他独立任务可以继续，发起人补充要求后重新计数。交付筛选只接受候选中确实存在的删除片段，全部结果和筛选过程保存在任务记录中。

`pendo_ledger_add` 与 `/pendo ledger record <JSON>` 共享 Pendo 的结构化字段模型。执行器传入发起人原话，在落库前校验分类和账户来源；分类和账户省略时采用其他、现金。`category_from`、`account_from` 可以引用本人已有账目，沿用已核实的字段。传统 add/quick 经 Agent 调用时也进入相同校验入口。最终反馈读取实际保存的金额、描述、分类、账户与日期。

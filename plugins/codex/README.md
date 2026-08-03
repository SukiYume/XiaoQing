# Codex 插件

通过 QQ 命令调用本机 Codex CLI 的后台任务插件。`plugin.json` 将全部 `/codex` 命令标记为 `admin_only: true` 且仅允许私聊，因此只有 `config/secrets.json` 中 `admin_user_ids` 列出的 Bot 管理员能在私聊中使用；资源预算不会削弱可信管理员选择 Codex sandbox、审批策略和工作目录的灵活性。

Codex 插件不使用 XiaoQing 的框架 Session。它维护自己的 Codex 会话标签、工作目录、任务队列和对话记录：同一标签内任务串行执行，不同标签可并行执行；任务完成后主动向 QQ 回发文字和图片结果。

## 常用命令

```text
/codex create <name> [cwd:<path>]
/codex <name> <任务>
/codex list
/codex status [name]
/codex cancel <name> [job_id]
/codex stop <name> [job_id]
/codex clear <name>
/codex delete <name> [--force] [--protected]
```

<!-- manifest-command-aliases:start -->
| 推荐入口 | manifest 等价别名 |
|---|---|
| `/codex` | 无；所有子命令共享这一个管理员入口。 |
<!-- manifest-command-aliases:end -->

`create/new/创建`、`list/ls/列表`、`status/状态`、`cancel/stop/取消/停止`、`clear/清空` 和 `delete/del/remove/rm/删除` 是 `/codex` 内部的等价子命令，不是独立 manifest 命令。未知选项、多余参数、带值的 `--force/--protected`、非 ASCII 或非正数任务编号都会被拒绝。

## 使用示例

```text
/codex create main
/codex create repo cwd:C:/workspace/project
/codex main 总结一下当前项目结构
/codex repo 跑一下测试并说明失败点
/codex status repo
/codex cancel repo
/codex delete repo --force
```

`cancel` 和 `stop` 是同一个操作：移除排队任务，或终止正在运行的 Codex CLI 子进程。

## arXiv 摘要会话

arXiv Filter 插件会把每天筛选出的所有 positive 论文链接交给固定 Codex 会话 `astro-ph`。日期必须是实际存在的 `YYYY-MM-DD`，单次最多接受 512 个 `arxiv.org/abs` 或 `arxiv.org/pdf` 链接；版本号、PDF 后缀和查询参数会被规范化为无版本 HTTPS abs 链接，其他站点或任意文本不会进入 prompt。该会话默认使用 Codex 插件数据目录下的 `workspaces/`，并假设工作目录下存在 `arxiv-summary-methodology.md`。如果运行时发现 `astro-ph` 还没有 Codex thread，插件会先发送一条静默初始化消息建立摘要规则；初始化结果只写入会话历史，不推送到 QQ。随后当天摘要任务按普通 Codex 队列任务投递。每次发送总结任务时，prompt 都会明确要求 Codex 先读取当前工作目录下的 `arxiv-summary-methodology.md`，并附上形如：

```markdown
## 2026-05-19
https://arxiv.org/abs/2605.16917
https://arxiv.org/abs/2605.18050
```

同一天的 arXiv 摘要请求会先查 `astro-ph` 的会话历史：

- 如果已有成功执行结果，直接重发历史总结。
- 如果已有任务正在队列或运行中，只发送状态提示，不重复排队。
- 如果之前失败或没有成功记录，则重新投递 Codex 总结。
- 如果 Codex 执行失败，插件会发送包含日期的失败消息。

`astro-ph` 默认是受保护会话，普通删除会被拒绝。确需删除时必须使用：

```text
/codex delete astro-ph --force --protected
```

删除会话时不会丢弃历史目录；插件会把 `data/session/<label>` 移到 `data/deleted_sessions/<label>-YYYYMMDD-HHMMSS`。因此同名会话重新创建后会使用新的空白历史，不会继续读取已归档的旧总结。

这部分业务逻辑独立放在 `arxiv_summary.py` 中；`manager.py` 只提供通用会话、队列、历史归档和结果发送能力。arXiv 失败提示通过任务 metadata 提供标题，不在主队列里写 arXiv 专用分支。

## 配置

在 `config/config.json` 中配置：

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
      "max_prompt_chars": 200000,
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

如果 Codex CLI 不在 PATH 中，可在 `plugins.codex.codex_bin` 指定可执行文件路径。`allowed_cwd_roots` 是工作目录安全边界，管理员指定的 `cwd:` 必须位于这些根目录下；默认目录也会先通过该边界校验，之后才允许创建。`sandbox` 只接受 `read-only`、`workspace-write`、`danger-full-access`，`approval_policy` 只接受 `untrusted`、`on-failure`、`on-request`、`never`。`spawn_timeout_seconds` 限制创建 CLI 进程的等待时间；进程登记与 prompt 提交之间受取消 handoff 保护。

安全边界说明：该插件只允许 Bot 管理员在私聊中触发，但它仍会以 Bot 进程身份启动本机 CLI。`danger-full-access` 代表 CLI 不受 Codex sandbox 的文件系统限制；`approval_policy: never` 不会增加人工确认；`skip_git_repo_check` 也只是仓库检查开关。`codex_bin` 可指定任意可执行文件，因此其权限等价于把该可执行文件交给 Bot 进程运行。只有受信任的管理员配置才能修改这些字段；不要把 Codex 入口开放到群聊或不受信任的配置写入路径。结果回发只暴露文件名，完整归档路径仅留在本机记录。

### 强制资源预算

下列字段保护 Bot 进程、磁盘和 QQ 投递链路。括号内是 `config.py` 接受的范围；JSON 整数超出范围时会被钳制到最近边界，布尔、浮点、数字字符串等错误类型不会再被隐式转换，而会回退到默认值。这些预算是管理员任务的存活性护栏，不是 Codex 能力 allowlist。

| 字段 | 默认值 | 合法范围 | 达到上限后的行为 |
|---|---:|---:|---|
| `max_parallel_jobs` | `2` | 1-64 | 全局并行许可达到上限后等待；热缩容不会抢占已运行任务，未启动任务按新上限重排。 |
| `per_session_queue_limit` | `10` | 1-1,000 | 单个会话的非内部排队任务达到上限后拒绝继续入队，不再只发软警告。 |
| `emergency_queue_limit` | `1000` | 10-10,000，且不低于会话上限 | 进程级紧急队列保护触发后拒绝继续入队。 |
| `max_prompt_chars` | `200000` | 1,000-1,000,000 | 用户入口和内部 sidecar 共用该入队边界，超长任务不会写入队列或历史。 |
| `spawn_timeout_seconds` | `30` 秒 | 1-120 | CLI 创建超时后任务失败并清理已拥有的临时资源。 |
| `job_timeout_seconds` | `3600` 秒 | 30-604,800 | 达到执行时限后终止整棵进程树并返回超时结果。 |
| `session_ttl_days` | `90` 天 | 0-3,650 | 归档过期、空闲、非受保护且无排队/运行任务的会话；`0` 关闭。 |
| `artifact_retention_days` | `30` 天 | 0-3,650 | 回收已结束任务目录、临时输出、隔离状态和删除归档；`0` 关闭。 |
| `emergency_disk_bytes` | 10 GiB | 64 MiB-1 TiB | 数据目录达到阈值后拒绝新任务。 |
| `max_stdout_bytes` | 16 MiB（`16777216`） | 64 KiB-128 MiB | Codex JSON stdout 累计超过上限时，任务标记为输出超限并终止整棵进程树。 |
| `max_stderr_bytes` | 4 MiB（`4194304`） | 64 KiB-64 MiB | stderr 累计超过上限时，任务标记为输出超限并终止整棵进程树。 |
| `max_json_line_bytes` | 1 MiB（`1048576`） | 16 KiB-8 MiB | 单条 stdout JSON 事件超过上限时立即终止任务，避免无界行缓冲。 |
| `max_final_output_bytes` | 8 MiB（`8388608`） | 64 KiB-64 MiB | Codex 最终输出文件超过上限时立即终止任务；只归档有界的头尾截断副本，不把完整超大文件读入内存。 |
| `max_qq_text_chars` | `60000` 字符 | 2,000-200,000 | 完整结果先写入该任务的受控归档；QQ 只发送截断文本和归档位置，不丢失可审计原文。 |
| `artifact_scan_max_entries` | `5000` 项 | 10-20,000 | 制品目录扫描达到条目数上限后停止继续遍历，未扫描项不会进入收集或发送流程。 |
| `artifact_scan_max_depth` | `8` 层 | 1-16 | 超过目录深度的条目不再扫描，防止异常目录树拖垮任务收尾。 |
| `max_image_artifacts` | `20` 张 | 1-100 | 超出数量的图片候选被拒绝，不复制到会话图片归档。 |
| `max_image_bytes` | 20 MiB（`20971520`） | 64 KiB-100 MiB | 超过单文件字节上限的图片被拒绝。 |
| `max_image_total_bytes` | 100 MiB（`104857600`） | 64 KiB-512 MiB | 已接受图片的累计字节达到上限后，后续图片被拒绝。 |
| `max_image_pixels` | `40000000` 像素 | 1,024-100,000,000 | 真实解码后的总像素数超限，或无法通过图片签名/解码校验时，该产物被拒绝。 |
| `max_image_frames` | `120` 帧 | 1-500 | GIF/WebP 等多帧图片的帧数超限时，该产物被拒绝。 |
| `max_qq_images` | `10` 张 | 1-20 | 每个任务最多向 QQ 发送此前已接受并归档的前 N 张图片；其余图片不造成消息洪泛。 |

输出流和最终输出的四项字节预算是硬限制，触发后会终止任务；最终输出文件超限时仅保留有界头尾归档，QQ 文本字符超限时则采用“完整归档、截断投递”；图片扫描、数量、字节、签名/解码、像素与帧数预算采用“拒绝不合格产物”，并把拒绝原因写入任务记录。`max_qq_images` 只限制发送数量，不把已通过校验的会话归档删除。

`sessions.json` 使用版本化字段白名单；截断、未知 schema、坏字段或已越出 `allowed_cwd_roots` 的记录会保留到 `quarantine/` 后以安全状态继续启动。`session_ttl_days` 自动归档超过保留期的空闲非受保护会话；`artifact_retention_days` 回收已结束的 job 目录、输出、隔离快照和删除归档。运行中或仍排队的 job、受保护会话不会被维护任务删除。设为 `0` 可分别关闭对应 TTL 回收。

## 路径格式

QQ 消息里建议统一使用 `/` 斜杠输入路径：

- Windows 可输入 `C:/workspace/project`。
- Linux/macOS 仍输入 `/home/user/project`。
- 相对路径不接受，工作目录必须是绝对路径。
- 非 Windows 系统会拒绝 Windows 盘符路径。

## 运行时数据

运行时数据保存在 `context.data_dir`（默认 `data/codex/`），不应提交到 Git：

- `sessions.json`：版本化的 Codex 会话标签、工作目录、owner 和 thread id 状态。
- `quarantine/sessions-*.json`：无法直接信任的状态快照；供排障，不参与运行时加载，并按制品保留期清理。
- `session/<name>/conversation.jsonl`：每个标签的用户任务、Codex 回复、取消、删除事件和图片记录。
- `session/<name>/images/`：该 Codex 会话已经透传到 QQ 的图片副本。
- `session/<name>/jobs/job-0001/artifacts/`：单次任务的图片输出目录；插件会自动把这个目录写入 Codex prompt。
- `deleted_sessions/<name>-YYYYMMDD-HHMMSS/`：删除会话时归档的旧历史目录。
- `outputs/`：Codex CLI 临时输出目录。

## 图片结果

插件会在每次 Codex 任务的默认 prompt 后自动追加图片输出约定。Codex 如果生成图片，应保存到当前任务的 `artifacts/` 目录，并在最终回复中用 Markdown 图片语法或 `图片: <path>` 标出。用户不需要在 QQ 命令里手写这段要求。

结果回发时，插件只解析最终文本中的显式本地图片路径，并扫描本任务专属的 `artifacts/` 目录；不会猜测或遍历全局 `$CODEX_HOME/generated_images/`。如果 imagegen 先把文件保存到全局目录，Codex 必须按自动附加的约定把它复制到本任务目录或在最终回复中显式引用。候选图片通过目录归属、非链接/非硬链接、文件身份稳定、字节、格式、真实解码、像素和帧数检查后，才复制到 `session/<name>/images/` 并通过 QQ image 消息段发送。长文本结果会先按 XiaoQing 的消息长度限制拆分，再发送图片，避免混合消息超长。

## 注意事项

- 插件只负责命令队列、路径校验和进程管理，不绕过 Codex CLI 自身的 sandbox、审批策略和系统权限。
- 同一个 Codex 标签内任务串行运行，避免多个任务同时 resume 同一个 thread。
- 结果在 `max_qq_text_chars` 内仍使用 XiaoQing 的统一消息分割逻辑；超过该预算时插件保留完整归档，只向 QQ 发送截断内容和归档位置。

# Codex 插件

通过 QQ 命令调用本机 Codex CLI 的后台任务插件。`plugin.json` 将全部 `/codex` 命令标记为 `admin_only: true`，因此只有 `config/secrets.json` 中 `admin_user_ids` 列出的 Bot 管理员可以使用；资源预算不会削弱可信管理员选择 Codex sandbox、审批策略和工作目录的灵活性。

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

## 使用示例

```text
/codex create main
/codex create repo cwd:C:/Users/testuser/Desktop/project
/codex main 总结一下当前项目结构
/codex repo 跑一下测试并说明失败点
/codex status repo
/codex cancel repo
/codex delete repo --force
```

`cancel` 和 `stop` 是同一个操作：移除排队任务，或终止正在运行的 Codex CLI 子进程。

## arXiv 摘要会话

arXiv Filter 插件会把每天筛选出的所有 positive 论文链接交给固定 Codex 会话 `astro-ph`。该会话默认工作目录为 `C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex`，并假设工作目录下存在 `arxiv-summary-methodology.md`。如果运行时发现 `astro-ph` 还没有 Codex thread，插件会先发送一条静默初始化消息建立摘要规则；初始化结果只写入会话历史，不推送到 QQ。随后当天摘要任务按普通 Codex 队列任务投递。每次发送总结任务时，prompt 都会明确要求 Codex 先读取当前工作目录下的 `arxiv-summary-methodology.md`，并附上形如：

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

如果 Codex CLI 不在 PATH 中，可在 `plugins.codex.codex_bin` 指定可执行文件路径。`allowed_cwd_roots` 是工作目录安全边界，管理员指定的 `cwd:` 必须位于这些根目录下。`spawn_timeout_seconds` 限制创建 CLI 进程的等待时间；进程登记与 prompt 提交之间受取消 handoff 保护。

### 强制资源预算

下列字段保护 Bot 进程、磁盘和 QQ 投递链路。括号内是 `config.py` 接受的范围；超出范围的配置值会被钳制到最近边界。这些预算是管理员任务的存活性护栏，不是 Codex 能力 allowlist。

| 字段 | 默认值 | 合法范围 | 达到上限后的行为 |
|---|---:|---:|---|
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

## 路径格式

QQ 消息里建议统一使用 `/` 斜杠输入路径：

- Windows 可输入 `C:/Users/testuser/Desktop/project`。
- Linux/macOS 仍输入 `/home/user/project`。
- 相对路径不接受，工作目录必须是绝对路径。
- 非 Windows 系统会拒绝 Windows 盘符路径。

## 运行时数据

运行时数据保存在 `plugins/codex/data/`，不应提交到 Git：

- `sessions.json`：Codex 会话标签、工作目录、owner 和 thread id。
- `session/<name>/conversation.jsonl`：每个标签的用户任务、Codex 回复、取消、删除事件和图片记录。
- `session/<name>/images/`：该 Codex 会话已经透传到 QQ 的图片副本。
- `session/<name>/jobs/job-0001/artifacts/`：单次任务的图片输出目录；插件会自动把这个目录写入 Codex prompt。
- `deleted_sessions/<name>-YYYYMMDD-HHMMSS/`：删除会话时归档的旧历史目录。
- `outputs/`：Codex CLI 临时输出目录。

## 图片结果

插件会在每次 Codex 任务的默认 prompt 后自动追加图片输出约定。Codex 如果生成图片，应保存到当前任务的 `artifacts/` 目录，并在最终回复中用 Markdown 图片语法或 `图片: <path>` 标出。用户不需要在 QQ 命令里手写这段要求。

结果回发时，插件会解析最终文本、扫描 `artifacts/` 目录，并兜底扫描 `$CODEX_HOME/generated_images/` 中该任务运行期间生成的图片，把本地图片复制到 `session/<name>/images/`，再通过 QQ image 消息段和文字一起发送。长文本结果会先按 XiaoQing 的消息长度限制拆分，再发送图片，避免混合消息超长。

## 注意事项

- 插件只负责命令队列、路径校验和进程管理，不绕过 Codex CLI 自身的 sandbox、审批策略和系统权限。
- 同一个 Codex 标签内任务串行运行，避免多个任务同时 resume 同一个 thread。
- 结果在 `max_qq_text_chars` 内仍使用 XiaoQing 的统一消息分割逻辑；超过该预算时插件保留完整归档，只向 QQ 发送截断内容和归档位置。

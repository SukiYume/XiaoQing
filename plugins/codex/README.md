# 🤖 Codex

`codex` 通过 QQ 私聊管理本机 Codex CLI 后台任务。每个会话标签保存独立的工作目录、Codex thread、任务队列和对话记录；同一标签串行执行，不同标签可并行执行。

---

## 🔐 权限与运行边界

Manifest 将 `/codex` 标记为 `admin_only: true`，并将上下文限定为私聊。可用用户来自 `config/secrets.json` 的 `admin_user_ids`。

Codex CLI 以 Bot 进程身份启动，文件系统权限由 Bot 账户、`sandbox`、`approval_policy` 和工作目录共同决定。`danger-full-access` 会授予 CLI Bot 账户可达的文件系统权限。生产配置建议采用受控的 `allowed_cwd_roots`、可信管理员列表和专用工作目录。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| Codex 会话队列 | `/codex` | 仅此入口 |
<!-- manifest-command-aliases:end -->

| 用法 | 说明 |
| --- | --- |
| `/codex create <name> [cwd:<path>]` | 创建会话 |
| `/codex <name> <任务>` | 向会话提交任务 |
| `/codex list` | 列出会话 |
| `/codex status [name]` | 查看全部或指定会话状态 |
| `/codex cancel <name> [job_id]` | 取消运行任务或移除排队任务 |
| `/codex clear <name>` | 清空指定会话的排队任务 |
| `/codex delete <name> [--force] [--protected]` | 删除会话并归档历史 |
| `/codex help` | 显示本地帮助 |

子命令别名：

- `create`：`new`、`创建`；
- `list`：`ls`、`列表`；
- `status`：`状态`；
- `cancel`：`stop`、`取消`、`停止`；
- `clear`：`清空`；
- `delete`：`del`、`remove`、`rm`、`删除`；
- `help`：`帮助`、`?`。

Windows 路径建议使用 `/`，例如 `C:/workspace/project`。Linux 和 macOS 使用对应的绝对路径。

---

## ⚙️ 基础配置

在 `config/config.json` 中配置：

```json
{
  "plugins": {
    "codex": {
      "codex_bin": "codex",
      "default_cwd": "C:/workspace",
      "allowed_cwd_roots": ["C:/workspace"],
      "sandbox": "workspace-write",
      "approval_policy": "never",
      "skip_git_repo_check": true,
      "max_parallel_jobs": 2,
      "per_session_queue_limit": 10,
      "job_timeout_seconds": 3600,
      "protected_sessions": ["astro-ph"],
      "arxiv_summary": {
        "label": "astro-ph",
        "cwd": "C:/workspace/arxiv",
        "methodology": "arxiv-summary-methodology.md"
      }
    }
  }
}
```

省略 `default_cwd` 时，插件使用 `data/codex/workspaces/`。省略 `allowed_cwd_roots` 时，该列表包含默认工作目录。`sandbox` 接受 `read-only`、`workspace-write`、`danger-full-access`；`approval_policy` 接受 `untrusted`、`on-failure`、`on-request`、`never`。

---

## 📌 资源预算

配置读取器会校验字段类型，并将整数限制在代码定义的安全范围内。主要默认值如下：

| 类别 | 字段与默认值 | 作用 |
| --- | --- | --- |
| 队列 | `max_parallel_jobs=2`、`per_session_queue_limit=10`、`emergency_queue_limit=1000` | 控制全局并行和会话队列规模 |
| 提示 | `max_prompt_chars=200000` | 限制单个用户任务与内部任务的提示长度 |
| 进程 | `spawn_timeout_seconds=30`、`job_timeout_seconds=3600` | 控制 CLI 创建和任务执行时限 |
| 输出 | `max_stdout_bytes=16777216`、`max_stderr_bytes=4194304`、`max_json_line_bytes=1048576` | 限制进程输出流和单条 JSON 事件 |
| 结果 | `max_final_output_bytes=8388608`、`max_qq_text_chars=60000` | 控制最终结果归档和 QQ 文本预览 |
| 扫描 | `artifact_scan_max_entries=5000`、`artifact_scan_max_depth=8` | 控制作业制品目录遍历 |
| 图片 | `max_image_artifacts=20`、`max_image_bytes=20971520`、`max_image_total_bytes=104857600` | 控制候选图片数量和字节规模 |
| 解码 | `max_image_pixels=40000000`、`max_image_frames=120`、`max_qq_images=10` | 控制图片解码复杂度和 QQ 投递数量 |
| 保留 | `session_ttl_days=90`、`artifact_retention_days=30` | 归档过期会话并回收任务制品 |
| 磁盘 | `emergency_disk_bytes=10737418240` | 为数据目录设置紧急容量阈值 |

输出流或最终输出达到硬预算时，插件终止任务并保存有界记录。QQ 文本达到预览预算时，完整结果进入本地归档，QQ 收到截取内容和归档位置。图片需要通过路径归属、文件身份、格式、解码、字节、像素与帧数检查。

---

## 💬 arXiv 摘要会话

`arxiv_filter` 通过 `codex.enqueue_arxiv_summary` 服务把当天 positive 论文交给固定会话，默认标签为 `astro-ph`。输入日期采用有效的 `YYYY-MM-DD`，单次最多包含 512 个 `arxiv.org/abs` 或 `arxiv.org/pdf` 链接。

插件会把版本号、PDF 后缀和查询参数统一为 HTTPS abs 链接，并以“源列表日期 + 规范化链接集合”作为任务身份：

- 已有成功结果：重发历史总结；
- 已有排队或运行任务：发送当前状态；
- 同一日期出现新链接集合：创建新任务；
- 已有失败记录：再次创建总结任务。

首次使用会话时，插件先提交一条内部初始化任务建立摘要规则，再提交当天总结。每次总结任务都要求 Codex 读取 `arxiv_summary.methodology` 指向的方法文件。`astro-ph` 自动加入受保护会话列表；删除该会话使用：

```text
/codex delete astro-ph --force --protected
```

---

## 💾 运行时数据

数据根目录默认为 `data/codex/`：

| 路径 | 内容 |
| --- | --- |
| `sessions.json` | 会话标签、工作目录、所有者、thread ID 和任务计数 |
| `quarantine/sessions-*.json` | 校验异常的状态快照 |
| `session/<name>/conversation.jsonl` | 用户任务、Codex 回复和任务状态事件 |
| `session/<name>/images/` | 已归档并投递的图片 |
| `session/<name>/jobs/job-<id>/artifacts/` | 单次任务的制品目录 |
| `deleted_sessions/<name>-<time>/` | 删除会话后的历史归档 |
| `outputs/` | Codex CLI 临时输出 |

状态文件采用版本化字段白名单。加载时依次验证主文件与 `.bak` 的语法、结构、字段和路径；主文件无效且备份有效时恢复备份。主副本均不可用的快照进入 `quarantine/`，运行时以安全状态继续启动。删除会话会移动历史目录；重建同名会话会创建新的记录链。

---

## 🎨 图片结果

插件会在任务提示末尾附加图片输出约定。Codex 可将图片保存到当前任务的 `artifacts/`，或在最终文本中使用 Markdown 图片语法和 `图片: <path>` 显式引用本地文件。

结果投递过程只处理当前任务制品与最终文本中的显式路径。校验通过的图片复制到会话图片目录，然后作为 OneBot 图片段发送。

---

## ⏰ 生命周期

插件在首次命令或 arXiv 服务调用时创建进程内 manager。配置代次变化会更新并行度和资源预算。卸载、重载或 Bot 关闭时，`shutdown()` 停止接收任务、取消运行进程树、等待 worker 收尾并保存状态。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 创建会话失败 | 核对绝对路径、`allowed_cwd_roots` 和 Bot 账户目录权限 |
| CLI 创建失败 | 核对 `codex_bin`、PATH 与 `spawn_timeout_seconds` |
| 任务持续排队 | 核对同标签运行任务、并行度和队列上限 |
| 任务超时或输出超限 | 查看任务状态、运行日志和对应资源预算 |
| 图片仅出现在文本中 | 核对制品目录、显式路径、文件格式和图片预算 |
| arXiv 方法文件读取失败 | 核对 `arxiv_summary.cwd` 与 `methodology` |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/codex
python -m ruff check plugins/codex
python -m mypy plugins/codex
```

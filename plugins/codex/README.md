# Codex 插件

通过 QQ 命令调用本机 Codex CLI 的后台任务插件。是否仅管理员可用由 `plugin.json` 中的 `admin_only` 配置决定。

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
/codex delete <name> [--force]
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

## 配置

在 `config/config.json` 中配置：

```json
{
  "plugins": {
    "codex": {
      "default_cwd": "C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex",
      "allowed_cwd_roots": ["C:/Users/testuser/Desktop/XiaoQing/XiaoQing_Codex"],
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

如果 Codex CLI 不在 PATH 中，可在 `plugins.codex.codex_bin` 指定可执行文件路径。`allowed_cwd_roots` 是工作目录安全边界，用户指定的 `cwd:` 必须位于这些根目录下。

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
- `outputs/`：Codex CLI 临时输出目录。

## 图片结果

插件会在每次 Codex 任务的默认 prompt 后自动追加图片输出约定。Codex 如果生成图片，应保存到当前任务的 `artifacts/` 目录，并在最终回复中用 Markdown 图片语法或 `图片: <path>` 标出。用户不需要在 QQ 命令里手写这段要求。

结果回发时，插件会解析最终文本、扫描 `artifacts/` 目录，并兜底扫描 `$CODEX_HOME/generated_images/` 中该任务运行期间生成的图片，把本地图片复制到 `session/<name>/images/`，再通过 QQ image 消息段和文字一起发送。长文本结果会先按 XiaoQing 的消息长度限制拆分，再发送图片，避免混合消息超长。

## 注意事项

- 插件只负责命令队列、路径校验和进程管理，不绕过 Codex CLI 自身的 sandbox、审批策略和系统权限。
- 同一个 Codex 标签内任务串行运行，避免多个任务同时 resume 同一个 thread。
- 长结果不在插件内截断，发送阶段由 XiaoQing 的统一消息分割逻辑处理。

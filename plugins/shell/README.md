# Shell 插件

受控的终端命令执行插件，仅管理员可用。

## 常用命令

```text
/shell <命令>
/shell help
/shell list
```

常用示例：

```text
/shell python --version
/shell git status --short
/shell cp C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
/shell cmd /c copy C:/Users/testuser/Desktop/a.txt C:/Users/testuser/Desktop/b.txt
```

## 配置

在 `config/secrets.json` 中配置：

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

### 配置项说明

- `whitelist`：自定义允许执行的命令
- `whitelist_mode`：`replace` 或 `extend`
- `timeout`：执行超时（秒）
- `disable_whitelist`：是否关闭白名单限制，危险模式

## 安全行为

- 默认只允许白名单命令。
- 命令链接符（如 `&&`、`||`、`;`、`|`）默认受限。
- 输出会截断，避免长结果刷屏。
- 命令超时后会终止整棵子进程树，而不只是直接子进程。
- 插件直接启动外部命令，不经过系统 shell。

## 路径格式

QQ 消息里建议统一使用 `/` 斜杠输入路径，插件会按 bot 所在系统归一化：

- Windows 可输入 `C:/Users/testuser/Desktop/a.txt`。
- Linux/macOS 仍输入 `/home/user/a.txt`、`~/a.txt`、`./file` 或 `../file`。
- `key=value` 中的 value 如果像路径，也会被归一化。
- URL 不会被当作路径改写。
- Windows 选项如 `cmd /c`、`xcopy /Y` 不会被误判成路径。

Windows 的 `copy`、`del`、`type` 等是 shell 内建命令，不能直接 `/shell copy ...`。复制文件优先使用 `cp`、`xcopy`、`robocopy`，或显式执行 `cmd /c copy <src> <dst>`。

## 注意事项

- 仅适合受控的运维与诊断场景。
- 即使关闭白名单，危险模式下也仍有基础黑名单保护；不建议在生产环境长期启用。

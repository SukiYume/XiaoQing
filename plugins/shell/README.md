# Shell 插件

受控的终端命令执行插件，仅管理员可用。

## 常用命令

```text
/shell <命令>
/shell help
/shell list
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

## 注意事项

- 仅适合受控的运维与诊断场景。
- 即使关闭白名单，危险模式下也仍有基础黑名单保护；不建议在生产环境长期启用。

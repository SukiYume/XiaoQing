# Shell 插件

仅供 Bot 管理员在私聊中于 Bot 所在主机执行单条本地命令。插件默认直接创建外部进程；也可由公开配置显式选择
Git Bash 作为终端。每次调用彼此独立，不保存工作目录或环境状态。

管理员身份、manifest 私聊场景和入站认证共同构成权限边界。命令启用列表、受限模式、超时和输出预算用于减少误触与资源失控，
**不是安全沙箱**；`python`、`cmd`、`powershell` 等解释器仍具有管理员授予的完整本机能力。

## 命令

```text
/shell <命令>
/shell help
/shell list
```

清单注册的三个等价入口为 `/shell`、`/sh` 和 `/exec`，均受同一 `admin_only` 与 `contexts: ["private"]` 约束。
`/shell list` 会按当前终端分别显示可执行和未找到的入口。启用只代表允许尝试，不负责安装程序，也不会替部署者修改 PATH。

Windows 常用示例：

```text
/shell python --version
/shell git status --short
/shell cmd /c dir
/shell cmd /c cd
/shell cmd /c copy C:/workspace/a.txt C:/workspace/b.txt
```

Linux/macOS 常用示例：

```text
/shell ls -la
/shell pwd
/shell cp /srv/a.txt /srv/b.txt
```

`cd`、`copy`、`dir`、`type` 等依赖命令解释器的内建语义，不能作为首个程序直接执行。确实需要时可显式
调用 `cmd /c ...` 或相应解释器；这样做也意味着后续参数由该解释器解释，启用列表不再限制其内部行为。

## 终端配置

公开终端配置位于 `config/config.json` 的 `plugins.shell`。未配置时使用 `direct`：

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "direct"
      }
    }
  }
}
```

Windows 上可显式指定 Git Bash。路径由部署者配置，项目不会猜测安装目录：

```json
{
  "plugins": {
    "shell": {
      "terminal": {
        "backend": "git-bash",
        "executable": "C:/Program Files/Git/bin/bash.exe"
      }
    }
  }
}
```

Git Bash 使用 `--noprofile --norc -c` 启动，不加载用户 profile/rc。原始命令仍会先经过危险模式和首入口启用列表校验，
随后才交给 Bash；启用 `python`、`bash` 等通用解释器仍等价于授予其完整能力。

## 命令启用配置

命令启用列表和超时位于 `config/secrets.json` 的 `plugins.shell`：

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

- 未提供 `whitelist` 时使用默认启用列表。
- `whitelist_mode = "replace"` 只启用自定义项；显式空列表会禁用全部命令入口。
- `whitelist_mode = "extend"` 在默认列表上增加自定义项。
- 非序列白名单和非字符串成员会被忽略，不会被拆成字符或隐式转换。
- `timeout` 必须是有限正数；无效值回退为 30 秒。
- 只有 JSON 布尔值 `true` 会关闭启用列表；字符串 `"true"` 或 `"false"` 不会改变权限。

## 执行与资源边界

- 明显的命令链接、管道、替换、多行输入及若干高风险误触形式始终被拒绝。
- `direct` 后端要求首个程序能由 Bot 进程 PATH 或明确路径解析；Git Bash 后端按其自身命令环境解析。
- 配置的 Git Bash 路径失效时会在执行前返回可操作提示，不会静默改用 WSL Bash 或 direct。
- 子进程标准输入固定连接到空设备；插件不支持交互式提示、编辑器或密码输入。
- 每条命令在独立进程组中启动；超时、输出溢出或任务取消会回收整棵子进程树。
- stdout 与 stderr 的原始捕获共享 64 KiB 硬上限，QQ 回复中的输出正文共享 4000 字符首尾预算。
- 审计日志只记录请求 ID、状态、返回码、长度和进程内指纹，不记录原始命令。

这些规则不能阻止已启用解释器执行任意代码，也不能限制普通程序自身的文件、网络或子进程能力。

## 路径格式

QQ 消息中建议统一使用 `/` 斜杠；插件会按 Bot 所在系统规范化看起来像本地路径的参数：

- Windows 可输入 `C:/workspace/a.txt`。
- Linux/macOS 可输入 `/home/user/a.txt`、`~/a.txt`、`./file` 或 `../file`。
- `key=value` 中的路径值也会规范化，URL 不会被改写。
- Windows 选项 `/c`、`/Y` 等不会被误判成路径。
- 路径含空格时必须使用引号。

## 运维建议

- 仅在受控管理员入口启用该插件，并定期检查管理员名单和入站认证。
- 生产环境优先使用最小化的 `replace` 列表，不要长期设置 `disable_whitelist = true`。
- 由部署者在启动 Bot 前配置程序和 PATH；若选择 Git Bash，其可执行文件路径只写在部署配置中，不写死在插件代码里。
- 不要把启用列表当作权限隔离；需要真正隔离时，应在操作系统层使用低权限账户、容器或专用执行服务。

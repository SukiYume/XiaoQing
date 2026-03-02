# Minecraft 插件

Minecraft 服务器管理插件，提供远程服务器控制和监控功能。

## 功能介绍

Minecraft 插件允许通过 XiaoQing 远程管理 Minecraft 服务器，包括启动、停止、执行命令、查看状态和日志监控等功能。

## 使用方法

### 基本命令

```
/mc start               # 启动服务器
/mc stop                # 停止服务器
/mc restart             # 重启服务器
/mc status              # 查看服务器状态
/mc cmd <命令>          # 执行服务器命令
/mc log [行数]          # 查看最新日志
/mc help                # 显示帮助信息
```

### 示例

```
/mc start
/mc status
/mc cmd list
/mc cmd say Hello World
/mc log 20
/mc stop
/mc help
```

## 配置说明

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "minecraft": {
      "server_path": "/path/to/minecraft/server",
      "java_path": "java",
      "java_args": "-Xmx2G -Xms2G",
      "jar_file": "server.jar",
      "auto_restart": true,
      "allowed_users": ["user_id_1", "user_id_2"]
    }
  }
}
```

### 配置项说明

- `server_path` - Minecraft 服务器目录路径（必需）
- `java_path` - Java 可执行文件路径（默认: java）
- `java_args` - Java 启动参数（如内存设置）
- `jar_file` - 服务器 JAR 文件名（默认: server.jar）
- `auto_restart` - 服务器崩溃时自动重启（默认: false）
- `allowed_users` - 允许使用此插件的用户 ID 列表

## 功能特性

- 远程启动/停止服务器
- 执行服务器控制台命令
- 实时查看服务器状态
- 日志监控和查看
- 自动重启支持
- 权限控制
- 服务器崩溃检测

## 安全注意事项

- 建议配置 `allowed_users` 限制可操作用户
- 服务器命令执行具有完全权限，请谨慎使用
- 定期备份服务器数据
- 确保 Java 环境正确配置

## 依赖

- Java Runtime Environment (用于运行 Minecraft 服务器)

## 注意事项

- 需要在服务器上运行 XiaoQing
- 确保有足够的系统资源运行 Minecraft 服务器
- 日志文件可能较大，注意磁盘空间
- 服务器启动需要时间，请耐心等待

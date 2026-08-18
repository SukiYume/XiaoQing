# 🚀 01 - 快速开始

本章从全新源码目录开始，完成依赖安装、配置创建、OneBot 接入、服务启动和首次验证。命令示例适用于 Git Bash、macOS 和 Linux。

---

## ⚙️ 准备环境

| 项目 | 要求 |
|---|---|
| Python | `3.10+`，可通过 `python` 命令调用 |
| OneBot | OneBot v11 实现，推荐 NapCatQQ |
| 依赖 | 当前环境可通过 pip 安装 `requirements.txt` |
| 网络 | 可访问 QQ 与启用插件所需的第三方服务 |

Python 环境、虚拟环境和依赖安装方式由使用者管理。XiaoQing 的统一启动命令为 `python main.py`。

---

## ⚙️ 1. 获取源码与安装依赖

```bash
git clone https://github.com/SukiYume/XiaoQing.git
cd XiaoQing
python -m pip install -r requirements.txt
```

根依赖清单覆盖 Core、默认插件和 Pendo Web。以下能力需要额外运行资源：

- Jupyter 插件需要可用的 Jupyter kernel。
- arXiv Filter 的本地推理需要 `plugins/arxiv_filter/best_model/` 模型资产。
- Flickr、Wolfram|Alpha、语音和其他外部 API 插件需要对应服务凭据。

---

## ⚙️ 2. 创建配置

```bash
cp config/config.json.example config/config.json
cp config/secrets.json.example config/secrets.json
```

### 公开配置

首次启动重点核对 `config/config.json` 中的以下字段：

```json
{
  "bot_name": "小青",
  "command_prefixes": ["/"],
  "require_bot_name_in_group": true,
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "onebot_http_base": "http://127.0.0.1:11001",
  "inbound_http_base": "http://127.0.0.1:12000",
  "inbound_trusted_tls_proxy": false,
  "timezone": "Asia/Shanghai",
  "log_level": "INFO",
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

### 敏感配置

`config/secrets.json` 至少设置 Inbound 鉴权 token 和管理员 QQ：

```json
{
  "inbound_token": "replace-with-a-random-secret",
  "admin_user_ids": [123456789]
}
```

将 `config/secrets.json` 保存在部署主机，并为文件设置与 Bot 进程相匹配的读取权限。项目的 Git 忽略规则覆盖正式 secrets 文件。

首次部署在启动前写入完整配置。服务运行期间可在管理员私聊中用 `/set_secret <已有路径> <值>` 提交单项敏感配置。需要新增路径或整体替换 `config.json`、`secrets.json` 时，先停止服务，保存两个完整文件，再启动新进程。

### AI 插件配置

`xiaoqing_chat`、Pendo AI 解析和其他模型调用统一使用 AI 注册表：

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions"
      }
    },
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"]
      }
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "routes": {
          "chat": {
            "models": ["deepseek-flash"]
          }
        }
      }
    }
  }
}
```

对应 API Key 位于 `config/secrets.json`：

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_key": "replace-with-provider-key"
      }
    }
  }
}
```

[配置详解](06-configuration.md) 说明完整字段、模型 fallback、插件配置和安全边界。

---

## ⚙️ 3. 配置 OneBot

### 被动 Inbound

被动模式由 OneBot 向 XiaoQing 推送事件。NapCat HTTP POST 示例：

```yaml
http:
  post:
    - url: http://127.0.0.1:12000/event
      secret: replace-with-a-random-secret
```

XiaoQing 侧配置：

```json
{
  "enable_ws_client": false,
  "enable_inbound_server": true,
  "inbound_http_base": "http://127.0.0.1:12000"
}
```

HTTP `/event` 与 WebSocket `/ws` 共享同一 `inbound_token`、会话排序和有界接纳队列。启用任一 Inbound Listener 时，请设置非空 `inbound_token`。

### 主动 WebSocket

主动模式由 XiaoQing 连接 OneBot：

```json
{
  "enable_ws_client": true,
  "enable_inbound_server": false,
  "onebot_ws_uri": "ws://127.0.0.1:11000/ws",
  "onebot_http_base": "http://127.0.0.1:11001"
}
```

OneBot 服务启用 Bearer 鉴权时，在 `config/secrets.json` 顶层配置 `onebot_token`。有效 secrets 快照中的空字符串表示双方约定的匿名连接模式。

### 网络边界

Inbound 默认绑定 loopback。公网部署使用 Nginx 或 Caddy 终止 HTTPS/WSS，并把流量转发到 loopback。受控跨容器网络可结合防火墙设置 `inbound_trusted_tls_proxy: true`。

---

## 🚀 4. 启动服务

先启动 OneBot 实现，再从项目根目录启动 XiaoQing：

```bash
python main.py
```

成功启动的日志包含以下信息：

- 配置 revision 发布完成
- 插件加载列表
- Inbound Server 或主动 WebSocket 状态
- Scheduler 启动状态
- Pendo Web 等插件内嵌服务状态

Windows 生产环境可使用受保护的启动链：

```text
scripts/run-bot.vbs
  → scripts/run-bot-monitor.ps1
  → scripts/run_process_with_rotating_logs.py
  → python main.py
```

该启动链从当前 `PATH` 调用 Python，并从 `config/config.json` 读取 NapCat 账号与可选 MKL threading 配置。

Windows 生产环境重启流程：

1. 双击 `scripts/stop-bot.vbs`。
2. 等待监控器、Bot 与 NapCat 已停止的完成提示。
3. 双击 `scripts/run-bot.vbs` 启动新进程。

停服入口使用仓库级互斥量和绝对命令路径识别当前部署，只收口该仓库的 PowerShell 监控器、Python 日志泵、`main.py` 与指定 NapCat 进程树。重复双击会返回同一完成结果。监控器由提升权限的 SSH、计划任务或管理员终端启动时，停服入口在身份读取连续失败后显示一次 UAC；提升后的进程重新完成全部身份校验再收口进程树。

---

## ✅ 5. 首次验证

在管理员私聊中依次发送：

```text
/help
/plugins
/metrics
/help bot_core
/help pendo
/help xiaoqing_chat
```

随后验证核心插件：

```text
/xc 你好
/pendo
/pendo todo list
/arxiv
```

命令帮助来自 Manifest，显示当前加载版本的入口、参数、权限、场景和样例。

---

## ✅ 上线前验证

### 自动化门禁

```bash
python -m compileall -q core plugins tests
python -m pytest -n 2
python -m ruff check .
python -m ruff format --check .
python -m mypy core plugins
git diff --check
```

### 完整 UAT

```bash
bash scripts/run_full_uat.sh --plan-only
bash scripts/run_full_uat.sh
```

UAT 运行真实服务，覆盖 HTTP/WS 命令矩阵、插件业务场景、Core 压力恢复、测试套件和静态门禁。报告位于 `test_reports/runs/project/full-uat-*/reports/FULL_UAT_REPORT.md`。

外部服务场景通过 `--include-external --scenario-fixtures <文件>` 启用，真实聊天质量场景通过 `--include-chat-quality` 启用。

---

## 🩺 常见问题

### 服务消息响应排查

1. 确认 OneBot 与 XiaoQing 进程均处于运行状态。
2. 核对 Inbound URL 或主动 WebSocket URI。
3. 核对 OneBot secret 与 `inbound_token`。
4. 在日志中查找连接状态、错误码和 request ID。
5. 日志同时出现 `secrets source is inconsistent` 和 `WebSocket client stopped` 时，停止服务，确认两个配置文件完整，再重新启动。

### 群聊命令响应排查

1. 确认消息包含命令前缀或符合 Bot 名称门控。
2. 确认命令允许群聊场景。
3. 确认当前 QQ 身份符合管理员要求。
4. 通过 `/help <命令>` 查看场景与权限。

### 普通聊天回复较少

1. 确认 `plugins.smalltalk_provider` 指向目标插件。
2. 检查当前群静音状态。
3. 检查 `xiaoqing_chat` 的 attention、频率控制和行为规划日志。

### 端口绑定异常

1. 核对 `onebot_http_base`、`onebot_ws_uri`、`inbound_http_base` 和 Pendo Web 端口。
2. 确认每个监听地址由一个服务占用。
3. 调整配置后重新启动服务。

### Pendo Web 登录页持续出现

1. 执行 `/pendo web status`。
2. 执行 `/pendo web token` 获取 7 天兑换期登录码。
3. 在登录页兑换 Code，并允许浏览器保存 Cookie。
4. 查看 Pendo Web 日志中的 session 校验结果。

---

## 🧭 下一步

- 生产参数与安全设置：[配置详解](06-configuration.md)
- 插件功能入口：[插件目录](09-plugins.md)
- 消息排障：[消息处理流程](08-message-flow.md)

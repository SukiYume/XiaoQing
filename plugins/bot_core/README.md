# Bot Core Plugin

XiaoQing Bot 核心管理插件，提供机器人的基础管理功能。

## 版本信息

- **版本**: 0.2.0
- **作者**: XiaoQing Bot Team
- **最后更新**: 2026-01-30

## 功能概览

Bot Core 插件提供以下核心功能：

1. **帮助系统** - 查看插件命令帮助信息
2. **热重载** - 动态重载配置和插件，无需重启
3. **插件管理** - 查看已加载的插件列表
4. **群消息控制** - 群内静音/解除静音
5. **配置管理** - 动态查看和修改 secrets 配置
6. **性能监控** - 查看运行指标和性能统计

## 命令列表

### 1. 帮助 (help)

查看命令帮助信息，支持关键词搜索。

**触发词**: `help`, `h`, `帮助`

**用法**:
```
/help                  # 查看所有命令
/help reload           # 查看特定命令
/帮助 静音              # 搜索相关命令
```

**参数**:
- `关键词` (可选): 搜索关键词，会匹配命令名称、触发词和帮助文本

**示例**:
```
用户: /help
Bot: 📋 可用命令列表
     ━━━━━━━━━━━━━━━━━━━━
     /help [关键词] - 查看帮助
     /reload - 热重载配置和插件
     ...

用户: /help 静音
Bot: 📋 搜索结果: 静音
     ━━━━━━━━━━━━━━━━━━━━
     /闭嘴 [分钟/1h] - 群内静音
     /说话 - 解除群内静音
```

**特性**:
- 自动过滤权限不足的管理员命令
- 支持模糊搜索，匹配命令名、触发词、帮助文本
- 紧凑显示，最多显示前 20 个匹配结果

---

### 2. 重载 (reload)

热重载配置文件和插件，无需重启机器人。

**触发词**: `reload`, `重载`

**权限**: 仅管理员

**用法**:
```
/reload
/重载
```

**功能**:
- 重新加载 `config.json` 和 `secrets.json`
- 重新扫描并加载插件
- 更新插件命令映射
- 同步调度任务

**示例**:
```
用户: /reload
Bot: ✅ 重载成功！
     已加载 15 个插件
```

**注意事项**:
- 重载期间可能会有短暂的响应延迟
- 配置错误会导致重载失败，需要修复后重试
- 不会影响已有的群静音状态

---

### 3. 插件列表 (plugins)

查看当前已加载的插件列表。

**触发词**: `plugins`, `插件`

**用法**:
```
/plugins
/插件
```

**示例**:
```
用户: /plugins
Bot: 📦 已加载插件 (15)
     ━━━━━━━━━━━━━━━━━━━━
     • bot_core (0.2.0)
     • chime (0.2.0)
     • chat (0.2.0)
     • choice (0.2.0)
     • color (0.2.0)
     ...
```

**信息包含**:
- 插件名称
- 版本号（如果在 plugin.json 中定义）
- 总数统计

---

### 4. 静音 (闭嘴)

让机器人在指定群内保持安静一段时间。

**触发词**: `闭嘴`, `shutup`, `mute`, `安静`

**权限**: 仅管理员

**用法**:
```
/闭嘴              # 默认静音 10 分钟
/闭嘴 30           # 静音 30 分钟
/闭嘴 1h           # 静音 1 小时
/闭嘴 1.5h         # 静音 1.5 小时
/mute 2h           # 静音 2 小时
```

**参数**:
- `时长` (可选): 静音时长，默认 10 分钟
  - 纯数字: 视为分钟数 (如 `30`)
  - 带 `m`/`min`/`分钟`: 分钟数 (如 `30m`)
  - 带 `h`/`小时`: 小时数 (如 `1.5h`)

**限制**:
- 最大静音时长: 24 小时 (1440 分钟)
- 仅在群聊中有效，私聊不支持

**示例**:
```
用户: /闭嘴 30
Bot: 🤐 好的，我会安静 30 分钟

用户: /mute 25h
Bot: ❌ 静音时长过长，最多支持 24 小时

用户: (私聊) /闭嘴
Bot: ❌ 私聊不支持此命令
```

**注意事项**:
- 静音期间机器人不会响应该群的任何消息
- 管理员命令不受静音影响
- 可以使用解除静音命令提前恢复

---

### 5. 解除静音 (说话)

解除群内静音状态，让机器人恢复响应。

**触发词**: `说话`, `speak`, `unmute`

**权限**: 仅管理员

**用法**:
```
/说话
/speak
/unmute
```

**示例**:
```
用户: /说话
Bot: 😊 好的！

用户: /说话
Bot: ❌ 本群未静音
```

**注意事项**:
- 仅在群聊中有效
- 会清除该群的所有静音状态

---

### 6. 设置密钥 (set_secret)

动态修改 `secrets.json` 中的配置值。

**触发词**: `set_secret`, `setsecret`, `设置密钥`

**权限**: 仅管理员

**用法**:
```
/set_secret <路径> <值>
/设置密钥 <路径> <值>
```

**参数**:
- `路径`: 使用 `.` 分隔的配置路径，如 `plugins.signin.yingshijufeng.sid`
- `值`: 新的配置值，支持字符串、数字、布尔值、JSON 数组/对象

**示例**:
```
# 设置字符串
用户: /set_secret plugins.signin.sid YZ123456
Bot: ✅ 已更新 plugins.signin.sid
     新值: YZ12****

# 设置数组
用户: /set_secret admin_user_ids [123456,789012]
Bot: ✅ 已更新 admin_user_ids
     新值: [123456, 789012]

# 设置数字
用户: /set_secret timeout 30
Bot: ✅ 已更新 timeout
     新值: 30

# 路径不存在
用户: /set_secret invalid.path value
Bot: ❌ 路径不存在
     💡 提示: 使用 /get_secret 查看现有配置路径
```

**值类型自动识别**:
- JSON 格式（数组、对象、数字、布尔）会自动解析
- 其他视为字符串

**注意事项**:
- 修改后会自动触发配置重载
- 敏感信息会自动打码显示
- 仅能修改已存在的配置路径

---

### 7. 查看密钥 (get_secret)

查看 `secrets.json` 中的配置值。

**触发词**: `get_secret`, `getsecret`, `查看密钥`

**权限**: 仅管理员

**用法**:
```
/get_secret <路径>
/查看密钥 <路径>
```

**参数**:
- `路径`: 使用 `.` 分隔的配置路径

**示例**:
```
# 查看具体值
用户: /get_secret plugins.signin.sid
Bot: 🔑 plugins.signin.sid = YZ12****

# 查看对象的键
用户: /get_secret plugins
Bot: 🔑 plugins 包含以下键:
       signin, chat, chime, ... 还有 10 个

# 查看数组
用户: /get_secret admin_user_ids
Bot: 🔑 admin_user_ids = [123456, 789012]

# 路径不存在
用户: /get_secret invalid.path
Bot: ❌ 路径 invalid.path 不存在
```

**特性**:
- 敏感信息自动打码（保留前 4 位，如 `YZ12****`）
- 对象类型显示键列表（超过 20 个会截断）
- 数组和基本类型直接显示值

---

### 8. 性能指标 (metrics)

查看机器人运行指标和性能统计。

**触发词**: `metrics`, `stats`, `性能`, `指标`

**权限**: 仅管理员

**用法**:
```
/metrics
/性能
/指标
```

**示例**:
```
用户: /metrics
Bot: 📈 运行指标
     ━━━━━━━━━━━━━━━━━━━━
     ⏱️ 运行时间: 3625s
     📦 总调用: 1245
     ✅ 成功率: 98.5%
     ⏳ 平均耗时: 0.123s
     🐢 慢调用: 23
     ❌ 错误: 18
     ━━━━━━━━━━━━━━━━━━━━
     ⚠️ 最慢插件:
       • arxiv_filter: 2.456s
       • github: 1.234s
       • wolframalpha: 0.987s
```

**指标说明**:
- **运行时间**: 自上次启动以来的秒数
- **总调用**: 处理的命令总数
- **成功率**: 成功处理的命令占比
- **平均耗时**: 命令平均执行时间
- **慢调用**: 超过阈值的命令数量
- **错误**: 执行失败的命令数量
- **最慢插件**: 按平均耗时排序的前 5 个插件

**注意事项**:
- 需要在 `config.json` 中启用 metrics 功能
- 如果未启用会提示 "❌ Metrics 未启用"

---

## 权限管理

### 管理员权限

以下命令需要管理员权限：

- `/reload` - 重载配置
- `/闭嘴` - 群内静音
- `/说话` - 解除静音
- `/set_secret` - 修改配置
- `/get_secret` - 查看配置
- `/metrics` - 查看指标

### 普通用户权限

以下命令所有用户都可使用：

- `/help` - 查看帮助
- `/plugins` - 查看插件列表

**权限配置**:

管理员用户 ID 在 `secrets.json` 中配置：

```json
{
  "admin_user_ids": [123456789, 987654321]
}
```

---

## 配置说明

### config.json

```json
{
  "metrics": {
    "enabled": true,
    "slow_threshold_seconds": 1.0
  }
}
```

**配置项**:
- `metrics.enabled`: 是否启用性能监控
- `metrics.slow_threshold_seconds`: 慢调用阈值（秒）

### secrets.json

```json
{
  "admin_user_ids": [123456789],
  "plugins": {
    "signin": {
      "yingshijufeng": {
        "sid": "YZ123456"
      }
    }
  }
}
```

**配置项**:
- `admin_user_ids`: 管理员用户 ID 列表
- `plugins.*`: 各插件的私密配置

---

## 常见问题

### Q: 重载失败怎么办？

**A**: 检查以下几点：
1. `config.json` 和 `secrets.json` 格式是否正确
2. 查看日志文件了解详细错误信息
3. 确保所有必需的配置项都存在
4. 尝试重启机器人

### Q: 静音后管理员命令也无法使用？

**A**: 不会，管理员命令不受静音影响。如果无法使用，请检查：
1. 你的用户 ID 是否在 `admin_user_ids` 中
2. 是否在正确的群聊中发送命令

### Q: 如何设置复杂的配置值？

**A**: 使用 JSON 格式：
```
/set_secret plugins.myconfig.list [1,2,3]
/set_secret plugins.myconfig.obj {"key":"value"}
```

### Q: 密钥会完全显示吗？

**A**: 不会，敏感信息会自动打码：
- 字符串: 只显示前 4 位，如 `ABCD****`
- 数组: 显示长度，如 `["***", "***"]`
- 其他: 显示类型，如 `<对象>`

### Q: Metrics 显示 "未启用" 怎么办？

**A**: 在 `config.json` 中添加：
```json
{
  "metrics": {
    "enabled": true
  }
}
```
然后执行 `/reload`

### Q: 如何查看所有可用的 secret 路径？

**A**: 逐级查看：
```
/get_secret plugins              # 查看所有插件
/get_secret plugins.signin       # 查看 signin 插件配置
/get_secret plugins.signin.sid   # 查看具体值
```

---

## 技术细节

### 常量定义

```python
# 默认静音时长 (分钟)
DEFAULT_MUTE_MINUTES = 10

# 最大静音时长 (分钟) - 24小时
MAX_MUTE_MINUTES = 1440

# 帮助列表最大显示数量
MAX_HELP_RESULTS = 20

# Metrics 分隔符
METRICS_SEPARATOR = "━" * 20
```

### 日志记录

所有命令执行都会记录详细日志：

```python
logger.info(f"执行帮助命令, 查询: {query}")
logger.warning(f"静音时长超过限制: {duration} > {MAX_MUTE_MINUTES}")
logger.error(f"重载失败: {e}", exc_info=True)
```

日志文件位置: `logs/xiaoqing.log.YYYY-MM-DD`

### 密钥脱敏

`mask_secret()` 函数自动处理敏感信息：

```python
mask_secret("ABCDEFGH")          # "ABCD****"
mask_secret([1, 2, 3])           # ["***", "***", "***"]
mask_secret({"key": "value"})    # "<对象>"
```

### 时长解析

`_parse_duration()` 支持多种格式：

```python
_parse_duration("10")      # 10.0 (分钟)
_parse_duration("30m")     # 30.0 (分钟)
_parse_duration("1h")      # 60.0 (分钟)
_parse_duration("1.5h")    # 90.0 (分钟)
_parse_duration("30分钟")   # 30.0 (分钟)
```

---

## 开发者指南

### 添加新命令

1. 在 `main.py` 中添加处理函数：
```python
def _handle_mycommand(args: str, context) -> List[Dict[str, Any]]:
    """处理自定义命令
    
    Args:
        args: 命令参数
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        # 处理逻辑
        logger.info(f"执行自定义命令: {args}")
        return segments("✅ 成功")
    except Exception as e:
        logger.error(f"命令执行失败: {e}", exc_info=True)
        return segments(f"❌ 失败: {e}")
```

2. 在 `handle()` 中添加路由：
```python
async def handle(event: Dict[str, Any], context) -> List[Dict[str, Any]]:
    cmd = event.get("raw_command", "").lower()
    
    if cmd in ["mycommand", "mycmd"]:
        return _handle_mycommand(args, context)
```

3. 在 `plugin.json` 中注册：
```json
{
  "name": "mycommand",
  "triggers": ["mycommand", "mycmd"],
  "help": "我的自定义命令 | /mycommand <参数>",
  "admin_only": false
}
```

### 测试建议

1. **单元测试**: 测试各个处理函数
2. **集成测试**: 测试完整命令流程
3. **权限测试**: 测试管理员权限控制
4. **错误测试**: 测试异常情况处理

---

## 更新日志

### v0.2.0 (2026-01-30)
- ✨ 添加常量定义（DEFAULT_MUTE_MINUTES, MAX_MUTE_MINUTES）
- 🔧 改进所有命令的错误处理
- 📝 添加详细的日志记录
- ✅ 增强输入验证（时长限制、路径格式）
- 🎨 优化消息格式和用户提示
- 📖 添加完整的函数文档字符串
- 🔒 改进密钥脱敏机制
- 📊 优化 Metrics 显示格式

### v0.1.0
- 🎉 初始版本
- 实现基础命令：help, reload, plugins, mute, unmute
- 实现配置管理：set_secret, get_secret
- 实现性能监控：metrics

---

## 许可证

本插件是 XiaoQing Bot 项目的一部分。

---

## 联系方式

如有问题或建议，请通过以下方式联系：

- 提交 Issue 到项目仓库
- 联系 XiaoQing Bot 开发团队

---

**文档更新**: 2026-01-30  
**插件版本**: 0.2.0

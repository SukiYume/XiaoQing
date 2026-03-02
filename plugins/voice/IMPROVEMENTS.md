# Voice Plugin Improvements (v2.0.0)

## 改进概述

本次对 `voice` 插件进行了全面重构，统一其编码规范与其他插件一致，并提升了代码的可维护性、可读性和用户体验。

---

## 主要改进

### 1. **标准化初始化函数**
- **新增** `init()` 函数，在插件加载时记录日志
- 遵循框架统一的初始化模式

```python
def init():
    """插件初始化"""
    logger.info("语音功能插件已加载 (Voice Plugin Loaded)")
```

### 2. **统一日志记录**
- **移除** 对 `context.logger` 的依赖
- **采用** `logging.getLogger(__name__)` 标准日志记录器
- 所有日志调用统一为 `logger.info()`, `logger.warning()`, `logger.error()` 等
- 涉及修改：
  - TTS 缓存使用日志
  - TTS API 调用日志
  - STT API 调用日志
  - 语音识别结果日志

### 3. **创建帮助函数**
- **新增** `_show_help()` 函数，返回类型统一为 `str`
- 提供清晰的使用说明和示例

```python
def _show_help() -> str:
    """返回帮助信息"""
    return (
        "🔊 语音功能使用方法：\n\n"
        "1. 文字转语音：\n"
        "   /语音 <文本> 或 /念 <文本> 或 /tts <文本>\n"
        "   例：/语音 你好，我是小青\n\n"
        "2. 查看帮助：\n"
        "   /语音 help\n\n"
        "💡 插件使用 Azure 认知服务，支持多种声音和风格"
    )
```

### 4. **统一命令解析与异常处理**

#### 4.1 使用 `parse()` 函数
- **新增** `from core.args import parse` 导入
- 在 `handle()` 函数中使用 `parse(args)` 进行参数解析
- 支持 `help` 参数查看详细帮助

#### 4.2 异常处理
- 为 `handle()` 函数添加统一的 `try-except` 块
- 捕获所有异常并返回友好错误信息：`"❌ 处理失败: {str(exc)}"`
- 记录详细的错误日志便于调试：`logger.error(f"处理命令时出错: {exc}", exc_info=True)`

```python
async def handle(command: str, args: str, event: dict[str, Any], context: PluginContext) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        # 使用 parse 解析参数
        parsed = parse(args)
        
        if command == "tts":
            # 检查是否请求帮助
            if parsed and parsed.first.lower() in ["help", "帮助"]:
                return segments(_show_help())
            
            return await _handle_tts(args, context)
        
        return segments("未知命令")
    
    except Exception as exc:
        logger.error(f"处理命令时出错: {exc}", exc_info=True)
        return segments(f"❌ 处理失败: {str(exc)}")
```

### 5. **帮助命令支持**
- 支持 `/语音 help` 查看详细帮助
- 帮助信息包含使用示例和提示

### 6. **优化日志消息**
- 所有日志消息改为中文，便于理解
- 添加更详细的上下文信息
- 区分不同级别的日志（info/warning/error）

### 7. **增强异常日志**
- 在 `text_to_speech()` 和 `speech_to_text()` 中添加 `exc_info=True`
- 记录完整的堆栈跟踪，便于调试

### 8. **更新 `plugin.json`**
- **版本** `1.0.0` → `2.0.0`
- **描述** 更详细：`语音功能插件 - 支持文字转语音(TTS)和语音转文字(STT)，使用 Azure 认知服务`
- **帮助文本** 添加 help 引导

---

## 特殊功能说明

### 1. **双重功能**
`voice` 插件提供两种功能：
- **TTS (Text-to-Speech)**: 文字转语音
- **STT (Speech-to-Text)**: 语音转文字

### 2. **Azure 认知服务集成**
- 使用 Microsoft Azure 认知服务
- 需要配置 `subscription_key` 和 `region`
- 支持多种语音（voice_name）、风格（style）和角色（role）

### 3. **音频缓存机制**
- 基于文本内容 MD5 哈希生成文件名
- 如果相同文本已生成过音频，直接返回缓存文件
- 避免重复调用 API，节省成本

### 4. **SSML 支持**
- 使用 SSML (Speech Synthesis Markup Language) 格式
- 支持表达样式（express-as）和角色设置
- 可配置声音名称、风格和角色

### 5. **工具函数导出**
- 提供 `text_to_speech()` 和 `convert_text_to_voice()` 供其他插件调用
- `speech_to_text()` 供语音消息处理使用
- 实现插件间协作（如 smalltalk 插件调用 voice 插件）

### 6. **配置灵活**
通过 `secrets.json` 配置：
- `voice.subscription_key`: Azure 订阅密钥（必需）
- `voice.region`: Azure 区域（默认 southeastasia/eastasia）
- `voice.voice_name`: 声音名称（默认 zh-CN-XiaomoNeural）
- `voice.style`: 说话风格（默认 cheerful）
- `voice.role`: 角色（默认 Girl）

---

## 与其他插件对比

| 特性 | voice | 其他插件（如 twitter, memo） |
|------|-------|---------------------------|
| 命令模式 | ✅ 单一命令 (tts) | ✅ 单一或多命令 |
| 外部 API | ✅ Azure 认知服务 | ✅ 或 ❌ 视插件而定 |
| 缓存机制 | ✅ 文件缓存 | ❌ 大部分无缓存 |
| 工具函数导出 | ✅ 供其他插件调用 | ❌ 大部分独立运行 |
| 持久化 | 文件存储 (audio/) | 文件/内存/会话 |
| 付费服务 | ✅ 需要 Azure 订阅 | ❌ 大部分免费 |

---

## 用户体验改进

### 改进前
- ❌ 无初始化日志
- ❌ 无内嵌帮助信息
- ❌ 日志使用 `context.logger`
- ❌ 无统一异常处理
- ❌ 日志消息为英文

### 改进后
- ✅ 标准化 `init()` 函数，记录加载信息
- ✅ 详细的内嵌帮助信息（`/语音 help`）
- ✅ 统一使用 `logging.getLogger(__name__)`
- ✅ 统一异常处理，友好错误提示
- ✅ 日志消息改为中文，便于理解

---

## 代码质量提升

### 1. 可读性
- 帮助信息清晰，易于理解
- 日志消息改为中文
- 函数职责明确

### 2. 可维护性
- 统一日志记录，便于调试
- 标准化异常处理，减少重复代码
- 配置提取为独立获取逻辑

### 3. 健壮性
- 添加详细的错误信息和堆栈跟踪（`exc_info=True`）
- 区分配置错误、API 错误和程序错误
- 缓存机制避免重复请求

### 4. 扩展性
- 工具函数易于被其他插件调用
- 配置灵活，易于调整行为
- SSML 格式易于扩展更多语音特性

---

## 兼容性说明

- ✅ **完全向后兼容**：旧的命令格式仍然有效
- ✅ **框架升级无痛**：遵循框架标准模式
- ✅ **配置文件兼容**：无需修改 `secrets.json` 格式
- ✅ **API 接口兼容**：`text_to_speech()` 等函数签名未变

---

## 测试建议

### 1. 功能测试
```
/语音 你好
/语音 今天天气真好
/念 测试语音合成
/tts Hello World
/语音 help
```

### 2. 缓存测试
- 发送相同文本两次，检查第二次是否使用缓存
- 检查日志是否显示 "使用缓存音频"

### 3. 异常测试
- 配置错误的 `subscription_key`，检查错误处理
- 发送空文本，检查提示信息
- 网络断开时测试，检查错误日志

### 4. 集成测试
- 测试其他插件调用 `convert_text_to_voice()` 函数
- 测试 smalltalk 插件的语音回复功能

### 5. 日志检查
查看 `logs/xiaoqing.log`：
- 启动时应有 `"语音功能插件已加载"` 日志
- 使用缓存应有 `"使用缓存音频: ..."` 日志
- API 调用应有 `"生成音频: ..."` 日志
- 错误应有详细的堆栈跟踪

---

## 配置示例

在 `config/secrets.json` 中配置：

```json
{
  "plugins": {
    "voice": {
      "subscription_key": "YOUR_AZURE_SUBSCRIPTION_KEY",
      "region": "southeastasia",
      "voice_name": "zh-CN-XiaomoNeural",
      "style": "cheerful",
      "role": "Girl"
    }
  }
}
```

### 可用声音列表
- `zh-CN-XiaomoNeural`: 小墨（女声）
- `zh-CN-XiaoxiaoNeural`: 晓晓（女声）
- `zh-CN-YunxiNeural`: 云希（男声）
- `zh-CN-YunjianNeural`: 云健（男声）

### 可用风格
- `cheerful`: 欢快
- `sad`: 悲伤
- `angry`: 愤怒
- `fearful`: 恐惧
- `calm`: 平静
- `gentle`: 温柔

---

## 技术细节

### TTS 工作流程
1. **文本处理**: 接收用户输入文本
2. **缓存检查**: 计算 MD5 哈希，检查是否已缓存
3. **SSML 构建**: 使用配置的声音、风格、角色构建 SSML
4. **API 调用**: 调用 Azure TTS API
5. **文件保存**: 保存为 MP3 文件
6. **返回路径**: 返回绝对路径供框架发送

### STT 工作流程
1. **音频读取**: 读取 WAV 音频文件
2. **API 调用**: 调用 Azure STT API
3. **结果解析**: 解析 JSON 响应，提取文本
4. **返回结果**: 返回 (分词文本, 完整文本) 元组

### 缓存机制
- **哈希算法**: MD5
- **文件命名**: `tts_{hash[:8]}.mp3`
- **存储位置**: `data/audio/`
- **缓存策略**: 永久缓存，除非手动清理

---

## 未来改进方向

1. **更多语音选项**: 支持更多语言和声音
2. **语速控制**: 添加语速配置选项
3. **音量控制**: 添加音量配置选项
4. **缓存管理**: 
   - 添加缓存大小限制
   - 自动清理过期缓存
   - 提供缓存清理命令
5. **STT 集成**: 完善语音转文字功能，自动处理语音消息
6. **错误重试**: API 调用失败时自动重试
7. **多区域支持**: 自动选择最快的 Azure 区域

---

## 成本优化建议

由于使用 Azure 付费服务，建议：

1. **启用缓存**: 相同文本不重复调用 API（已实现）
2. **限制长度**: 配置最大文本长度限制
3. **频率限制**: 添加用户调用频率限制
4. **监控使用**: 定期检查 Azure 使用量和成本
5. **降级方案**: API 失败时提供降级文本回复

---

## 总结

本次重构使 `voice` 插件达到与其他插件相同的代码质量标准，同时保留了其独特的 Azure 集成、缓存机制和工具函数导出功能。所有改进均遵循 XiaoQing 框架的最佳实践，为后续维护和扩展打下良好基础。

**版本变更**：`1.0.0` → `2.0.0`  
**重构完成日期**：2026-02-04

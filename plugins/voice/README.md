# Azure Voice

公开命令只有管理员专用的 TTS：`/语音 <文本>`、`/念 <文本>` 或 `/tts <文本>`。STT 是供内部插件调用的工具函数，不是独立聊天命令。

在 `config/secrets.json` 的唯一受支持层级配置：

```json
{
  "plugins": {
    "voice": {
      "subscription_key": "AZURE_SPEECH_KEY",
      "region": "southeastasia",
      "voice_name": "zh-CN-XiaomoNeural",
      "style": "cheerful",
      "role": "Girl"
    }
  }
}
```

不要把 key 写到公开 `config.json` 或聊天消息。TTS 文本、返回音频、STT 输入字节/时长和缓存总量均有上限；日志只记录长度、状态和错误类型，不记录 key 或识别正文。

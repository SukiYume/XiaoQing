# Azure Voice

`voice` 通过 Azure Speech 提供管理员专用的文字转语音命令，并向已声明的插件提供 `voice.synthesize_text` 服务。`speech_to_text()` 是内部工具函数，不会注册独立聊天命令。

## 命令与服务

- `/语音 <文本>`、`/念 <文本>`、`/tts <文本>`：合成 MP3 语音，文本最多 500 个字符。
- `/语音 help`：查看命令帮助。
- `voice.synthesize_text`：把文本转换为 OneBot `record` 消息段，当前允许调用方为 `smalltalk`。

## 配置

只从 `config/secrets.json` 的 `plugins.voice` 读取配置：

```json
{
  "plugins": {
    "voice": {
      "subscription_key": "AZURE_SPEECH_KEY",
      "region": "southeastasia",
      "voice_name": "zh-CN-XiaomoNeural",
      "style": "cheerful",
      "role": "Girl",
      "proxy": ""
    }
  }
}
```

`subscription_key` 必填；其余语音字段使用上面的安全默认值。`proxy` 留空表示直连，非空时必须是结构完整的 HTTP(S) 代理 URL。不要把订阅密钥写入公开配置、文档或聊天消息。

## 边界与缓存

- TTS 响应经过状态、MIME、压缩后字节数和 MP3 文件头校验，单个响应最多 10 MiB。
- 缓存键包含文本、区域和全部音色设置；缓存最多 2048 项、256 MiB，条目保留 7 天。损坏的历史缓存会被删除后重新生成。
- 内部 STT 只接受最多 10 MiB、最长 120 秒的 16 kHz、单声道、16 位未压缩 PCM WAV；损坏、截断或格式不符的文件不会上传。
- TTS 与 STT 共用最多 2 个并发 Azure 请求；网络响应与识别文本均有显式上限。
- 日志只记录状态、字节数和文本长度，不记录订阅密钥、代理凭据或识别正文。

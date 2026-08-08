# 🎙️ Azure Voice

`voice` 通过 Azure Speech 提供管理员文字转语音命令，并向 Manifest 授权的插件发布 `voice.synthesize_text` 服务。

---

## ⌨️ 命令与服务

```text
/语音 <文本>
/念 <文本>
/tts <文本>
/语音 help
```

命令文本长度范围为 1～500 个字符，返回 OneBot `record` 消息段。Manifest 将命令标记为 Bot 管理员入口。

服务契约：

| 服务 | 回调 | 当前调用方 |
| --- | --- | --- |
| `voice.synthesize_text` | `convert_text_to_voice` | `smalltalk` |

Core 根据服务调用者白名单签发能力，调用方收到与命令相同格式的语音消息段。

---

## ⚙️ 凭据与音色

在 `config/secrets.json` 中配置：

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

| 字段 | 规则 |
| --- | --- |
| `subscription_key` | 必填 Azure Speech 订阅密钥 |
| `region` | Azure 区域，默认 `southeastasia` |
| `voice_name` | 音色名，默认 `zh-CN-XiaomoNeural` |
| `style` | SSML 风格，默认 `cheerful` |
| `role` | SSML 角色，默认 `Girl` |
| `proxy` | 可选 HTTP 或 HTTPS 代理 URL |

字段会经过类型、长度和字符模式校验。订阅密钥与代理凭据保存在聊天、公开配置和普通日志边界之外。

---

## 🔄 合成流程

1. 规范化文本并读取原子 secret 快照；
2. 以文本、区域和全部音色设置计算缓存身份；
3. 命中有效 MP3 缓存时直接返回；
4. 生成经过 XML 转义的 SSML；
5. 请求 Azure Speech MP3 输出；
6. 校验响应并原子写入缓存；
7. 返回 OneBot 语音消息段。

同一缓存身份由 keyed lock 合并并发生成，Azure 请求最多同时执行 2 个。

---

## 🔐 响应与缓存边界

| 项目 | 当前预算 |
| --- | ---: |
| 文本 | 500 个字符 |
| Azure 总超时 | 60 秒 |
| 单个音频 | 10 MiB |
| 缓存 | 2048 项、256 MiB、7 天 |

响应需要通过 HTTP 状态、MIME、解码字节和 MP3 文件头校验。缓存文件位于：

```text
data/voice/audio/
```

缓存键包含全部影响音频的输入。缓存读取会校验普通文件、大小与 MP3 头，异常条目进入清理和重新生成路径。

---

## 🩺 排障

| 现象 | 检查项 |
| --- | --- |
| 提示订阅密钥缺失 | 核对 `plugins.voice.subscription_key` |
| Azure 返回认证错误 | 核对密钥与 `region` |
| SSML 音色错误 | 核对 `voice_name`、`style` 和 `role` 是否受该音色支持 |
| 代理连接失败 | 核对代理 URL 与网络权限 |
| 语音服务调用被拒绝 | 核对 Manifest 服务调用者白名单 |
| 缓存写入失败 | 检查 `data/voice/audio/` 权限与磁盘空间 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m pytest -q tests/plugins/voice/test_voice.py \
  tests/core/test_app_plugin_capabilities.py
python -m ruff check plugins/voice tests/plugins/voice/test_voice.py
python -m mypy plugins/voice
```

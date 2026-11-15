# 🎙️ Azure Voice

`voice` 通过 Azure Speech 提供所有人可用的文字转语音命令，并向被授权的插件提供 `voice.synthesize_text` 服务。

---

## ⌨️ 命令与服务

```text
/语音 <文本>
/念 <文本>
/tts <文本>
/语音 help
```

命令文本长度范围为 1～500 个字符，返回 OneBot `record` 消息段。命令对所有人开放，没有按人限次，每次调用都计入 Azure 用量。

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

`subscription_key` 和 `region` 来自 Azure 门户里创建的语音（Speech）资源，需要自行取得。`region`、`voice_name`、`style`、`role` 的取值超长或含非法字符时回退到默认值，不会报错。订阅密钥与代理凭据不会出现在聊天、公开配置和日志里。`subscription_key` 缺失时命令回复“语音合成失败”。

---

## 🔄 合成流程

1. 文本、区域和全部音色设置都相同的请求命中缓存时，直接返回已有的 MP3。
2. 否则请求 Azure Speech 合成 MP3，校验后写入缓存，并以 OneBot 语音消息段返回。

同一段文本的并发请求合并为一次合成，Azure 请求最多同时执行 2 个。

---

## 🔐 响应与缓存边界

| 项目 | 当前预算 |
| --- | ---: |
| 文本 | 500 个字符 |
| Azure 总超时 | 60 秒 |
| 单个音频 | 10 MiB |
| 缓存 | 2048 项、256 MiB、7 天 |

Azure 的响应必须是合法的 MP3 音频，否则视为合成失败。缓存文件位于：

```text
data/voice/audio/
```

缓存键包含文本、区域和全部音色设置，修改配置后不会误用旧音频。损坏的缓存文件会被清除并重新合成。

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

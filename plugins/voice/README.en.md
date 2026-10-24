# 🎙️ Azure Voice

[中文](README.md) | English

`voice` uses Azure Speech to supply text-to-speech commands to all users.
It also supplies `voice.synthesize_text` to plugins authorized by the Manifest.

## ⌨️ Commands and service

```text
/语音 <text>
/念 <text>
/tts <text>
/语音 help
```

Command text must contain 1–500 characters. The command returns a OneBot `record` message segment.
All users can use the command. Each user has unlimited calls. Each call counts toward Azure usage.

| Service | Callback | Current caller |
| --- | --- | --- |
| `voice.synthesize_text` | `convert_text_to_voice` | `smalltalk` |

Core issues capabilities from the service caller list. Service callers receive the same voice message format as command callers.

## ⚙️ Credentials and voice settings

The settings are in `config/secrets.json`:

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

| Field | Rule |
| --- | --- |
| `subscription_key` | Necessary Azure Speech subscription key |
| `region` | Azure region. Default: `southeastasia` |
| `voice_name` | Voice name. Default: `zh-CN-XiaomoNeural` |
| `style` | SSML style. Default: `cheerful` |
| `role` | SSML role. Default: `Girl` |
| `proxy` | Optional HTTP or HTTPS proxy URL |

Checks control field types, lengths, and character patterns.
Credential filtering excludes subscription keys and proxy credentials from chat, public configuration, and ordinary logs.

## 🔄 Synthesis procedure

1. Normalize the text and read an atomic secret snapshot.
2. Calculate the cache identity from the text, region, and all voice settings.
3. Return a valid cached MP3 when available.
4. Create SSML with XML escaping.
5. Request MP3 output from Azure Speech.
6. Validate the response and write the cache atomically.
7. Return a OneBot voice message segment.

A keyed lock combines concurrent requests for the same cache identity. A maximum of two Azure requests can run concurrently.

## 🔐 Response and cache limits

| Item | Limit |
| --- | ---: |
| Text | 500 characters |
| Total Azure timeout | 60 seconds |
| One audio file | 10 MiB |
| Cache | 2048 entries, 256 MiB, 7 days |

Checks validate HTTP status, MIME type, decoded bytes, and the MP3 header. Cache files are in this directory:

```text
data/voice/audio/
```

The cache key contains all inputs that affect the audio. Cache reads validate regular-file status, size, and the MP3 header.
The plugin removes invalid entries and generates them again.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Missing subscription key | Examine `plugins.voice.subscription_key`. |
| Azure authentication error | Examine the key and `region`. |
| SSML voice error | Examine support for `voice_name`, `style`, and `role`. |
| Proxy connection failure | Examine the proxy URL and network permissions. |
| Rejected service call | Examine the Manifest service caller list. |
| Cache write failure | Examine `data/voice/audio/` permissions and disk space. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/voice/test_voice.py \
  tests/core/test_app_plugin_capabilities.py
python -m ruff check plugins/voice tests/plugins/voice/test_voice.py
python -m mypy plugins/voice
```

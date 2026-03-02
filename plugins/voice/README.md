# Voice 插件使用说明

## 功能概述

Voice 插件提供了文字转语音(TTS)和语音转文字(STT)功能，使用 Azure 认知服务实现。

## 配置

### 1. 在 `config/secrets.json` 中添加 voice 配置：

```json
"voice": {
    "subscription_key": "你的Azure订阅密钥",
    "region": "southeastasia",
    "voice_name": "zh-CN-XiaomoNeural",
    "style": "cheerful",
    "role": "Girl"
}
```

配置说明：
- `subscription_key`: Azure 认知服务订阅密钥（必需）
- `region`: Azure 服务区域，TTS 使用 southeastasia，STT 使用 eastasia（可选，默认 southeastasia）
- `voice_name`: 语音名称（可选，默认 zh-CN-XiaomoNeural）
- `style`: 语音风格（可选，默认 cheerful）
- `role`: 角色设定（可选，默认 Girl）

### 2. 配置 smalltalk 语音转换概率

在 `config/config.json` 中添加 smalltalk 配置：

```json
"plugins": {
    "smalltalk": {
        "voice_probability": 0.2
    }
}
```

- `voice_probability`: 闲聊回复转换为语音的概率（0-1之间，默认 0.2 即 20%）

## 使用方法

### 1. 命令方式使用 TTS

直接调用语音命令：

```
语音 你好，我是小青
/tts 今天天气真好
念 这是一条语音消息
```

### 2. 闲聊自动转语音

当你和小青闲聊时，有 20% 的概率（可配置）会收到语音回复而不是文字回复。

示例：
```
用户：小青，今天天气怎么样？
小青：[语音消息] （有20%概率）
小青：今天天气不错哦~ （有80%概率）
```

### 3. 在其他插件中使用

其他插件可以调用 voice 插件的功能：

```python
from plugins.voice import main as voice_plugin

# 将文字转换为语音消息段
voice_reply = await voice_plugin.convert_text_to_voice("你好", context)
```

## 文件结构

```
XiaoQing/plugins/voice/
├── __init__.py           # 插件初始化
├── main.py              # 主要功能实现
├── plugin.json          # 插件配置
└── data/
    └── audio/           # 生成的音频文件（自动创建）
```

## 功能特性

1. **文字转语音(TTS)**
   - 使用 Azure 认知服务 TTS API
   - 支持 SSML 标签自定义语音风格
   - 自动缓存生成的音频文件（基于文本内容哈希）
   
2. **语音转文字(STT)**
   - 使用 Azure 认知服务 STT API
   - 支持详细识别结果
   - 返回分词文本和完整文本

3. **与 smalltalk 集成**
   - 可配置的语音转换概率
   - 自动将文字回复转换为语音
   - 转换失败时自动降级为文字

## 注意事项

1. 需要有效的 Azure 订阅密钥
2. 生成的音频文件会缓存在 `data/audio/` 目录下
3. 音频文件命名基于文本内容的 MD5 哈希值
4. 语音转换是异步的，不会阻塞其他功能
5. 如果语音转换失败，会自动返回文字消息

## 常见问题

**Q: 为什么语音生成失败？**
A: 检查以下几点：
- Azure 订阅密钥是否正确
- 网络连接是否正常
- 区域设置是否正确

**Q: 如何调整语音转换的概率？**
A: 在 `config/config.json` 中修改 `plugins.smalltalk.voice_probability` 的值（0-1之间）

**Q: 如何更换语音？**
A: 在 `config/secrets.json` 中修改 `voice_name`，可用的语音列表参考 Azure 文档

**Q: 音频文件占用太多空间怎么办？**
A: 可以定期清理 `XiaoQing/plugins/voice/data/audio/` 目录下的缓存文件

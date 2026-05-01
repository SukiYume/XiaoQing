# xiaoqing_chat

`xiaoqing_chat` 是 XiaoQing 的拟人聊天插件。它是一个接入群聊上下文、记忆、行为规划、多模态消息和回复检查的聊天运行时。插件目标是让小青在 QQ 群里更像真实群友，能理解上下文，判断回复和沉默时机，接住文字、图片、表情和 reply 引用，并在必要时用图片、表情包或 QQ face 表达。

## 文档定位

本 README 是 `xiaoqing_chat` 的使用手册，重点说明入口、触发、配置、多模态能力、测试和排障。工程结构、模块职责和回复链路见 `plugins/xiaoqing_chat/ARCHITECTURE.md`。

## 入口与运行方式

插件提供显式命令入口，也可以作为全局闲聊提供者接管普通群聊消息。日常使用里，显式命令适合点名聊天，smalltalk_provider 模式适合让小青自然参与群聊。

| 入口 | 说明 |
| --- | --- |
| `/xc <内容>` | 显式和小青聊天，直接进入 forced 回复路径 |
| `/xc help` | 查看插件帮助 |
| `/xc 清空` | 清空当前 chat 的短期上下文、目标、PFC 状态、heartflow 和 action history |
| `/xc 统计` | 查看当前会话统计 |
| `/xc 配置` | 查看当前插件配置概要 |
| `/xc 记忆 <关键词>` | 检索长期记忆 |
| `/xc 表达` | 查看表达学习结果 |
| `/xc 黑话` | 查看学习到的黑话、缩写和表达解释 |
| `/xc 模型 [provider]` | 查看或切换聊天 LLM provider，切换 provider 需要管理员权限 |

小青参与普通群聊时，需要设置全局配置。

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

在这个模式下，框架会把所有群聊消息交给插件观察。插件内部的 attention gate、频率控制、PFC planner、主 LLM 和 reply checker 决定是否真的回复。全局 `random_reply_rate` 不再决定消息是否进入插件。

## 回复决策模型

`xiaoqing_chat` 把“明确是在叫小青”和“普通群聊是否插话”分开判断。

### Attention Gate

以下消息会被视为 directed attention，跳过普通插话概率，进入 forced 回复路径。

- `/xc <内容>` 或其它显式命令路径。
- 私聊普通消息；如果开启 `brain_chat.enable_private_brain_chat`，会使用深度对话模式。
- 群聊中 `@` 小青。
- 群聊文本直接包含 bot name，例如 `小青 你看看`。
- 用户只喊 bot name 后，同一用户短时间内继续追问。
- reply 引用的是小青上一条回复。
- 最近上下文已经锚定小青时，当前消息用 `她/他/ta` 等共指召唤，例如 `不@她能不能听见啊`、`她会不会回啊`。

共指触发要求最近历史里有小青锚点。没有上下文的普通“她”“ta”不会强行触发，以避免普通群友聊天被误判。

### 普通 Participation Gate

没有 directed attention 的群聊消息会进入普通参与判断。普通参与路径会综合以下因素。

- 最小回复间隔、每分钟最大回复数、连续回复冷却。
- 基础插话概率 `reply_probability_base`。
- 活跃话题状态：近期目标仍活跃时，可以使用更短的 `active_topic_min_reply_interval`。
- heartflow 软评分：问题、goal match、短文本惩罚、连续未回复、长时间沉默等。
- PFC planner：普通群聊中判断下一步动作是回复、观察、等待还是调整目标。

私聊和点名由 attention gate 标记为 directed attention，不通过提高普通概率实现。硬频控由 `frequency_control.py` 先执行；heartflow 不重复承担点名、私聊和速率限制语义。

## 一次回复的主链路

真实回复一般经过以下步骤。

1. `observe_message()` 观察消息，保留 OneBot 原始消息段。
2. 插件按原始 segment 顺序重建有效用户输入。
3. attention gate 判断是否 forced。
4. 普通群聊消息进入 `_should_reply()` 做硬频控和插话概率判断。
5. 非 forced 且 planner 开启时进入 PFC planner；forced/direct 场景直接进入回复生成。
6. 主回复 LLM 读取最近历史、相关记忆、当前目标、人物资料、表达习惯和媒体上下文。
7. 主回复可以输出纯文本，也可以附带一个媒体 marker：`[想发表情:hint]`、`[想发QQ表情:hint]` 或 `[想发图片:hint]`。
8. marker resolver 从本地图库、历史媒体或 QQ face catalog 中解析实际 OneBot 消息段。
9. reply checker 做启发式检查和可选 LLM 检查，过滤重复、违和、过度连续等回复。
10. 通过检查后写回短期记忆、长期记忆、action history、heartflow 和目标状态，再返回 OneBot 消息段。

## 多模态能力

### 入站消息

插件能接收并理解以下 OneBot segment。

- `text`: 普通文本。
- `at`: 群聊点名。
- `reply`: 引用回复，用于判断 reply-to-bot 和上下文。
- `face`: QQ 原生表情。
- `mface`: NapCat 表情包。
- `image`: 普通图片。
- 混合消息：文字、图片、表情和 reply 按原始顺序组合。

启用 `media.enable_inbound_media_context` 后，图片和表情会被渲染为上下文 marker。普通图片可调用视觉模型获取描述；识别为表情包的图片会写入 `plugins/xiaoqing_chat/data/media/library/`，后续可作为出站素材。

### 出站消息

主 LLM 不直接构造 OneBot JSON。它会在自然回复中写一个媒体意图 marker。

```text
哈哈这个太离谱了 [想发表情:笑哭]
```

插件解析 marker 后再转换为实际消息段。

- `[想发表情:hint]`: 匹配本地表情包库。
- `[想发QQ表情:hint]`: 匹配 QQ face catalog。
- `[想发图片:hint]`: 匹配图片库或历史图片素材。

如果 marker 无法解析，插件会安全降级为纯文本，不阻塞当前回复。

## 记忆、目标和表达学习

`xiaoqing_chat` 有多层上下文。

- 短期历史：当前 chat 最近消息，用于连续对话、reply checker 和共指判断。
- 语义记忆：向量检索相关长期记忆，默认由 `memory.top_k` 和 `memory.min_score` 控制。
- 人物资料：为用户积累昵称、偏好和事实印象。
- topic summary：为较长对话维护话题摘要缓存。
- goal state：记录当前聊天目标和活跃话题。
- PFC state：维护 planner 的观察、行动和反思状态。
- expression store：学习群友表达、黑话和口癖，供回复 prompt 选择性注入。

这些状态按 chat_id 隔离。群聊、私聊和不同群之间不会共享短期上下文；长期记忆仍受插件自己的检索和写入策略约束。

## 配置

行为配置位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`。聊天和视觉模型 provider 位于项目级 `config/secrets.json`。

常用行为项如下。

```json
{
  "enable_smalltalk": true,
  "reply_probability_base": 0.72,
  "min_reply_interval_seconds": 3,
  "active_topic_min_reply_interval": 3.0,
  "max_replies_per_minute": 6,
  "continuous_reply_limit": 5,
  "continuous_cooldown_seconds": 12,
  "max_context_size": 30,
  "planner": {
    "enable_planner": true,
    "think_mode": "dynamic"
  },
  "memory": {
    "enable_memory_retrieval": true,
    "top_k": 5,
    "min_score": 0.12
  },
  "reply_check": {
    "enable_reply_checker": true,
    "enable_llm_checker": true,
    "max_regen": 2,
    "max_replan": 2
  },
  "heartflow": {
    "enable_heartflow": true,
    "base_score": 0.2
  },
  "media": {
    "enable_inbound_media_context": true,
    "max_media_per_message": 1,
    "vision_provider": ""
  }
}
```

模型配置示例如下。

```json
{
  "plugins": {
    "xiaoqing_chat": {
      "default": "deepseek",
      "providers": {
        "deepseek": {
          "api_base": "https://api.deepseek.com",
          "api_key": "sk-xxx",
          "model": "deepseek-chat",
          "endpoint_path": "/v1/chat/completions",
          "proxy": ""
        }
      }
    }
  }
}
```

`/xc 模型` 会显示当前 provider；管理员可以用 `/xc 模型 <provider>` 切换到已配置 provider。

## 数据目录

运行时数据默认位于 `plugins/xiaoqing_chat/data/`。常见内容包括以下几类。

- `media/library/`: 学到的表情包和图片素材。
- `media/render_cache.json`: 媒体描述缓存。
- 记忆、人物资料、表达学习和 planner 状态文件。
- 测试报告和实验输出通常位于 `plugins/xiaoqing_chat/test_reports/`。

这些数据属于本地运行时状态，不应作为源码提交。

## 测试和实验

常用回归命令如下。

```powershell
python -m pytest tests/plugins/test_xiaoqing_chat.py -q
python -m pytest tests/plugins/test_xiaoqing_chat_media.py -q
python -m pytest tests/plugins/test_reply_checker.py -q
python -m pytest tests -k "xiaoqing or reply_checker" -q
```

拟人大群实验 runner 命令如下。

```powershell
python -m plugins.xiaoqing_chat.experiments.anthropomorphic_group --mode real --run-id <RUN_ID> --groups 20 --rounds-per-group 150
```

实验 runner 走真实 `observe_message()` 和 `handle_smalltalk()` 主路径，覆盖接收、上下文、触发、频控、PFC/直接回复、主 LLM、reply checker 和 OneBot 消息段返回。runner 不会把回复 POST 到 live OneBot HTTP 网关。

全量测试任务说明见 `plugins/xiaoqing_chat/xiaoqing_chat测试.md`。测试说明是执行 prompt，不作为插件手册。日常维护时先读本 README 和 `ARCHITECTURE.md`。

## 排障

**群里不回复**

1. 确认 `config/config.json` 中 `plugins.smalltalk_provider` 是 `xiaoqing_chat`。
2. 如果消息是普通闲聊，检查 `reply_probability_base`、硬频控、连续回复冷却和 planner 决策。
3. 如果消息是 directed attention，检查是否包含 `@`、bot name、reply-to-bot 或上下文锚定共指。
4. 查看日志中的 attention gate 和 reply gate 字段。

**图片或表情没有进入上下文**

1. 确认 `media.enable_inbound_media_context` 为 `true`。
2. 检查 OneBot 实现是否真的发送 `image`、`face` 或 `mface` segment。
3. 检查视觉 provider 是否配置；未配置时图片会保守降级为 marker。

**LLM 不可用**

1. 检查 `config/secrets.json` 中 provider 的 `api_base`、`api_key`、`model` 和 `endpoint_path`。
2. 用 `/xc 模型` 查看当前 provider。
3. 检查代理和网络；请求失败时插件会按 retry 配置重试。

**回复太频繁或太少**

1. directed attention 会强制回复，普通概率只影响非点名群聊。
2. 降低或提高 `reply_probability_base`。
3. 调整 `min_reply_interval_seconds`、`max_replies_per_minute`、`continuous_reply_limit` 和 `continuous_cooldown_seconds`。
4. 如需减少主动参与，可关闭 planner 或提高硬频控限制。

## 相关文档

- `plugins/xiaoqing_chat/ARCHITECTURE.md`
- `plugins/xiaoqing_chat/xiaoqing_chat测试.md`
- `docs/08-message-flow.md`
- `docs/09-plugins.md`

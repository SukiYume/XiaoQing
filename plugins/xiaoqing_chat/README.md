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
| `/xc 深度` | 查看私聊深度对话模式状态 |
| `/xc 配置` | 查看当前插件配置概要 |
| `/xc 记忆 <关键词>` | 检索长期记忆 |
| `/xc 表达` | 查看表达学习结果 |
| `/xc 黑话` | 查看学习到的黑话、缩写和表达解释 |
| `/xc 模型 [别名]` | 查看 route 中的模型 profile，或由管理员严格固定到指定 profile；`默认` 恢复自动 fallback |
| `/xc 审查 <操作> <会话ID> [内容]` | 由管理员处理目标或表达反思会话 |

小青参与普通群聊时，需要设置全局配置。

```json
{
  "plugins": {
    "smalltalk_provider": "xiaoqing_chat"
  }
}
```

在这个模式下，框架会把所有群聊消息交给插件观察。插件内部的 attention gate、频率控制、PFC planner、主 LLM 和 reply checker 决定是否真的回复。

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
- 明确面向全群的问题、邀请和开场话由 `participation_cue_reply_probability` 提高进入概率；通过硬频控后直接进入回复，避免 planner 再因“没有点名”否决。明确点给其他人的消息不属于该信号。
- 只有 OneBot `face` 协议码和标点的低信息消息直接保持安静，不启动完整规划链。
- 活跃话题状态：只使用上一轮目标判断；普通延续由 `active_topic_reply_probability` 与 `active_topic_min_reply_interval` 控制，明确追问再使用独立的 question 概率和短间隔。当前消息不会先把自己标成活跃话题。
- heartflow 软评分：问题、goal match、短文本惩罚、连续未回复、长时间沉默等。
- PFC planner：普通群聊中判断下一步动作是回复、观察、等待还是调整目标。

私聊和点名由 attention gate 标记为 directed attention，不通过提高普通概率实现。硬频控由 `frequency_control.py` 先执行；heartflow 不重复承担点名、私聊和速率限制语义。

## 一次回复的主链路

真实回复一般经过以下步骤。

1. `observe_message()` 观察消息，保留 OneBot 原始消息段。
2. 插件按原始 segment 顺序重建有效用户输入。
3. 若本轮与上一条消息的间隔超过 `memory.conversation_idle_gap_seconds`，插件保留原始历史，但清除旧目标、PFC、action history 和话题摘要；即时生成只读取空档后的当前会话片段。
4. attention gate 判断是否 forced。
5. 普通群聊消息进入 `_should_reply()` 做硬频控和插话概率判断；点给其他人的消息和单独协议表情直接沉默，面向全群的开放话题获得独立参与信号。
6. 非 forced 且 planner 开启时进入 PFC planner；已通过硬频控的开放群聊邀请可以直接进入回复生成，forced/direct 场景也直接进入回复生成。
7. 主回复 LLM 读取当前会话片段、相关记忆、当前目标、人物资料、表达习惯和媒体上下文。
8. 主回复可以输出纯文本，也可以附带一个媒体 marker：`[想发表情:hint]`、`[想发QQ表情:hint]` 或 `[想发图片:hint]`。
9. marker resolver 从本地图库、历史媒体或 QQ face catalog 中解析实际 OneBot 消息段。
10. reply checker 先做确定性检查，再对人物、第三方事实、明确交流约束和媒体语义等风险候选做独立语义审查。配置允许时，普通、低风险且符合稳定人设的日常小经历可以没有历史证据；精确身份、重大经历、现实承诺、真实群友和外部事实仍受严格边界约束。硬错误会重生成；主动插话的坏候选在耗尽后快速沉默，明确点名场景使用安全承接，不会整轮无响应。
11. 通过检查后写回短期记忆、长期记忆、action history、heartflow 和目标状态，再返回 OneBot 消息段。

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

启用 `media.enable_inbound_media_context` 后，图片和表情会被渲染为上下文 marker。普通图片可调用视觉模型获取描述；识别为表情包的图片会写入 `data/xiaoqing_chat/media/library/`，后续可作为出站素材。

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

- 短期历史：原始消息按 chat 完整保留；即时回复、PFC 和思考级别只使用最近一次长空档后的连续会话片段。默认空档阈值为 1800 秒。
- 语义记忆：向量检索相关长期记忆，默认最多 3 条、1200 字符，由 `memory.top_k`、`memory.min_score` 和 `memory.max_block_chars` 控制。
- 直接检索未命中时，只有明确回指既往聊天或人物稳定信息的消息才启动记忆工具代理；普通问题不会为无关检索额外等待。
- 人物资料：为用户积累昵称、偏好和事实印象。
- 小青人设：稳定身份和边界由 `personality.identity` 定义；`allow_low_stakes_persona_fiction` 默认允许符合人设的普通日常小故事，不允许扩展精确现实资料或替真实人物补事实。
- topic summary：为较长对话维护话题摘要缓存。
- goal state：记录当前聊天目标和活跃话题。
- PFC state：维护 planner 的观察、行动和反思状态。
- expression store：学习群友表达、黑话和口癖；学习与使用分开，默认不注入，只有人工审核并主动开启选择器后才会每轮最多注入一条。

这些状态按 chat_id 隔离。群聊、私聊和不同群之间不会共享短期上下文；长期记忆仍受插件自己的检索和写入策略约束。

## 配置

行为配置位于 `plugins/xiaoqing_chat/config/xiaoqing_config.json`。聊天和视觉模型共用项目级注册表：公开连接与模型 profile 位于 `config/config.json`，只有 API Key 位于 `config/secrets.json`。

常用行为项如下。

```json
{
  "enable_smalltalk": true,
  "reply_probability_base": 0.55,
  "participation_cue_reply_probability": 0.9,
  "active_topic_reply_probability": 0.6,
  "active_topic_question_reply_probability": 0.9,
  "min_reply_interval_seconds": 8,
  "active_topic_min_reply_interval": 3.0,
  "active_topic_question_min_reply_interval": 2.0,
  "max_replies_per_minute": 4,
  "continuous_reply_limit": 3,
  "continuous_cooldown_seconds": 25,
  "max_context_size": 30,
  "planner": {
    "enable_planner": true,
    "think_mode": "dynamic"
  },
  "memory": {
    "enable_memory_retrieval": true,
    "conversation_idle_gap_seconds": 1800,
    "top_k": 3,
    "min_score": 0.12,
    "max_block_chars": 1200,
    "agent_on_direct_miss_requires_reference": true
  },
  "personality": {
    "allow_low_stakes_persona_fiction": true
  },
  "reply_check": {
    "enable_reply_checker": true,
    "enable_llm_checker": true,
    "llm_checker_mode": "risk",
    "timeout_seconds": 5.0,
    "max_tokens": 512,
    "max_regen": 1,
    "max_replan": 1
  },
  "heartflow": {
    "enable_heartflow": true,
    "base_score": 0.2
  },
  "expression": {
    "enable_expression_learning": true,
    "enable_expression_selector": false,
    "max_injected": 1
  },
  "media": {
    "enable_inbound_media_context": true,
    "max_media_per_message": 1
  }
}
```

### 远程模型数据说明

配置远程聊天或视觉 provider 后，插件处理消息时会发送当前输入及生成回复所需的聊天历史、记忆、人物资料、规划结果和媒体上下文。部署者应按所选 provider 的条款管理数据用途、保留和删除策略。普通日志不会写入完整 prompt、响应或凭据。

`config/config.json` 中配置可复用 provider、model profile 和本插件的有序 route：

```json
{
  "ai": {
    "providers": {
      "deepseek": {
        "api_base": "https://api.deepseek.com",
        "endpoint_path": "/chat/completions"
      },
      "zhipu": {
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "endpoint_path": "/chat/completions"
      }
    },
    "models": {
      "deepseek-flash": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"],
        "request_defaults": {"thinking": {"type": "disabled"}}
      },
      "deepseek-flash-thinking": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "modalities": ["text"],
        "request_defaults": {
          "thinking": {"type": "enabled"},
          "reasoning_effort": "high"
        }
      },
      "glm-5.2": {
        "provider": "zhipu",
        "model": "glm-5.2",
        "modalities": ["text"]
      },
      "glm-4.6v-flash": {
        "provider": "zhipu",
        "model": "glm-4.6v-flash",
        "modalities": ["text", "image"]
      }
    }
  },
  "plugins": {
    "xiaoqing_chat": {
      "ai": {
        "default_model_alias": "deepseek",
        "model_aliases": {
          "deepseek": "deepseek-flash",
          "glm": "glm-5.2"
        },
        "routes": {
          "chat": {
            "models": ["deepseek-flash", "glm-5.2"]
          },
          "checker": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"]
          },
          "reasoning": {
            "models": ["deepseek-flash-thinking", "deepseek-pro", "glm-5.2"]
          },
          "vision": {
            "models": ["glm-4.6v-flash"]
          }
        }
      }
    }
  }
}
```

`config/secrets.json` 只保存同名 provider 的密钥：

```json
{
  "ai": {
    "providers": {
      "deepseek": {"api_key": "your-deepseek-api-key"},
      "zhipu": {"api_key": "your-zhipu-api-key"}
    }
  }
}
```

四个 route 都按从前到后的顺序 fallback：普通闲聊使用关闭思考的低延迟 `deepseek-v4-flash`；数值、单位和科学关系使用开启思考的 Flash，再向 `deepseek-v4-pro` 降级；图片理解走 `vision`。checker 仍使用独立 route 避免主模型自审，但请求会显式关闭思考并限制为 512 token，只在风险模式命中时调用。推理模式的 token 额度同时覆盖隐藏思考与最终答案，因此主科学回复至少使用 2048。DeepSeek 的 `low`/`medium` 思考强度会被映射到 `high`，提速应切换 Flash 或关闭思考，而不是填写低强度。`/xc 模型` 只控制聊天 profile；管理员显式选择别名后会固定主回复模型，使用 `/xc 模型 默认` 才恢复自动路由。GLM provider 使用标准按量 API，Coding Plan 专属端点不适用于这里。

远程 checker 是风险质量增强层：重复、连续刷屏、媒体错位、第三方事实和群体状态等确定性规则仍会 fail-closed；人物日常创作开启后，由语义检查区分普通小经历与精确身份、重大经历或现实承诺。远程 checker 自身超时、不可用或返回无效 JSON 时，在本地确定性检查通过后受控 fail-open，避免每次人物化表达都因审查服务故障消失。`xiaoqing_chat` 故障时不会回退到无状态的通用闲聊 provider 冒充小青回复。

## 数据目录

运行时数据由框架限定在 `context.data_dir`（默认 `data/xiaoqing_chat/`）。常见内容包括以下几类。

- `media/library/`: 学到的表情包和图片素材。
- `media/render_cache.json`: 媒体描述缓存。
- 记忆、人物资料、表达学习、媒体注册表和 planner 状态文件。

这些内容属于本地运行时状态，不应作为源码提交。Xiaoqing Chat 专项测试报告和
实验输出统一写入项目级 `test_reports/runs/plugins/xiaoqing_chat/`，不放在插件源码目录中。

## 测试和实验

常用回归命令如下。

```powershell
python -m pytest tests/plugins -k "xiaoqing_chat or reply_checker" -q
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
3. 检查 `xiaoqing_chat.ai.routes.vision` 及其中 profile 的 `image` 模态；未配置时图片会保守降级为 marker。

**LLM 不可用**

1. 检查 `config.ai.providers`、`config.ai.models` 与 `plugins.xiaoqing_chat.ai.routes` 的引用。
2. 检查 `secrets.ai.providers.<provider>.api_key`，不要把连接字段放回插件 secrets。
3. 用 `/xc 模型` 查看 profile；若此前手动固定过模型，可用 `/xc 模型 默认` 恢复 fallback。
4. 检查代理和网络；网络、限流和服务端故障会按 route 重试或切换，认证和参数错误会直接暴露以便修正。

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

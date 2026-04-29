# XiaoQing Chat 拟人聊天插件简化设计

日期：2026-04-27
范围：`plugins/xiaoqing_chat`

## 背景

`xiaoqing_chat` 是一个拟人群聊对话插件。最朴素的诉求：bot 的回复要让人分辨不出是 AI，收发都支持文本、图片、QQ 系统表情。

当前实现承载了这个目标，但在演进过程中堆叠了较多的"魔法 fallback"和针对特定场景的优化层，主要问题集中在：

- 一轮对话最坏要打 5~8 次 LLM 调用：主生成、rewrite、3 个媒体 selector、emoji 二次验证、reply_checker
- 三个媒体类型（image / emoji / qq_face）各有一个独立 planner 文件，结构 90% 同构
- 候选库每条消息都从磁盘全量加载（含文件遍历 + 哈希 + 索引回写）
- 大量硬编码常量与表（频控时段表、兴趣词集合、同形字典、兜底回复池、单插件白名单等）

## 目标

1. 减少每轮 LLM 调用数到稳定 2 次（主生成 + checker），出站延迟和成本同步下降
2. 把"出站媒体决策"的责任从外挂 selector 完全收回给主 LLM，候选库代码退化为本地查找
3. 候选库加进程内缓存，稳态零磁盘 IO
4. 清理一批已无必要的硬编码常量与魔法路径
5. 入站 vision 解析、reply_checker 的 LLM 检查、PFC 规划骨架不动

## 非目标

- 入站 vision 解析的多 provider fallback 链不在本次范围
- PFC engine 内部逻辑、heartflow 评分公式、记忆/表达学习子系统不在本次范围
- 不引入新的外部依赖；不更换 LLM provider
- 不做 UI/前端改动

## 改造后的回复主流程

```
入站事件
 → 媒体解析（vision，三层 provider fallback 保留）
 → 频控判定（基础概率 × heartflow × 静默期补偿；删除 talk_schedule、_HIGH_WORDS、interest_adjust）
 → PFC 规划
 → 主 LLM 生成（系统提示扩充：拟人腔指引 + marker 语法说明）
     输出形如：「哈哈我也觉得 [想发表情:笑哭]」 或纯文本
 → pre-heuristic 本地兜底（空 / 全重复 / 连发，命中即重生成；rewrite 层删除）
 → marker 解析（轻量正则）
     ├─ 命中 marker → 按 hint 在候选库里找最贴合项；找不到则当作纯文本继续
     └─ 无 marker → 直接进入 checker
 → 后处理（句尾截断等；同形字 typo 注入删除）
 → reply_checker（保持 LLM 检查，prompt 仍把 marker/媒体作为整体看）
     不通过 → 视 need_replan 走 regen 或 PFC replan
 → 出站（媒体 marker 在发送层渲染成实际 face/image/emoji）
```

关键变化：
- LLM 调用：最坏 5~8 → 稳定 2（主 + checker），加上入站 vision 不变
- rewrite LLM、3 个 media selector LLM、emoji 二次验证 LLM 全部删除
- 媒体决策权完全收给主 LLM，候选库代码退化为"按 hint 查找"的纯本地操作

## marker 协议

### 语法

```
[想发表情:hint]      ← 表情包（自定义图片表情）
[想发QQ表情:hint]    ← QQ 系统 face
[想发图片:hint]      ← 普通图片
```

约定：

- `hint` 任意中英文短词，最长 12 字。LLM 可写情绪词（"笑哭"）、画面词（"猫举手"）、或候选库高频标签
- 一次回复最多带一个 marker；多写了只取第一个
- marker 位置不限，解析后从文本剥除，由发送层决定文字与媒体的拼接顺序
- LLM 没有想发媒体时不写 marker（不是"显式 none"）

### 系统提示新增片段

`prompt_builder.py` 的 system prompt 末段追加：

> 你可以在合适的时候为这条回复挂一个媒体——表情包、QQ 系统表情或图片。挂法是在文本里加一个 marker：`[想发表情:简短描述]`、`[想发QQ表情:简短描述]`、`[想发图片:简短描述]`。每条回复最多挂一个；不挂就不写。挂的前提是这个媒体能为这条回复加一层语气、情绪或调侃，单纯复读情绪没必要挂。`简短描述`写最贴近你想要的感觉的词，比如"笑哭""猫举手""离谱"，候选库会按描述去查最匹配的项，找不到就当没挂。

### 解析模块

新增 `plugins/xiaoqing_chat/media/marker_resolver.py`：

```python
@dataclass(frozen=True)
class ParsedMarker:
    kind: Literal["emoji", "qq_face", "image"]
    hint: str
    raw_span: tuple[int, int]    # 在原文里的 [start, end)

@dataclass(frozen=True)
class ResolvedMarker:
    kind: Literal["emoji", "qq_face", "image"]
    hint: str
    raw_span: tuple[int, int]
    entry: Any                    # 候选库实体

def parse_marker(text: str) -> Optional[ParsedMarker]: ...
async def resolve_marker(parsed, *, context, runtime) -> Optional[ResolvedMarker]: ...
def strip_marker(text: str, span: tuple[int, int]) -> str: ...
```

行为：

1. 正则 `\[想发(表情|QQ表情|图片)[:：]([^\]]{1,12})\]`，全/半角冒号都吃
2. 同一文本多个 marker 只取第一个
3. `resolve_marker` 按 kind 路由到对应库，hint 走 `find_candidate_by_hint`
4. 解析失败 / hint 无效 / 候选库无匹配 → 返回 `None`，文本保留 marker 字面；出站前再做一次"删除任何 `[想发*]` 残留"的兜底清理

### 与 reply_checker 的衔接

`reply_checker` 现有 prompt 已经把 `[表情包：...]` / `[QQ表情：...]` 当作回复一部分来检查。新版在 `marker_resolver` 内把 `[想发*:hint]` 替换成最终 marker 形式（`[表情包：xxx]` / `[QQ表情：xxx]` / `[图片：xxx]`），再送 checker。checker prompt 不用动。

## 模块层面的增删改

### 删除

| 路径 | 处理 |
|---|---|
| `plugins/xiaoqing_chat/media/emoji_reply.py` | 整个文件删除 |
| `plugins/xiaoqing_chat/media/qq_face_reply.py` | 整个文件删除 |
| `plugins/xiaoqing_chat/media/image_reply.py` | 整个文件删除 |
| `plugins/xiaoqing_chat/media/reply_planner_common.py` | 整个文件删除；保留的工具函数（`extract_inbound_marker_labels`、`tokenize_media_text`、`find_candidate_by_hint`）迁入 `marker_resolver.py` |
| `plugins/xiaoqing_chat/reply_media_helpers.py` | 整个文件删除；功能并入 `marker_resolver.py` |
| `plugins/xiaoqing_chat/llm/postprocess.py` | 删除 `_HOMOGLYPH_REPLACE` 及调用它的 typo 注入函数 |
| `plugins/xiaoqing_chat/frequency_control.py` | 删除 `_HIGH_WORDS`、`_DEFAULT_TALK_SCHEDULE`、`interest_adjust` 计算段、talk_value 时段倍率段。频控只剩基础概率 × heartflow × 静默期补偿 |
| `plugins/xiaoqing_chat/handlers.py` | 删除 `_NOISY_EXTERNAL_SOURCE_PLUGINS` 常量；两组兜底回复池删一组、剩一组迁配置 |
| `plugins/xiaoqing_chat/reply_generator.py` | 删除 rewrite 层（`_run_rewrite_pass` 之类）；删除 `_run_forced_media_fallback`；删除 `_attach_reply_media` 的并行三 planner 调用 |

### 新增

| 路径 | 用途 |
|---|---|
| `plugins/xiaoqing_chat/media/marker_resolver.py` | marker 解析 + 候选库查找；并入原 reply_planner_common 与 reply_media_helpers 的剩余功能 |

可选：persona 指令片段如果有体量，可单拆 `plugins/xiaoqing_chat/llm/persona_directives.py`；否则直接写进 `prompt_builder.py`。

### 改造

| 路径 | 改造点 |
|---|---|
| `plugins/xiaoqing_chat/media/emoji_library.py` | 加 mtime + index 文件 mtime 双签名缓存层；mutating 函数（`mark_*_used` 等）落盘后失效缓存项 |
| `plugins/xiaoqing_chat/media/qq_face_catalog.py` | 同上 |
| `plugins/xiaoqing_chat/llm/prompt_builder.py` | 系统提示加入：(a) 反 AI 腔指引（继承自原 rewrite 层的诉求）；(b) marker 语法说明 |
| `plugins/xiaoqing_chat/reply_generator.py` | `_generate_reply_draft` 改为：主生成 → pre-heuristic → marker 解析 → 后处理 → checker。`_attach_reply_media` 改名 `_attach_reply_media_marker`，只做 marker 解析+查表 |
| `plugins/xiaoqing_chat/llm/reply_checker.py` | prompt 不变；输入端不再有"独立 plan 拼回去"的歧义路径 |
| `plugins/xiaoqing_chat/config/config.py` | `MediaConfig` 删除 `enable_outbound_{image,emoji,face}_reply`、`{image,emoji,face}_reply_probability`、`{image,emoji,face}_cooldown_turns`、`{image,emoji,face}_candidate_count` 共 12 个字段；`max_media_per_message` 默认从 3 改为 1（marker 协议每轮最多一个）；保留各候选库路径配置；新增 `noisy_external_source_plugins: list[str] = []`、`fallback_idle_replies: list[str]`；删除 `enable_rewrite` / `rewrite_*` 系列；删除 `talk_schedule` 字段。`_HIGH_WORDS` 是代码常量、不是配置字段，随 `frequency_control.py` 的删除一并消失 |
| `plugins/xiaoqing_chat/config/xiaoqing_config.json` | 同步清理 |

### 测试

| 测试文件 | 处理 |
|---|---|
| `tests/plugins/test_xiaoqing_chat_media.py` | 大量重写。selector LLM mock、forced fallback、双重验证等用例全删；新增 marker 解析与降级路径用例（见下） |
| `tests/plugins/test_reply_checker.py` | 基本不动 |
| `tests/plugins/test_xiaoqing_chat.py`、`test_xiaoqing_chat_dedup_json_llm.py`、`test_xiaoqing_chat_review_regressions.py`、`test_xiaoqing_chat_runtime_hardening.py` | 视具体断言调整：用到 rewrite / talk_schedule / 频控兴趣词的断言要清掉 |

测试覆盖底线：

| 场景 | 期望 |
|---|---|
| 主 LLM 输出无 marker | 走纯文本路径，checker 通过即发送 |
| 主 LLM 输出合法 marker、命中候选 | marker 替换为最终 marker、文本剥离、checker 通过 |
| 主 LLM 输出 marker、候选未命中 | 走纯文本路径、不带媒体 |
| 主 LLM 输出畸形 marker（缺括号、非法 kind、hint 超长） | 不解析、文本里残留字符在出站前清理 |
| 主 LLM 输出多个 marker | 只取第一个 |
| 入站带 marker（用户发了表情/图片） | inbound 解析路径不变 |
| pre-heuristic 拦截 | 直接 regen，不进 checker |
| checker 不通过 | 按 need_replan 走 regen 或 PFC replan |
| 候选库缓存命中 | 不触发磁盘读 |
| 候选库目录新增文件 | 下次 load 拿到新条目 |

## 候选库缓存设计

`emoji_library.py` 与 `qq_face_catalog.py` 各加一份模块级缓存：

```python
_LIBRARY_CACHE: dict[str, tuple[tuple[float, float], list[Entry]]] = {}
# key: library_dir 字符串
# value: ((dir_mtime, index_mtime), entries)
```

- `load_*()` 先算当前签名，与缓存里一致就直接返回，不一致才走全量加载
- `mark_*_used()` / `record_*()` 等 mutating 函数落盘后清掉对应缓存项；下次 load 时自然重建
- 用户手动往目录扔新文件：dir mtime 变化能感知到

A2 路线下候选库不再参与排序（原 `_entry_relevance` 用到的 `usage_count` / `last_used_ts` 在选 marker 时不再使用），所以 mutating 函数即使只更新磁盘不更新内存缓存也不影响正确性。

## 错误处理

| 阶段 | 失败 | 处理 |
|---|---|---|
| marker 解析 | 正则不匹配 | 当作无 marker，走纯文本 |
| marker 解析 | 多 marker | 取第一个，其余按字面文本保留 |
| marker 解析 | hint 超长 / 非法 kind | 当作无 marker |
| marker 查表 | 候选库无匹配 | 返回 None；文本剥离 marker、不带媒体发送 |
| 出站前清理 | 文本里仍有 `[想发*` 残留 | 用兜底正则删除整段 |
| 主 LLM | 失败 / 超时 | 沿用现有 `LLMError` 处理（regen / 兜底回复池） |
| reply_checker | 失败 / 超时 | 沿用现有 fail-open 逻辑（放行当前回复） |
| 候选库加载 | mtime stat 失败 | 退回到无缓存路径，全量加载一次 |

## 实施分期

每期一个独立可发布的小步，互不依赖。

### Phase 1 — 候选库缓存层

- `emoji_library.py` / `qq_face_catalog.py` 各加 mtime 缓存层
- mutating 函数加缓存失效钩子
- 行为不变，只看响应延迟和磁盘 IO 是否下降
- 单元测试：缓存命中路径、mtime 变化触发重载、mutating 后下次 load 拿到新数据

### Phase 2 — 删除 frequency_control 硬编码段 + 删除 homoglyph + 收窄 noisy plugin

- 这几项纯删除/迁配置，不依赖 marker 协议
- 删 `_HIGH_WORDS` / `_DEFAULT_TALK_SCHEDULE` / `interest_adjust` / talk_value 时段倍率
- 删 `_HOMOGLYPH_REPLACE` 及调用
- `_NOISY_EXTERNAL_SOURCE_PLUGINS` 改成 `cfg.noisy_external_source_plugins: list[str]`
- 配置层删除对应字段，xiaoqing_config.json 同步
- 测试：清掉相关断言，新增"频控只剩基础概率链"的回归

### Phase 3 — 引入 marker 协议（只读，未启用）

- 新增 `marker_resolver.py`（含从 `reply_planner_common.py` 迁过来的工具函数）
- 新增 marker 解析 / 解析后替换为最终 marker / 出站前残留清理三段函数
- 单元测试：合法 marker、多 marker、畸形 marker、命中失败、残留清理
- 这一期主流程不调用，纯加载验证

### Phase 4 — 切换主流程到 marker 协议

- `prompt_builder.py` 系统提示加 marker 语法说明 + 反 AI 腔指引
- `reply_generator._generate_reply_draft`：删 rewrite 层、删 `_attach_reply_media` 三 planner 并行调用、删 `_run_forced_media_fallback`，改为 `attach_reply_media_marker(reply_text)` 一行
- `reply_media_helpers.py` 内容并入 `marker_resolver.py`，文件删除
- 三个 planner 文件 `emoji_reply.py` / `qq_face_reply.py` / `image_reply.py` 整体删除
- `reply_planner_common.py` 删除
- `MediaConfig` 大幅瘦身（删 12 个字段，`max_media_per_message` 默认改为 1）
- `tests/plugins/test_xiaoqing_chat_media.py` 重写
- 这一期是最大的一期，单独 PR

### Phase 5 — pre-heuristic 与 checker 共享底层规则

- pre-heuristic 和 checker 启发式分支共享底层规则，抽到 `reply_checker._heuristic_check` 让两边都用
- 删除两组 fallback 回复池中的一组，剩下的迁到 `cfg.fallback_idle_replies`
- 小型清理，最后做

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 主 LLM 不按 marker 语法输出 | 解析失败安全降级（当纯文本发），不会卡流程；Phase 3 提前验证解析鲁棒性 |
| 主 LLM 过度使用 marker（每条都挂） | 系统提示里强调"挂的前提是能为这条回复加层意思"；observability 里加一个 `marker_attach_rate` 指标，必要时调 prompt |
| 主 LLM hint 写得太抽象、命中失败率高 | 灰度时观察 `marker_resolve_miss_rate`；如果偏高，prompt 里给两三个候选库高频标签做 few-shot 示例 |
| 删除 talk_schedule 后某些时段消息密度变化 | Phase 2 单独发，便于回滚；如果不接受可用配置外挂同样曲线 |
| Phase 4 涉及多文件删除，老 import 链断裂 | 全局 grep 清理 import；CI 跑全量 unit test 后再合 |

## Observability

新增 `_log_step` 埋点（写在现有日志里）：

- `reply.marker.parsed`：字段含 kind、hint
- `reply.marker.resolved`：字段含 kind、hint、entry_id
- `reply.marker.miss`：字段含 kind、hint、reason（解析失败 / 候选库无匹配）
- `reply.checker.skip`：当 pre-heuristic 拒绝时

便于上线后判断主 LLM 对 marker 的使用率和命中率。

## 成功判定

- 单轮稳态 LLM 调用数从 5~8 降到 2（出站；入站 vision 不计）
- 所有现存测试通过；marker 相关新测试通过
- `marker_attach_rate` 在合理区间（预期 5%~25%；过低说明 prompt 没把语法讲清楚，过高说明 LLM 滥用）
- `marker_resolve_miss_rate` 不超过 30%（命中失败时降级安全，但偏高意味着 hint 与候选库脱节）
- 候选库稳态磁盘 IO 接近零（除目录变更和 mutating 写入）

# 🎲 随机选择

`choice` 在聊天中执行有界随机抽样，支持重复项加权、有放回多选和唯一项多选。

---

## ⌨️ 命令

<!-- manifest-command-aliases:start -->
| 功能 | 推荐入口 | Manifest 等价别名 |
| --- | --- | --- |
| 随机选择 | `/选择` | `/choice` `/决定` `/抽奖` |
<!-- manifest-command-aliases:end -->

```text
/选择 <问题> <选项1> <选项2> ...
/选择 <问题> <选项1> <选项2> ... -n <数量>
/选择 <问题> <选项1> <选项2> ... -n <数量> -u
/选择 help
```

问题或选项包含空格时使用引号：

```text
/选择 "今天吃什么" "ice cream" 火锅
```

---

## 🎮 抽样规则

| 参数 | 规则 |
| --- | --- |
| 默认模式 | 有放回抽样；重复项会提高对应文本的候选权重 |
| `-n <数量>` | 返回 1～10 个结果 |
| `-u`、`--unique` | 先按文本去重，再从唯一项中抽样 |
| `--` | 结束参数解析，后续内容均作为普通候选项 |

系统随机源负责候选抽样和结果 emoji。插件在内存中完成单次选择，运行目录保持零插件数据文件。

---

## 🔐 输入边界

- 候选位置数量为 2～50 个；
- 问题长度为 1～100 个可显示字符；
- 每个选项长度为 1～200 个可显示字符；
- 整体参数长度上限为 4096 个字符；
- `-n` 接受一次 1～10 的 ASCII 整数；
- 唯一项模式要求结果数量位于唯一候选数量范围内；
- 引号使用 Shell 风格配对规则。

日志记录参数长度、候选数量和抽样模式，聊天文本与抽样结果保留在日志边界之外。

---

## 💡 示例

```text
/选择 午饭 火锅 烤肉 日料
/选择 抽奖 小明 小红 -n 5
/选择 抽奖 小明 小红 小明 -n 2 -u
/选择 符号 -- -n -u
```

---

## 🩺 排障

| 提示 | 处理方式 |
| --- | --- |
| 参数格式错误 | 检查引号配对、`-n` 数值与参数顺序 |
| 候选数量超出范围 | 调整为 2～50 个候选位置 |
| 唯一候选数量不足 | 减小 `-n`，或增加不同文本的候选项 |

---

## ✅ 开发验证

在仓库根目录运行：

```bash
python -m ruff check plugins/choice tests/plugins/choice/test_choice.py
python -m mypy plugins/choice
python -m pytest -q tests/plugins/choice/test_choice.py \
  tests/plugins/contracts/test_plugin_documentation_contracts.py \
  tests/plugins/contracts/test_public_error_redaction.py
```

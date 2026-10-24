# Random choice

[中文](README.md)

The `choice` plugin selects random items from a list in a chat.
Repeated items can increase their selection probability.
The plugin can select items with replacement or select unique items.

## Commands

The main command is `/选择`. Its aliases are `/choice`, `/决定`, and `/抽奖`.

```text
/选择 <question> <item1> <item2> ...
/选择 <question> <item1> <item2> ... -n <count>
/选择 <question> <item1> <item2> ... -n <count> -u
/选择 help
```

Use quotation marks around a question or item that contains spaces:

```text
/选择 "What to eat" "ice cream" 火锅
```

## Selection rules

| Mode or option | Result |
| --- | --- |
| Default | Selection with replacement. Repeated items increase the probability for that text. |
| `-n <count>` | The result contains 1 to 10 items. |
| `-u`, `--unique` | The plugin removes repeated text before selection. |
| `--` | Option parsing stops. All subsequent arguments are items. |

The system random source selects the items and result emoji.
Each selection uses memory. The plugin creates no data files.

## Input limits

- The list contains 2 to 50 item positions.
- The question contains 1 to 100 display characters.
- Each item contains 1 to 200 display characters.
- The complete argument has a limit of 4096 characters.
- `-n` accepts one ASCII integer from 1 to 10. This option can occur once.
- In unique mode, the result count must be at most the number of unique items.
- Quotation marks follow shell pairing rules.

The log contains argument length, item count, and selection mode.
Chat text and results stay outside the log.

## Examples

```text
/选择 午饭 火锅 烤肉 日料
/选择 抽奖 小明 小红 -n 5
/选择 抽奖 小明 小红 小明 -n 2 -u
/选择 符号 -- -n -u
```

## Fault isolation

| Message | Action |
| --- | --- |
| Invalid argument format | Examine quotation marks, the `-n` value, and argument order. |
| Item count outside the range | Supply 2 to 50 item positions. |
| Too few unique items | Decrease `-n` or add items with different text. |

## Verification

Run these commands from the repository root:

```bash
python -m ruff check plugins/choice tests/plugins/choice/test_choice.py
python -m mypy plugins/choice
python -m pytest -q tests/plugins/choice/test_choice.py \
  tests/plugins/contracts/test_public_error_redaction.py
```

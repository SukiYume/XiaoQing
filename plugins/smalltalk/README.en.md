# 💬 Smalltalk

[中文](README.md) | English

`smalltalk` supplies short bot-name replies, exact question-and-answer records maintained by administrators, and basic chat through `chat.reply`.
The optional `voice.synthesize_text` service converts plain-text replies to speech at a configured probability.

## ⌨️ Dispatcher entry point

Core selects the smalltalk provider in `config/config.json`:

```json
{
  "plugins": {
    "smalltalk_provider": "smalltalk"
  }
}
```

Core calls `handle_smalltalk` after command, Session, mute, and Dispatcher checks.
When `xiaoqing_chat` is selected, XiaoQing Chat receives the applicable fallback messages.

## ⌨️ Administrator QA commands

```text
/记忆 <question> <answer>
/记住 <question> <answer>
/学习 <question> <answer>
/对话 [question]
/删除对话 <question> [answer]
```

Bot administrator permission is necessary for all three Manifest command groups.

| Item | Rule |
| --- | --- |
| Question | First non-whitespace token. Maximum 128 characters |
| Answer | Spaces permitted. Maximum 1000 characters |
| Answers per question | Maximum 20 different texts. Random selection on a match |
| Questions per scope | Maximum 2000 |
| `/对话 <question>` | Exact match of the complete question |
| List output | 2800 characters. Includes the omitted count |

Group QA records are shared by group ID. Private QA records are isolated by user ID:

```text
data/smalltalk/QA_group_<group-id>.json
data/smalltalk/QA_private_<user-id>.json
data/smalltalk/QA_audit.json
```

Process-local locks and atomic writes protect main-file updates.
The change audit keeps a maximum of 5000 entries with the operation and question digest.
The plugin validates group and user IDs before it creates a scope.

Disk commit and memory-cache publication form one update transaction.
After cancellation, a thread write that has started completes cache publication before it releases the scope lock.
The next edit reads the latest state.

## 💬 Bot-name replies

When a message contains only the bot name, the plugin selects a reply set in this order:

1. The `小青` array in `data/smalltalk/小青.json`.
2. The `responses` array in `data/smalltalk/responses.json`.
3. Built-in short replies.

Each set accepts 200 unique nonempty strings. Each string has a limit of 1000 characters.
The loader filters structure, types, lengths, and duplicate values.

## 💬 Chat service

An exact QA match returns the local answer directly.
Other chat uses the Core-issued `context.capabilities.chat_reply` to call `chat.reply`.
The call includes the current `user_id` and `group_id` actor fields.

The `smalltalk` Manifest `uses_services` declaration and the `chat` caller list control service access together.
A provider error or invalid message segment produces a fixed fallback message. Logs contain a redacted error category.

## 🎨 Voice probability

The setting is in `config/config.json`:

```json
{
  "plugins": {
    "smalltalk": {
      "voice_probability": 0.2
    }
  }
}
```

The field accepts finite numbers from 0 to 1. The default is `0.2`.
A successful probability check calls `voice.synthesize_text` for plain-text replies of at most 3000 characters.
Mixed media and voice service errors keep the original text reply.

## 🔐 Concurrency and privacy

The plugin Manifest uses `parallel`. Each QA file lock serializes writes.
Remote Chat calls share the `chat` plugin concurrency and quota limits.

Logs contain operations, lengths, counts, status, and error categories.
Ordinary log filtering excludes QA answers, chat bodies, voice text, and actor identifiers.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Another plugin receives fallback messages | Examine `plugins.smalltalk_provider`. |
| Empty exact QA query | Examine the scope, question token, and administrator identity used for the write. |
| Chat fallback message | Examine `chat.reply` authorization, Chat credentials, and quotas. |
| Reply stays as text | Examine `voice_probability`, plain-text length, and the Voice service. |
| Invalid QA state file | Back up the JSON file. Then examine its structure and write permissions. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/smalltalk tests/plugins/smalltalk/test_smalltalk.py
python -m mypy plugins/smalltalk
python -m pytest -q tests/plugins/smalltalk/test_smalltalk.py -n 2
```

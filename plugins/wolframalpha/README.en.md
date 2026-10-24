# 🧮 Wolfram|Alpha

[中文](README.md) | English

`wolframalpha` uses fixed Wolfram|Alpha HTTPS APIs for mathematics, physics, chemistry, unit conversions, and public-data queries.
All users can use the commands in private and group chats.

## ⌨️ Commands

```text
/alpha 1+1
/wa sin(pi/4)
/alpha --mode=step integrate x^2
/alpha --mode=complete population of China
/alpha help
```

The triggers are `/alpha`, `/wolfram`, `/wa`, and `/计算`.

| Mode | API and result |
| --- | --- |
| `simple` | Quick text result endpoint. Default mode |
| `step` | XML solution steps |
| `complete` | JSON Result pods |
| `cp` | Short alias for `complete` |

An initial `--mode=<value>` selects the mode. A final `step` or `cp` in the question stays part of the query text.
Queries have a limit of 500 characters.

## ⚙️ App ID

The setting is in `config/secrets.json`:

```json
{
  "plugins": {
    "wolframalpha": {
      "appid": "YOUR-WOLFRAM-APPID"
    }
  }
}
```

The App ID accepts 1–128 letters, digits, hyphens, or underscores.
Credential filtering excludes this value from public configuration, chat replies, and ordinary logs.

## 🌐 Request procedure

- `simple` uses `https://api.wolframalpha.com/v1/result`.
- `step` and `complete` use `https://api.wolframalpha.com/v2/query`.
- All modes use GET, a fixed domain, a 30-second total timeout, and a client with resource limits.
- The client receives the App ID as a request parameter.
- The plugin permits a maximum of two concurrent Wolfram requests.

Ordinary logs contain the mode, status, response length, and error category. Log filtering excludes request URLs and parameters.

## 🔐 Response limits

| Item | Limit |
| --- | ---: |
| Wire response and decoded response | 1 MiB each |
| XML/JSON | Depth, node, attribute, and string limits |
| Result items | 20 |
| API text | 2400 characters |
| Query | 500 characters |

Final text keeps complete result-item boundaries and fits the QQ single-message limit.
HTTP, network, timeout, format, and internal errors each map to stable public messages.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| App ID configuration error | Examine the secret location and permitted characters. |
| Empty result | Try the applicable `simple`, `step`, or `complete` mode. |
| Request timeout | Examine Wolfram|Alpha and the network connection. |
| XML or JSON validation failure | Examine official API structure changes and response limits. |
| Result has an ellipsis | Reduce the query scope or divide it into multiple questions. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/wolframalpha/test_wolframalpha.py
python -m ruff check plugins/wolframalpha tests/plugins/wolframalpha/test_wolframalpha.py
python -m mypy plugins/wolframalpha
```

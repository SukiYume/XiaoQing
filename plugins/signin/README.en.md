# Yingshi remote sign-in

[中文](README.md)

The `signin` plugin uses shared Youzan and Yingshi credentials for remote sign-in.
The operator supplies these credentials.
The manifest restricts the command to Bot administrators.
Manual and scheduled calls run in sequence.

## Commands and schedule

```text
/signin yingshi
/签到 yingshi
/signin y
/signin help
```

`yingshi`, `yingshijufeng`, and `y` select the same process.
`帮助` and `?` are aliases for `help`.

The manifest runs `scheduled_yingshi` each day at 00:30 in the scheduler time zone.
The task uses the Core `broadcast` mode.
The plugin uses `sequential` concurrency. Calls use the shared credentials in their arrival order.

## Credentials

Set these fields in `config/secrets.json`:

```json
{
  "plugins": {
    "signin": {
      "yingshijufeng": {
        "app_id": "...",
        "kdt_id": "...",
        "access_token": "...",
        "sid": "..."
      }
    }
  }
}
```

Each field accepts a nonempty string or an integer.
The plugin reads credentials from its secret snapshot.
Credentials stay outside chat messages, public settings, and ordinary logs.

## Remote process

The plugin uses the fixed host `https://h5.youzan.com`.

1. The plugin gets the current sign-in ID.
2. The plugin sends a sign-in request with that ID.
3. The plugin reads the description, total sign-in count, and rewards.
4. The plugin shows at most 20 valid reward records.

The `Authorization` header contains the access token.
The Core transport validates HTTP status, MIME type, compression ratio, response size, JSON depth, and node count.
The plugin also validates the type, length, and control characters of remote text.
The operator manages account authorization and service use under the provider's terms.

## Fault isolation

| Condition | Action |
| --- | --- |
| Credentials are missing | Examine all four fields and their JSON location. |
| Remote authentication fails | Update `access_token`, `sid`, and account authorization. |
| A scheduled task fails | Examine the scheduler time zone, logs at 00:30, and network connection. |
| Response format is invalid | Examine API changes and bounded-response error codes. |

## Verification

Run these commands from the repository root:

```bash
python -m ruff check plugins/signin tests/plugins/signin/test_signin.py
python -m mypy plugins/signin
python -m pytest -q tests/plugins/signin/test_signin.py -n 2
```

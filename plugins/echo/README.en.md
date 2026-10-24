# Echo

[中文](README.md)

The `echo` plugin returns text and sends a greeting to the user.
The plugin shows command dispatch, message segments, and public error handling.

## Commands

| Function | Command | Alias |
| --- | --- | --- |
| Return text | `/echo` | `/回显` |
| Send a greeting | `/hello` | `/你好` |

```text
/echo Hello world
/回显 支持中文别名
/hello
/你好
```

## Operation limits

- `/echo` removes spaces at the start and end of the text.
- An empty argument shows the local help.
- The Core limit for one QQ text message applies. The current limit is 3000 characters.
- The text can contain newlines and tabs. Other C0 and C1 control characters cause an argument error.
- `/hello` accepts an empty argument. The greeting contains the current QQ ID after validation as a positive integer.
- A missing or invalid user ID gives the label `未知用户`.
- The log contains the length of accepted text. The message body stays outside the log.

The plugin has no settings, credentials, network requests, data files, or scheduled tasks.

## Development

`main.py` publishes the `handle()` entry point.
Separate functions supply help, validate control characters, and normalize QQ IDs.
These functions show how a small plugin returns messages and handles public errors.

## Verification

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/echo/test_echo.py
python -m ruff check plugins/echo
python -m mypy plugins/echo
```

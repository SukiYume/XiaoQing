# Number game

[中文](README.md)

The `guess_number` plugin uses a Core Session for a short number game.
Each user can have one active session in each group chat or private chat.

## Commands

The main command is `/猜数字`. Its aliases are `/guess` and `/猜`.

| Command | Result |
| --- | --- |
| `/猜数字` | Starts a normal game. |
| `/猜数字 简单` | Starts an easy game. |
| `/猜数字 普通` | Starts a normal game. |
| `/猜数字 困难` | Starts a hard game. |
| `/猜数字 地狱` | Starts a hell game. |
| `/猜数字 status` | Shows progress. |
| `/猜数字 restart` | Replaces this plugin's session with a new normal game. |
| `/猜数字 help` | Shows local help. |

Difficulty aliases are `easy`, `e`, `normal`, `n`, `hard`, `h`, `hell`, and `nightmare`.
Status aliases are `状态`, `info`, and `信息`.
Restart aliases are `重新开始` and `重开`.

## Game procedure

1. Send a command with the applicable difficulty.
2. Send an ASCII decimal integer for each guess.
3. To see progress, send `状态`, `status`, `信息`, `info`, or `?`.
4. To end the session, send `退出`, `取消`, `exit`, `quit`, or `q`.

Core ends a session after 180 seconds without input.
If another plugin owns the active session, the command asks you to end that session first.
`restart` replaces only a game that this plugin owns.

## Difficulty

| Difficulty | Target range | Attempts |
| --- | --- | --- |
| Easy | 1 to 50 | 10 |
| Normal | 1 to 100 | 7 |
| Hard | 1 to 200 | 8 |
| Hell | 1 to 1000 | 10 |

A guess has a limit of 64 characters. The integer contains 1 to 4 digits.
Invalid format or an out-of-range value leaves the remaining attempts unchanged.

## Session state

The session stores the target, current lower and upper limits, used attempts, guess history, and normalized difficulty.
The plugin calculates remaining attempts and the hint range when it reads the state.
The game ends after a win, the last attempt, an exit command, a timeout, or a state validation error.

The system random source supplies the target. The target stays outside the log.
Core commits session updates in sequence for each user and chat scope.
The plugin has no settings, credentials, network requests, files, or scheduled tasks.

## Fault isolation

| Condition | Action |
| --- | --- |
| Another session is active | Send `退出`. Then start the game. |
| Number input fails | Use an ASCII integer in the current hint range. |
| The game has expired | Send `/猜数字` again. |
| `restart` reports a session ownership error | End the current Core Session first. |

## Verification

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/guess_number/test_guess_number.py
python -m ruff check plugins/guess_number
python -m mypy plugins/guess_number
```

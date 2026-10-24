# Earthquake

[中文](README.md)

The `earthquake` plugin reads recent bulletins from the [China Earthquake Networks Center Weibo account](https://www.weibo.com/ceic).
Manual commands show the latest record.
Scheduled tasks send new bulletins with magnitude 4.0 or more to the groups that Core supplies for the schedule.

## Commands

The main command is `/earthquake`. Its alias is `/地震`.

| Command | Result |
| --- | --- |
| `/earthquake` | Shows the latest valid bulletin. |
| `/earthquake latest` | Shows the latest valid bulletin. |
| `/earthquake help` | Shows local help. |

`最新` is an alias for `latest`. `h` and `帮助` are aliases for `help`.
Manual queries keep the scheduled task's cursor.

## Scheduled delivery

The manifest runs `scheduled` every five minutes in Core `targeted` mode.

1. The plugin first continues pending events from `earthquake_delivery.json`.
2. The plugin gets and validates Weibo cards.
3. The plugin scans new events and selects records with magnitude 4.0 or more for delivery.
4. The plugin sends each target group a message and records confirmations atomically.
5. After all targets confirm, the plugin commits the Weibo cursor and checkpoint.

Records with lower magnitudes contribute to scan progress.
Pending events continue after a restart.
An empty target list, network error, or failed target keeps the current delivery cursor.

## Local state

The default data directory is `data/earthquake/`.

| Path | Contents |
| --- | --- |
| `earthquake.json` | Current Weibo cursor. |
| `earthquake.checkpoint.json` | Cursor recovery checkpoint. |
| `earthquake_delivery.json` | Pending events and progress for each target. |
| `EarthquakeFigures/` | Validated image cache. |

Cursor files use bounded JSON.
The plugin atomically moves malformed files to `*.corrupt-*` evidence copies.
It then recovers from a valid checkpoint.
Image cache names use content hashes.
The cache has limits of 64 entries and 64 MiB. Retention is 30 days.

## Network and data limits

- Weibo visitor responses have limits for MIME type, compression ratio, decoded size, JSON depth, node count, and card count.
- Image URLs use HTTPS hosts from `wx1.sinaimg.cn` through `wx4.sinaimg.cn`.
- Each image has an 8 MiB limit.
- Image checks include format, container ending, frame count, dimensions, pixel count, and decoding memory.
- Body cleanup removes scripts, styles, HTML tags, and control characters.
- A card parsing error stays within that record.

The plugin uses `requests`, `beautifulsoup4`, and `Pillow`.
It reuses a Weibo session within the current plugin generation and replaces it after 15 minutes.

## Information use

The visitor API supplies best-effort notifications.
For emergency decisions, also consult official CENC applications, WeChat services, mini programs, and local emergency authorities.

The plugin uses public access without credentials.
Access to Weibo and Sina image hosts is necessary for manual queries.
For automatic delivery, at least one target from the schedule's `group_ids` or global `default_group_ids` is necessary.

## Lifecycle

A process-local lock serializes scheduled scans.
On unload, reload, or Bot shutdown, `shutdown()` closes the current Weibo session in a thread pool.
The cursor, checkpoint, and pending file restore progress after a restart.

## Fault isolation

| Condition | Action |
| --- | --- |
| Manual queries fail | Examine the visitor API, connection, and runtime log. |
| Automatic messages stay pending | Examine default groups, Bot send permissions, and the pending file. |
| A state file becomes an evidence copy | Examine the checkpoint and data-directory write permission. |
| Image download fails | Examine the Sina host, image limits, and Pillow decoding log. |

## Verification

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/earthquake/test_earthquake.py
python -m ruff check plugins/earthquake
python -m mypy plugins/earthquake
```

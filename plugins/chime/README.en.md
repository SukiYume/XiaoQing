# CHIME/FRB

[中文](README.md)

The `chime` plugin reads the CHIME/FRB repeater catalog.
It uses a local notification baseline to find new repeaters and bursts.

## Commands

The main command is `/chime`. Its alias is `/frb`.

| Command | Result |
| --- | --- |
| `/chime` | Shows updates since the last successful notification. |
| `/chime list` | Shows five recently updated FRBs in observation-time order. |
| `/chime FRB20180916B` | Shows time, DM, RA, DEC, and SNR for the named FRB. |
| `/chime help` | Shows local help. |

The command accepts one subcommand or one canonical FRB name.
`列表` and `帮助` are aliases for `list` and `help`.

## Scheduled notifications

The manifest runs `scheduled_check` at 09:00 and 21:00 each day in Core `targeted` mode.
Core supplies the target groups for that schedule.
Each check has these stages:

1. The plugin reads `chime_delivery.json` and first continues pending notifications.
2. The plugin gets and validates the remote catalog.
3. The plugin finds records added since the notification baseline.
4. The plugin sends each target group a message and records each confirmation atomically.
5. After all targets confirm, the plugin updates `chime_history.json` and removes the pending notification.

Pending delivery continues after a process restart.
Network delivery and local confirmation give at-least-once delivery.
A power failure between these operations can cause a repeated notification to one target.
Manual requests share a five-minute catalog cache.
Concurrent requests with an empty cache share one download.
Each scheduled check gets a new catalog.

## Data source

The runtime endpoint is `https://catalog.chime-frb.ca/repeaters`.
The Chinese source records a runtime check on 2026-09-05.
That check found a TLS hostname mismatch at this endpoint, before catalog parsing.
It also records an infrastructure rebuild notice on the [official public page](https://www.chime-frb.ca/repeaters).
This dated observation gives historical context. A new runtime check is necessary to establish current endpoint availability.

This endpoint or an equivalent official endpoint is necessary for live queries and incremental notifications.
Published archives have fixed data cutoff dates.
The plugin keeps certificate validation and its notification baseline after a fetch failure.
The reply reports the fetch failure.

Official archives are available from the [CHIME/FRB Catalog](https://www.chime-frb.ca/catalog)
and [CHIME/FRB Open Data](https://chime-frb-open-data.github.io/).

## Local state

| File | Contents |
| --- | --- |
| `data/chime/chime_history.json` | Last confirmed notification timestamp for each FRB. |
| `data/chime/chime_delivery.json` | Pending notifications, groups, and confirmation state for each target. |

The plugin validates structure, record count, names, and timestamps in both files.
Catalog errors, state errors, send errors, and empty target lists keep the current baseline.
Processing can continue after maintenance.

## Input and network limits

- Burst date keys use all six date digits.
- FRB names pass format, length, and control-character validation.
- Observation times use ISO-style strings with a date and seconds.
- Remote responses have limits for body size, JSON depth, node count, and field types.
- Logs contain bounded names and error codes.

## Fault isolation

| Condition | Action |
| --- | --- |
| Catalog retrieval fails | Examine the endpoint, connection, and runtime log. |
| Notifications stay pending | Examine target groups, send permissions, and `chime_delivery.json`. |
| State validation fails | Restore the JSON from a backup, or have a maintainer confirm the baseline before reconstruction. |
| A query returns no records | Examine the FRB name and catalog response time. |

## Verification

Run these commands from the repository root:

```bash
python -m ruff check plugins/chime tests/plugins/chime/test_chime.py
python -m mypy plugins/chime
python -m pytest -q tests/plugins/chime/test_chime.py \
  tests/plugins/contracts/test_durable_plugin_notifications.py \
  tests/plugins/contracts/test_fixed_origin_http_clients.py
```

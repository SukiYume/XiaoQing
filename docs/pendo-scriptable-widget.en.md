# 📱 Pendo Scriptable widget

[中文](pendo-scriptable-widget.md) | English

Pendo supplies a read-only iPhone Scriptable summary.
It shows upcoming events, tasks, finances, or notes on the home screen.

## 🔐 Interfaces and permissions

| Item | Value |
|---|---|
| Summary interface | `GET /api/widget/summary` |
| Calendar reconciliation interface | `GET /api/widget/calendar?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` |
| Section parameter | `section=tasks\|ledger\|notes\|all\|auto` |
| Authentication | `Authorization: Bearer <widget_token>` |
| Permission scope | `GET /api/widget/*` |
| Default lifetime | 365 days |

`tasks`, `ledger`, and `notes` return events and one specified panel.
`all` returns events and all three panels.
`auto` rotates hourly through `tasks → ledger → notes`.
Widget tokens are read-only Bearer credentials. Browser cookie sessions use a separate permission scope.

## 📌 Issue and revoke tokens

Run this command in QQ private chat:

```text
/pendo web widget-token
```

`WEB_WIDGET_TOKEN_EXPIRE_SECONDS` defines the default lifetime.
Each issuance creates an independent `jti`.
The server stores its digest, user, issuance time, and absolute expiry time in `pendo.db`.

Revoke the current user's widget tokens:

```text
/pendo web widget-revoke
```

Browser cookie sessions have separate records and revocation boundaries.

## 📌 Scriptable script

Script location:

```text
plugins/pendo/web/scriptable/pendo_widget.js
```

Set the Pendo Web URL and widget token at the script top:

```javascript
const BASE_URL = normalizeBaseUrl('https://example.com/pendo');
const TOKEN = 'PASTE_WIDGET_TOKEN_HERE';
```

Common addresses:

- Local host: `http://127.0.0.1:12001`
- Reverse-proxy subpath: `https://example.com/pendo`

Set `TOKEN` to the complete value returned by `/pendo web widget-token`.
For HTTP 401, generate a new token in QQ private chat and replace `TOKEN` at the script top.

## 📌 Summary layout

Events cover the next 30 days, with at most 5 entries.
Task, finance, and note panels each return at most 5 entries.
The three widget sizes use these fixed data and layout contracts:

| Size | Request parameters | Display |
|---|---|---|
| `small` | Fixed `section=auto` | Event summary and list |
| `medium` | Widget parameter `tasks`, `ledger`, `notes`, or `auto` | Events on the left, one panel on the right |
| `large` | Fixed `section=all` | Events, tasks, finances, and notes |

`small` and `large` use fixed sections. Widget parameters control `medium`.
Invalid or empty medium parameters fall back to `auto`.
All sizes share title, time, body, and status type-size hierarchy.

Finance summaries return and display income, expense, and balance separately by currency.
Amounts keep their currency labels.
The interface supplies original-currency totals. Additional sources and rules are necessary for exchange conversion.

## 📌 iPhone setup

1. Install Scriptable.
2. Run `/pendo web widget-token` in QQ private chat.
3. Create a script and paste the complete `pendo_widget.js` content.
4. Set `BASE_URL` and `TOKEN` at the script top.
5. Run the script in the Scriptable app.
6. Confirm that the summary loads.
7. Add a Scriptable widget to the iPhone home screen.
8. Select the Pendo script.
9. For a medium widget, set its parameter to `tasks`, `ledger`, `notes`, or `auto`.

For a custom server port, update both `config.plugins.pendo.web_port` and script `BASE_URL`.

## 📌 iOS calendar synchronization

The calendar name at the script top selects the synchronization target:

```javascript
const SYNC_CALENDAR_NAME = 'Pendo';
```

Create a writable calendar with that name in the Calendar app.
Permit Scriptable calendar access.
Set `SYNC_CALENDAR_NAME` to an empty string to disable synchronization.

Synchronization runs when you execute the script directly inside Scriptable.
Home-screen refresh reads only the summary, with at most 5 events.
Direct execution fetches the complete window through the calendar interface.
`Pendo-ID` markers identify events by item ID.
Older events with a synchronization marker and no ID can be claimed once by title and start time.
User events without synchronization markers keep their original content and existence.

A successful-day cursor in Keychain controls the window:

- The first successful run queries 30 days before through 30 days after the current day.
  This establishes a complete recent calendar window.
- Later runs always look back at least 30 days and forward 30 days from the execution day.
  An earlier successful cursor extends the start to fill the interval.
- One API window permits at most 3660 days.
  Older saved cursors start at the earliest date within that limit.
- Each run makes one API query and one iOS target-calendar query.
  The cursor advances after the API read and all reconciliation operations succeed.
- A missing calendar, API failure, or event-save failure keeps the original cursor.
  The next run processes the same window again.

Calendar writes reconcile by Pendo ID.
Timed events represent actual instants with offset-aware ISO timestamps. Devices write those instants to the calendar.
The server derives offsets from the event timeline.
This covers different user/device timezones and repeated wall-clock times during daylight-saving rollback.
All-day events keep date-only semantics.

- Timed events default to 1 hour. Valid API end times take priority.
- Date-only events become 1-day all-day events.
- Calendar events receive the location. Notes contain `[由 Pendo Widget 同步]` and `Pendo-ID` markers.
- Existing `Pendo-ID` events update title, start, end, all-day state, and location in place.
  Identical fields skip repeat saves.
- Within the complete current window, managed events deleted on the server or moved outside the window are removed.
  Duplicate copies with the same `Pendo-ID` are also removed.
- Automatic deletion applies to events with both the synchronization marker and `Pendo-ID`.
  Unmarked events, older events without IDs, and events outside the window remain unchanged.

After direct execution, notifications and console output show added, updated, deleted, unchanged, and skipped counts.
A missing-calendar notification identifies the necessary calendar name.

## 📌 Interaction

- Tap the event area to open Pendo events.
- Tap tasks, finances, or notes to open that module.
- Edit data in Pendo Web.
- iOS controls actual refresh frequency. The script supplies a suggested refresh time.

## 🔐 Security guidance

- Use an HTTPS reverse proxy for public Pendo Web access.
- Treat widget tokens as personal read-only data credentials.
- Scriptable source contains the token. Manage script copies and exports as personal credentials.
- After phone transfer, loss, or credential exposure, run `/pendo web widget-revoke`.
- Check user, `jti`, expiry, and revocation results in Pendo logs.

See the [plugin README](../plugins/pendo/README.en.md) and [architecture](../plugins/pendo/ARCHITECTURE.en.md) for complete Web authentication and data models.

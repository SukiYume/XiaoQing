# GitHub Trending

[中文](README.md)

The `github` plugin reads the [official GitHub Trending page](https://github.com/trending).
It shows daily, weekly, and monthly repository trends.

## Commands

The main command is `/github`. Its aliases are `/gh` and `/trending`.

| Command | Result |
| --- | --- |
| `/github` | Shows daily trends. |
| `/github daily` | Shows daily trends. |
| `/github weekly` | Shows weekly trends. |
| `/github monthly` | Shows monthly trends. |
| `/github help` | Shows local help. |

`h` and `帮助` are aliases for `help`.
The command accepts one complete time-range argument.

## Retrieval and output

The plugin parses official GitHub HTML with these limits:

- Direct requests use `https://github.com`.
- Response checks cover MIME type, same-origin redirects, compression ratio, and a 2 MiB decoded-size limit.
- Text encoding is UTF-8 or ASCII.
- One request parses at most 50 article nodes.
- Repository links use GitHub paths with two components.
- The parser normalizes descriptions, languages, counts, and control characters separately.
- QQ output contains at most 10 complete repository blocks and obeys the Core text-message limit.

After a change to the Trending page structure, updates to parser rules and test fixtures are necessary.

## Schedule and history

The manifest runs `scheduled` each day at 08:30 in the scheduler time zone.
The task uses Core `broadcast` mode. Core sends results to the schedule's target groups.
A successful fetch writes these files:

```text
data/github/trending_<range>_latest.json
data/github/history/trending_<range>_<YYYY-MM-DD>.json
```

The `latest` file gives the current pointer. Dated files contain historical snapshots.
Each time range keeps at most 90 regular history files.
A process-local lock serializes writes and retention operations.

## Optional proxy

The optional proxy is the plugin's only credential field.
Set it in `config/secrets.json`:

```json
{
  "plugins": {
    "github": {
      "proxy": "http://proxy.example.com:8080"
    }
  }
}
```

The address must be a complete HTTP or HTTPS URL.
The proxy makes the target DNS connection. The administrator must trust the proxy.
Logs contain request status. Proxy credentials stay outside the log.

## Lifecycle

Each command or scheduled task runs one complete fetch transaction.
Direct requests use the Core public-network fetcher with security checks.
Proxy requests reuse the HTTP session that Core manages.
History files and the current transaction form the plugin's runtime state.

## Fault isolation

| Condition | Action |
| --- | --- |
| Page retrieval fails | Examine GitHub connectivity, proxy settings, and runtime logs. |
| The result is empty | Examine the page structure and `article.Box-row` parser rule. |
| History writes fail | Examine `data/github/` permissions and disk space. |
| The time range is invalid | Use `daily`, `weekly`, or `monthly`. |

## Verification

Run these commands from the repository root:

```bash
python -m pytest -q tests/plugins/github/test_github.py
python -m ruff check plugins/github
python -m mypy plugins/github
```

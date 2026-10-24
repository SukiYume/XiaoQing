# 🐦 Twitter Images

[中文](README.md) | English

`twitter` downloads images from a specified X/Twitter account into a local cache with resource limits.
It randomly sends one image that remains unsent in the current cycle.

## ⌨️ Commands and schedule

| Usage | Permission | Effect |
| --- | --- | --- |
| `/twimg`, `/twitter`, `/推特` | All users | Send one random image from the local cache |
| `/tw_fetch`, `/抓取推特` | Bot administrator | Start a background fetch and send a private completion notification |

Both commands accept `help`, `帮助`, and `?`.
The Manifest starts a daily background fetch at 03:00 in Core `silent` mode. This task updates only the cache.
Manual and scheduled fetches share one background task.
Concurrent submissions reuse the current task and combine pagination and downloads into one execution.

Each account has an initial full-fetch completion marker.
A new account first traverses all permitted pages. Subsequent incremental fetches stop after two consecutive pages without new images.

## ⚙️ Credentials

The settings are in `config/secrets.json`:

```json
{
  "plugins": {
    "twitter": {
      "user_id": "Twitter用户ID",
      "headers": {
        "authorization": "Bearer <TWITTER_BEARER_TOKEN>"
      },
      "cookies": {},
      "proxy": "http://proxy.example.com:8080",
      "max_pages": 50
    }
  }
}
```

| Field | Rule |
| --- | --- |
| `user_id` | Necessary. A nonempty string or positive integer for the target account |
| `headers` | String mapping with size limits for the GraphQL API |
| `cookies` | String mapping with size limits for the GraphQL API |
| `proxy` | Optional HTTP/HTTPS proxy for the GraphQL API and media downloads |
| `max_pages` | Integer from 1 to 50. Default: 50 |

API requests use the configured authentication headers, cookies, and proxy.
Media downloads use the same proxy and separate minimal headers.
Trusted Twitter domains, HTTPS, and response limits are necessary for media requests.
Authentication values remain in secret configuration. Ordinary log filtering excludes these values.

## 🔄 Fetch procedure

1. Request the `x.com` user timeline GraphQL API.
2. Extract image URLs and pagination cursors from JSON within resource limits.
3. Normalize media URLs to original-image addresses.
4. Download up to four images concurrently through the configured proxy.
5. Validate images and write them to the cache with content SHA-256 names.
6. Traverse the initial timeline to its end or `max_pages`, then record completion for the current `user_id`.
7. Stop subsequent incremental fetches after two consecutive pages without new images.

X/Twitter GraphQL APIs and authentication fields can change with site updates.
Maintainers must update request features and test fixtures accordingly.

## 🔐 Network and image limits

| Item | Limit |
| --- | ---: |
| API response | 5 MiB. JSON depth, node, and string limits |
| Media domains | `pbs.twimg.com`, `ton.twitter.com`, `video.twimg.com` |
| Media protocol | HTTPS |
| One image | 10 MiB, 40 million pixels, 120 frames |
| Image formats | JPEG, PNG, WebP |
| Initial full fetch | Timeline end or maximum 50 pages |
| Incremental stop condition | Two consecutive pages without new images |
| Concurrent downloads | 4 |
| Cache | 5000 entries, 2 GiB, 90 days |

## 💾 Send cycles and data

Runtime data uses these paths:

```text
data/twitter/images/
data/twitter/posted.txt
data/twitter/backfill_complete.json
```

`images/` contains images named by content identity.
`posted.txt` contains filenames with confirmed delivery in the current cycle. Its read limit is 1 MiB.
The plugin reserves an image before transmission and commits its filename after OneBot confirmation.
A send error releases the reservation. After all valid images complete a cycle, state starts a new cycle automatically.

`backfill_complete.json` contains a small initial full-fetch marker bound to `user_id`.
A missing or damaged marker, or an account change, starts another full fetch on the next run.
The marker updates only after complete traversal and successful media downloads.

GraphQL `errors` and missing or malformed timeline structures cause fetch failure. Existing backfill state remains intact.
A structurally valid empty timeline is a normal end signal.
Cache reads validate image formats and resource limits again, and remove stale filenames from state.

## ⏰ Lifecycle

Background fetch tasks and manual notification tasks belong to the current plugin generation.
At unload, reload, or Bot shutdown, `shutdown()` cancels these tasks and waits for cleanup.
Images and send-cycle state remain in the data directory.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Empty `/twimg` cache | Have an administrator run `/tw_fetch`. Wait for completion, then request an image again. |
| GraphQL authentication failure | Update the Bearer token, cookies, target user ID, and request features. |
| All downloads fail | Examine the proxy URL, proxy availability, and Twitter media domain connections. |
| Every run uses a full fetch | Examine write access to `backfill_complete.json` and media download failures in completion logs. |
| Rejected image | Examine media domain, format, bytes, pixels, and frame count. |
| Scheduled cache stays unchanged | Examine 03:00 logs and site API changes. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/twitter tests/plugins/twitter/test_twitter.py
python -m mypy plugins/twitter
python -m pytest -q tests/plugins/twitter/test_twitter.py \
  tests/plugins/contracts/test_twitter_voice_resource_bounds.py -n 2
```

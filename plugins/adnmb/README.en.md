# ADNMB

[中文](README.md)

The ADNMB plugin shows public timelines, boards, threads, replies, and anonymous subscription lists.
The plugin derives an anonymous UUID for each QQ user. This UUID identifies the user's subscriptions.

## Requirements

Commands work in group chats and private chats.
The dependencies are `aiohttp` and Pillow.
Network access to the public ADNMB API and image addresses is necessary.

## Commands

| Command | Function |
| --- | --- |
| `/adnmb -t [-p <page>]` | Show the timeline. |
| `/adnmb -f` | Show boards. |
| `/adnmb -m <board> [-p <page>]` | Show board contents. |
| `/adnmb -c <thread> [-p <page>]` | Show a thread and replies. |
| `/adnmb -r <reply>` | Show one reply. |
| `/adnmb -d [-p <page>]` | Show anonymous subscriptions. |
| `/adnmb -a <thread>` | Add a subscription. |
| `/adnmb -e <thread>` | Remove a subscription. |
| `/adnmb -h` | Show plugin help. |

`/help adnmb` shows all aliases, argument limits, and error examples.

## Settings and data

`config.plugins.adnmb.uuid` can supply a deployment UUID.
The plugin combines this UUID with each QQ user to derive a subscription identity.
The default identity uses the plugin data directory and user to make a stable UUID.

The image cache is in `data/adnmb/images/`.
The cache has limits for TTL, entry count, and total bytes.
Remote images pass timeout, response size, MIME type, dimension, and pixel checks.

The parser accepts at most 65,536 characters from each external body.
HTML cleanup uses a linear scan.
Processing has bounds for malformed tags and repeated angle brackets.
Display text also obeys the message length limit.

## Permissions

The command catalog contains public content access and anonymous Feed management.
Subscription operations change the Feed for the corresponding anonymous UUID through the ADNMB API.

## Fault isolation

1. Send `/adnmb -f` to test the API connection.
2. Send `/adnmb -t` to test timeline parsing.
3. Examine HTTP status, content type, and image validation results in the log.
4. Examine write permission for `data/adnmb/images/`.

## Verification

```bash
python -m pytest tests/plugins/adnmb/test_adnmb.py -q
python -m ruff check plugins/adnmb tests/plugins/adnmb/test_adnmb.py
```

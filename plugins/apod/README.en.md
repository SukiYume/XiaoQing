# Astronomy Picture of the Day

[中文](README.md)

The APOD plugin reads the current NASA Astronomy Picture of the Day page.
The reply contains an image or video link, title, description, date, credits, and source link.

## Requirements

Commands work in group chats and private chats.
The dependencies are Beautiful Soup and Pillow.
Network access to `science.nasa.gov` and `assets.science.nasa.gov` is necessary.
The plugin also accepts the old host `apod.nasa.gov`.

## Commands

| Command | Function |
| --- | --- |
| `/apod` | Get the current APOD. |
| `/apod help` | Show plugin help. |

`/每日一天文图` is an alias for `/apod`.
An empty argument selects the current page at `https://science.nasa.gov/apod/`.
`/help apod` shows arguments and error examples.

## Settings and schedule

These public settings are optional:

```json
{
  "plugins": {
    "apod": {
      "url": "https://science.nasa.gov/apod/",
      "allowed_hosts": []
    }
  }
}
```

The daily task runs at 13:30 in Core `broadcast` mode.
Core sends results to the schedule's `group_ids`.
If that field is absent, Core uses `default_group_ids`.
At least one target group is necessary for a production schedule.

The internal host allowlist contains `science.nasa.gov`, `assets.science.nasa.gov`, and `apod.nasa.gov`.
`allowed_hosts` adds other trusted hosts.
The old address `https://apod.nasa.gov/apod/astropix.html` selects the new home page automatically.
Other configured addresses stay unchanged. These include historical `image-article` pages.

## Page parsing

On new pages, `.hds-media-detail-hero` contains the APOD content.
The parser reads the title from `h1` or `h2` in this element.
It reads the description from `.media-detail-hero__description`.
It reads the date and credits from `.media-detail-hero__meta-table`.
The body stops before submission notices, migration notices, and next-day previews.

Media comes from `.media-detail-hero__media`.
The parser accepts images, iframe elements, and video elements.
For videos, it gives priority to the actual media link.
For responsive images, it selects the largest `srcset` candidate with a width of at most 1600 pixels.
It then tries `src` or `data-src`.
All download candidates pass the host allowlist check.

The parser also accepts `center` titles and Explanation paragraphs on old pages.

## Data and network limits

The cache is in `data/apod/images/`.
Page and media requests use HTTPS.
Each redirect causes new DNS and `allowed_hosts` checks.
The three internal NASA hosts can use reserved fake-IP addresses from the Clash transparent proxy.
Requests use the address from security validation and validate the TLS hostname.
The global security layer rejects other nonpublic addresses.

Public DNS addresses are necessary for additional hosts.
HTML, image bytes, MIME types, dimensions, and pixels have validation limits.
The SHA-256 digest of the final URL supplies the cache name.

## Fault isolation

1. Send `/apod` to test page retrieval.
2. Examine `allowed_hosts` and the final media host.
3. Examine DNS, security policy, HTTP status, redirects, MIME type, and image decoding in the log.
4. Examine write permission and cache capacity for `data/apod/`.

## Verification

```bash
python -m pytest tests/plugins/apod/test_apod.py -q
python -m ruff check plugins/apod tests/plugins/apod/test_apod.py
```

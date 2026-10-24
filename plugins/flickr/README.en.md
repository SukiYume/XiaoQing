# 📷 Flickr Public Photography Plugin

[中文](README.md) | English

The plugin displays Flickr selections, public search results, public user photos, and public albums in QQ.
Replies keep the author, license, and original photo-page information.

## ✨ Functions

The plugin reads public photos through the official REST API. The default command shows today's selection.
Search supports keywords, tags, sort order, license types, and capture dates.
Each query creates a 15-minute browsing session for `/flickr more`.

The default `license=any` includes all public Flickr license types: All Rights Reserved, Creative Commons, public-domain marks, and government works.
Each reply shows the photo license. Photo use must follow the author's and Flickr page's terms.

| Function | Description |
| --- | --- |
| Today's selection | Today's Flickr Interestingness list |
| Public search | Text, tags, sort order, license, and capture-date filters |
| Commons | Sources restricted to Flickr Commons collections |
| User photos | Public photos found by username, NSID, or profile URL |
| Albums | Photos in public albums |
| Continued browsing | Another 1–5 photos for the same user and chat scope |
| Photo details | Title, author, license, date, tags, description, and original page |

## 🔐 Requirements

1. Install common plugin dependencies:

```bash
   python -m pip install -e ".[plugins]"
   ```

2. Create a noncommercial API key in [Flickr App Garden](https://www.flickr.com/services/apps/create/apply/).

The application page shows eligibility for the account plan.

3. Put the key in `config/secrets.json`:

```json
   {
     "plugins": {
       "flickr": {
         "api_key": "your-flickr-api-key"
       }
     }
   }
   ```

`api_key` is private configuration. `config/secrets.json.example` contains only a placeholder.
The deployment environment manages the real key.
For initial deployment, save a complete secrets file before Bot startup.
If the running instance already has the Flickr path, an administrator can use a private chat:

```text
/set_secret plugins.flickr.api_key <your-API-key>
```

To add a missing Flickr path to a running instance, save a complete valid `config/secrets.json`.
When public configuration stays at the current version, the watcher stages the candidate.
It privately tells administrators that `plugins.flickr.api_key` was added.
After examination of the notice, the administrator sends `/reload` to apply it.
Notifications and logs contain field paths only. The API key stays in the secrets source.

## ⌨️ Commands

### Today's selection

```text
/flickr
/flicker
/弗里克
```

The command returns the first photo from today's selection and starts a browsing session.

### Public photo search

```text
/flickr search aurora
/flickr search Milky Way --sort interesting
/flickr search --tags nebula,telescope --license cc
/flickr search eclipse --date 2026-08
/flickr 搜索 星云 --license any
```

| Option | Values | Default | Meaning |
| --- | --- | --- | --- |
| `--tags` | Comma-separated tags | Empty | All tags must match |
| `--sort` | `relevance`, `interesting`, `new`, `old` | `relevance` | Relevance, interest, or publication date order |
| `--license` | `any`, `cc`, `public-domain` | `any` | All public licenses, CC, or public-domain categories |
| `--date` | `YYYY-MM` or `YYYY-MM-DD` | Empty | Capture-date filter |

`--tags` also works without text keywords.

### Flickr Commons

```text
/flickr commons astronomy
/flickr 典藏 moon --sort interesting
```

Parameters match `search`. Results are restricted to Flickr Commons.

### Public user photos

```text
/flickr user NASA on The Commons
/flickr user 12345678@N00
/flickr user https://www.flickr.com/photos/example/
```

The Flickr API resolves usernames to NSIDs. Standard `photos` and `people` profile URLs are supported.

### Public albums

```text
/flickr album https://www.flickr.com/photos/example/albums/72100000000000000
/flickr album example 72100000000000000
```

The owner and album ID are necessary for the album API. A complete album URL supplies both fields automatically.

### Continue browsing

```text
/flickr more
/flickr more 5
/flickr 更多 3
```

The default is one more photo. Each call permits up to five photos.
State is isolated by private user or by group ID plus user. It expires after 15 minutes.

### Photo details

```text
/flickr info
/flickr info 123456789
/flickr info https://www.flickr.com/photos/example/123456789/
/flickr info https://flic.kr/p/AbCdEf
```

An empty argument selects the last photo in the current session.
Photo IDs, standard photo pages, and `flic.kr` short links are supported.

## 🔐 Network, content, and license limits

- API requests use the fixed endpoint `https://api.flickr.com/services/rest`.
- Requests use HTTPS, explicit timeouts, response-byte limits, JSON depth/node limits, and strict MIME validation.
- The API key appears only in fixed API request parameters. Redaction excludes it from public errors, logs, and QQ replies.
- Public calls without OAuth follow Flickr Safe Search visibility and read public photos only.
- Image downloads accept only `https://live.staticflickr.com`.
- Each download validates domain, TLS, redirects, MIME type, bytes, pixels, dimensions, and frame count again.
- External titles, authors, tags, and descriptions have character and UTF-8 byte limits.
- Reply licenses come from Flickr photo metadata.
- All Rights Reserved photos can be browsed in private Bot sessions. The applicable rights are necessary for copying, republication, and reuse.

## 💾 Data and lifecycle

| Data | Location | Limit |
| --- | --- | --- |
| Image cache | `data/flickr/images/` | 256 entries, 256 MiB, 1 hour |
| Browsing sessions | Plugin-generation memory | 512 sessions, 15 minutes |
| API key | `config/secrets.json` | Deployment secret configuration ignored by Git |

Hot reload and process restart clear browsing sessions.
Cached image names use content-source hashes. Images remain in `data/flickr/` for backup or cleanup with runtime data.
Session locks are removed after the current call and its waiters exit.
Repeated `more` or `info` calls without a session return a message and release temporary lock entries.

## 🩺 Troubleshooting

| Message | Check |
| --- | --- |
| `Flickr API Key 未配置` | Examine the `plugins.flickr.api_key` location and JSON format. |
| `Flickr API Key 无效或已失效` | Examine key status in App Garden and update secret configuration. |
| `Flickr 暂时不可用` | Examine internet access, DNS, proxy, Flickr status, and request frequency. |
| `没有找到可发送的公开照片` | Relax keyword, tag, date, or license filters. |
| `图片下载暂时失败` | Open the original Flickr page. Examine the static-image domain and proxy DNS. |
| `当前没有可继续的结果` | Start another selection, search, user, or album query. |

## ✅ Development checks

```bash
python -m pytest tests/plugins/flickr -q
python -m pytest tests/core/test_plugin_manifest_stability.py -q
python -m ruff check plugins/flickr tests/plugins/flickr
python -m ruff format --check plugins/flickr tests/plugins/flickr
```

Unit tests cover fixed API transport, JSON contracts, all-license defaults, filters, user/album parsing, short links, session isolation, and pagination.
They also cover image validation, caches, invalid input, and key redaction.
A configured runtime can use QQ commands for live API acceptance checks.

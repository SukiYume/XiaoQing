# 🔗 URL Parser

[中文](README.md) | English

`url_parser` creates a page title, description, and optional preview image for a message that contains one complete URL.
It uses the Dispatcher URL entry point. Its Manifest command list is empty.

## 📌 Trigger conditions

Dispatcher calls `handle_url()` when the cleaned message fully matches one `http://` or `https://` URL.
Command messages, additional text, and multiple URLs use their respective Dispatcher branches.

Example:

```text
https://example.com/article
```

## 💾 Metadata rules

| Content | Source and rule |
| --- | --- |
| Title | `<title>`. Collapsed whitespace. Maximum 200 characters |
| Description | `description`, `og:description`, `twitter:description`. Maximum 100 characters |
| Image | `og:image`, `twitter:image`. Relative paths use the final page URL |
| Fallback title | `网页预览` when the page supplies a description or image |

Valid metadata produces a text summary and an optional image.
Empty metadata produces an empty message-segment list. Core then ends the preview.

## 🔐 Public network requests

Pages and images use separate public-network clients with empty credentials.
Each request and redirect validates the URL, DNS, and destination address again. Destinations must be public HTTP/HTTPS services.

| Item | Limit |
| --- | ---: |
| Input URL | 2048 characters |
| HTML | 2 MiB |
| Image URL | 4096 characters |
| One image | 5 MiB, 20 million pixels, 120 frames |
| Image formats | JPEG, PNG, WebP |
| Concurrent page previews | 4 |

Clients use separate sessions, empty credentials, and empty environment-proxy settings.
An image error keeps the parsed text summary. A page request error ends the preview.

## 💾 Image cache

Preview images are in this directory:

```text
data/url_parser/url_previews/
```

Filenames contain the SHA-256 of the image URL and the validated extension.
The cache permits 128 entries and 128 MiB, with a 7-day retention period.
Each cache hit validates current URL/DNS policy and local image limits again.

## 📌 Site support

The plugin reads standard HTML metadata from the server response.
Pages with server-rendered titles, Open Graph data, and Twitter Cards can produce previews directly.
For pages with browser scripts, login sessions, or proprietary APIs, preview results depend on the initial HTML content.

## 🔐 Privacy and logs

Requests carry minimal public headers.
Request filtering excludes Core application credentials, cookies, plugin secrets, shared proxies, and user identities.
Logs contain the destination host, title length, status, and safe error category.

## 🩺 Troubleshooting

| Symptom | Check |
| --- | --- |
| Preview does not start | Make sure that the message contains one complete HTTP/HTTPS URL. |
| Text without an image | Examine image metadata, the image domain, and resource limits. |
| Empty summary | Examine title and description metadata in the original HTML. |
| Rejected request | Examine destination DNS, redirects, and public-address properties. |
| Cache write failure | Examine `data/url_parser/` permissions and disk space. |

## ✅ Development checks

Run these commands from the repository root:

```bash
python -m ruff check plugins/url_parser tests/plugins/url_parser/test_url_parser.py
python -m mypy plugins/url_parser
python -m pytest -q tests/plugins/url_parser/test_url_parser.py -n 2
```

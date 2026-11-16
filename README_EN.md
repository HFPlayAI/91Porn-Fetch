# 91Fetch

English | [简体中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-f5c542.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Version: 1.1.9](https://img.shields.io/badge/Version-1.1.9-e96b43.svg)](https://github.com/ChaosTechDev/91Fetch/releases)

## About

91Fetch is a local web application for browsing, collecting, and batch-downloading videos from the **91porn website**, designed to run directly on a Windows computer. It supports latest, currently popular, featured, daily ranking, monthly popular, most favorited, most discussed, HD, and original-video categories, along with author pages and custom listing URLs.

Listings show titles, thumbnails, authors, durations, view counts, and viewkeys. Select one video or the whole visible page and add it to the download queue without leaving the catalog. 91Fetch resolves actual `.m3u8` or `.mp4` media URLs and uses yt-dlp for concurrent HLS fragments, retries, and resume support.

Project homepage: [https://github.com/ChaosTechDev/91Fetch](https://github.com/ChaosTechDev/91Fetch)

## Edition Differences

- **Windows desktop edition (this repository and its Releases)**: extract the package and run `启动.cmd` for local use. It provides manual browsing, selection, and download queues; it does not include background scheduled downloads, container account login, or NAS-specific paths.
- **Docker edition**: a separate container deployment for NAS/servers, with scheduled downloads, inventory scanning, account login, and container path mappings. Docker files and NAS configuration are not included in this desktop Release.

Both editions share the site parser and download core, but their data directories and deployment methods are separate. Choose the edition that matches your environment and do not copy Docker environment variables or NAS paths into the desktop settings.

## Features

- Browse nine 91porn categories, author pages, and custom listing URLs
- Site-wide search by title or username, or paste a viewkey / video URL to open one video directly
- Cache listing pages locally (first page 5 min, deeper pages 30 min) and prefetch the next page; scrolling to the bottom loads more seamlessly
- Display titles, thumbnails, authors, durations, view counts, and viewkeys
- Select one or all videos and keep browsing after adding downloads
- Queue additional downloads while another batch is running
- Resolve `.m3u8` and `.mp4` streams with concurrent HLS fragments, retries, and resume support
- Filter downloads by all, incomplete, or completed; select, retry, and remove tasks
- "Download all incomplete" only re-queues the tasks listed in download management (the count is shown on the button and it is disabled at 0); removed tasks and never-downloaded catalog entries are no longer pulled back into the queue
- The bottom-left card shows live active download count, total speed, and queue size, refreshed at the configured interval; **pause / resume** is supported, pausing keeps the `.part` file and resuming continues from the breakpoint
- Stalled connections reconnect automatically: the site throttles per egress IP behind Cloudflare, squeezing a single connection down to tens of KB/s or even 99 B/s without closing TCP. When the real rate stays below 8 KB/s over a 40-second window the transfer is interrupted and resumed from `.part` (up to 2 reconnects), so tasks no longer hang forever at "downloading"
- Signed media URLs (valid for roughly 3 hours) are reused while fresh instead of re-resolving the video page, so restoring dozens of queued tasks does not fire a burst of requests that triggers the site's IP block (`403 error 1106`, lifts by itself in 1–2 minutes)
- Downloads share one **global queue**: every entry point (download selected / download all incomplete / resume / retry one) is capped by the configured parallel count. Set it to 2 and only 2 videos download at a time, the rest wait in order and a new one starts as soon as a slot frees up; a single connection is capped by the site CDN at roughly 150–300 KB/s, so throughput scales with the parallel count
- Nothing starts downloading at launch: tasks interrupted by a shutdown are marked paused and only continue when you click resume; download tasks, progress, and completion state are persisted
- Persist tasks, progress, and completion state; resume interrupted tasks after restart
- Scan the download directory, deduplicate by viewkey, and show downloaded status across categories
- Light and dark themes, list transitions, and partial progress updates
- Custom proxy: enter an address such as `127.0.0.1:7897` and every crawl, resolve, login, and download request goes through it, with a built-in connectivity test
- Site list: add other 91PORN sites, save them, and switch the active site from the top bar or the settings page; already-crawled entries are re-pointed to the new domain
- Local settings for download directory, archive layout, concurrency, and UI refresh interval

## Windows Quick Start

Windows 10/11 and Python 3.11 or newer are required. Download and extract the Release archive, then double-click `启动.cmd`. On first run it creates `.venv`, installs dependencies, generates `site.json`, starts the local service, and opens the web interface.

Application data is stored in `downloads/` by default, with videos in `downloads/videos/`. The video directory and archive layout can be changed in Settings.

The application serves its web interface locally and listens on `127.0.0.1:8765` by default. Keep the `downloads/` directory when upgrading because it contains task state and the local downloaded-file index.

Manual startup:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item site.example.json site.json
.\.venv\Scripts\python.exe -m viewkey_batch.web
```

The default address is `http://127.0.0.1:8765`. The application selects the next available local port when 8765 is occupied.

## Site Configuration and Rate Limits

`启动.cmd` creates `site.json` from `site.example.json` on first run. Edit it to adjust category URLs, request interval, jitter, retries, and backoff settings.

### Rule profiles (multiple site structures)

Mirror sites do not share one page structure, so switching the host alone would scrape nothing. Two rule profiles ship built in:

| Profile | Video page | Stream source | Applies to |
| --- | --- | --- | --- |
| `classic` | `/view_video.php?viewkey=xxx` | Inline in the detail page (`strencode2(...)`) | 91porn and mirrors with the same layout |
| `pinse` | `/v/475058` (ID in the path) | `POST /api/videos/<id>/playback` returning an m3u8 | 91pinse and mirrors with the same layout |

- The site list stores a `profile` plus the full `rules` for every entry in `downloads/sites.json`. Switching a site replaces the whole `site.json`, so keys that only exist in the previous profile are dropped instead of leaking into the new one.
- Adding a site with "auto detect" picks the profile from the hostname (anything containing `pinse` uses `pinse`) and falls back to `classic`.
- Categories the active site does not have are greyed out in the sidebar.
- `pinse`-style sites serve HLS with MPEG-TS segments; `imageio-ffmpeg` (bundled ffmpeg) remuxes them to standard MP4 without re-encoding. Without that dependency the file is kept as `.ts`, playable in VLC / PotPlayer / mpv.
- A new layout only needs a new entry in `src/viewkey_batch/profiles.py` (card selectors, how the ID is derived, how the stream is resolved); no parser changes required.

Requests are rate-limited by default. The client backs off on `429` and `5xx` responses and stops the current task when it detects a challenge page. Category browsing establishes a site session and adds a cache-busting query value to prevent stale cross-category results. Media pages are resolved again before each download to refresh expiring CDN URLs.

## Command Line

```powershell
91fetch crawl --category latest --max-pages 10
91fetch crawl --author AUTHOR_ID
91fetch download --workers 2 --fragments 4
91fetch run --category featured --workers 2 --fragments 4
```

The web interface is the default workflow. The CLI is intended for one-off scripted tasks.

## License

91Fetch is released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the software, provided that the copyright and license notices remain in copies or substantial portions of the software. The software is provided as-is without warranty.

Users are responsible for confirming that they may save downloaded content and for complying with the target site's terms and applicable local rules.

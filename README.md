# Video Scraper

A desktop application that scrapes a webpage for videos, previews them in
an embedded player, and downloads the ones you select.

Built with **PyQt6** and **yt-dlp** so it works with both arbitrary
webpages and 1000+ supported sites (YouTube, Bilibili, Twitter / X,
TikTok, Vimeo, Instagram, etc.).

## Features

- Paste any URL and discover all videos on that page.
- X / Twitter post links show one row per video, with in-app playback and
  download of the selected video.
- Four-stage extraction:
  1. **yt-dlp** for 1000+ supported sites,
  2. **HTML scan** for `<video>` / `<source>` / og:video / direct links,
  3. **JS-array obfuscation decoder** for self-hosted portals that
     scramble URLs into permutation arrays (e.g. papalah-style),
  4. **Headless WebEngine renderer** that runs JS and re-extracts when
     the static stages return nothing.
- Thumbnail + metadata for every result.
- Embedded media player to preview a clip before downloading.
- Smart playback buffer: every byte the proxy fetches is mirrored to a
  per-stream sparse temp file, and a background prefetcher reads ahead
  by a configurable amount of seconds (default 60s, max 300s) so
  scrubbing the timeline is instant inside the cached window. Adjust it
  from **Preferences** (⌘,) — cache lives in a temp folder that's wiped
  on app exit.
- Single selection: click a video to preview it, then download that current
  video.
- Quality picker (best / 1080p / 720p / 480p / audio only).
- Automatic ``Referer`` header for sites that hot-link-protect their CDN.
- Custom output folder, with progress bar and live log.

## Requirements

- Python 3.10+
- macOS / Linux / Windows
- [`ffmpeg`](https://ffmpeg.org/) on the `PATH` is recommended so yt-dlp
  can merge separate video + audio streams.

On macOS:

```bash
brew install ffmpeg
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

## Usage

1. Paste a URL into the input box and click **Scrape** (or press Enter).
2. Browse the discovered videos in the left list. Click any item to load
   metadata and thumbnail on the right; click **Play** to preview it
   inline when the source is directly playable.
3. Click the video you want to download.
4. (Optional) Change quality and choose a download folder.
5. Click **Download current**.

For an X / Twitter post, paste its `x.com` or `twitter.com` status link.
If X requires a login, first sign in to X in a supported browser, then
choose that browser from **X session** beside the URL and scrape again.
The app reads the browser session only when you select it; it does not
ask for or save your X password, and does not export cookies to a file.
Private or restricted posts are available only if your account can view
them. Select the video you want from the list when a post has multiple
videos, and use **Play** to preview it before downloading.

Files are saved to `./downloads` by default.

## Project layout

```
video scrap/
├── main.py
├── requirements.txt
├── README.md
├── downloads/                # auto-created when needed
└── src/
    ├── scraper.py            # yt-dlp + HTML scraping + decoder pipeline
    ├── downloader.py         # yt-dlp download wrapper with progress
    ├── js_decoder.py         # decodes obfuscated JS-array video URLs
    ├── media_proxy.py        # loopback HTTP proxy + sparse cache + prefetch
    ├── settings.py           # persistent app preferences
    ├── utils.py
    └── gui/
        ├── main_window.py
        ├── video_list.py     # left list with thumbnails
        ├── preview_panel.py  # right preview + embedded player
        ├── settings_dialog.py # Preferences dialog (playback buffer)
        ├── js_renderer.py    # headless QWebEnginePage fallback
        └── workers.py        # QThread workers
```

## Notes

- Inline preview works best on direct media URLs (e.g. `.mp4`, `.webm`).
  For some yt-dlp sources the streamable URL may not be playable
  directly in Qt's media backend; the download itself still works fine.
- Some pages require login or are unavailable from a particular region.
  For X posts, use the **X session** browser selector when your account
  can view the post. Other sites may need the interactive browser mode.

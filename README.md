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
- Black-and-white themes: charcoal dark mode by default, with **Light mode** /
  **Dark mode** in the top-right toolbar. The last choice is saved across restarts.

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

## macOS app

The packaged app runs without a separate Python installation. It includes
Qt WebEngine, the media player backend and FFmpeg. Open `Video Scraper.app`
by double-clicking it, or copy it into Applications. The app saves downloads
to `~/Downloads/Video Scraper` by default; use **Choose…** to change the folder.
Source runs still use `./downloads`.

To rebuild with the isolated build environment prepared for this project:

```bash
build/.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
build/.venv/bin/python scripts/build_macos.py
```

Outputs are `dist/Video Scraper.app`, a ZIP preserving the bundle's symbolic
links, and `dist/build-info.json` with dependency versions and source hashes.
Build logs are saved under `build/macos/build.log`. The current build targets
Apple Silicon and macOS 13 or newer. It uses an ad-hoc signature; a Developer
ID signature and notarization are not configured.
For a fresh build, create `build/.venv` using a Python runtime compiled for
the intended macOS version. The builder checks the deployment targets of
Python, Qt, FFmpeg and the resulting bundle: a Python runtime compiled only
for macOS 27 also makes the resulting app require macOS 27. This build uses
Python 3.12.14 with a macOS 11 deployment target; Qt sets the final minimum
to macOS 13. Minimum-version metadata has been checked; actual execution has
been tested on the current macOS 27.0.1 host.

The bundled offline check uses temporary settings, a local test server and
generated sample media, without reading browser login information:

```bash
"dist/Video Scraper.app/Contents/MacOS/Video Scraper" --smoke-test /tmp/video-scraper-smoke.json
```

Packaging uses [PyInstaller](https://pyinstaller.org/en/stable/usage.html)
and the FFmpeg executable supplied by
[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg).
Third-party license texts are included in the app's Resources directory.

## Windows app

Build the Windows x64 version on Windows using 64-bit Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

The result is `dist/Video Scraper/Video Scraper.exe` with its dependency folder
and `dist/VideoScraper-Windows-x64.zip`. Distribute the entire ZIP; keep the
executable and `_internal` folder together. Both builds use the root `Logo.png`
for the application icon. The Windows build script bundles FFmpeg and uses the
same offline check as the macOS build. Windows execution has not yet been tested.

See [the Windows step-by-step guide](WINDOWS_BUILD.md) for source copying,
verification and troubleshooting. PyInstaller requires separate builds on
each [target operating system](https://pyinstaller.org/en/stable/).

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

# Video Scraper

<img src="Logo.png" alt="Video Scraper application icon" width="160" />

Paste a webpage or X (formerly Twitter) post link to discover videos, preview them in the app, and download the selected video.
The interface uses PyQt6, with extraction handled by yt-dlp, HTML parsing, and Qt WebEngine.

## Current Status

As of October 6, 2026 (UTC+08:00):

| Component | Status |
| --- | --- |
| Webpage video extraction, preview, and downloads | Implemented |
| X / Twitter posts, individual video selection, playback, and downloads | Implemented |
| Audio-only downloads with Original audio / MP3 / M4A selection | Included and checked in the rebuilt macOS app; latest Windows build not verified here |
| About dialog with application logo and GitHub project link | Included and checked in the rebuilt macOS app; latest Windows build not verified here |
| Dark and light themes with saved preferences | Implemented |
| Sectioned layout, monochrome icons, and More menu | Included and checked in the macOS app; latest Windows build not verified here |
| Fullscreen preview, volume, and mute controls | Included and checked in the macOS app; latest Windows build not verified here |
| Single-instance startup with existing-window activation | Included and checked in the macOS app; latest Windows build not verified here |
| Application icon | Uses `Logo.png` from the repository root |
| macOS Apple Silicon application | `.app` and ZIP built; application checks passed |
| Windows x64 application | ZIP built; startup, playback, and downloads tested on Windows |

The macOS application has been checked for startup, theme switching and persistence, exact byte matching for direct downloads, HLS byte-range downloading and merging, playback, and embedded webpage JavaScript.
All 90 tests across the existing 11 test scripts passed on macOS with Python 3.12.14.
Windows startup, playback, and downloads have been tested. Automated Windows regression and smoke-test reports have not been collected.
The macOS app was rebuilt as version 0.2.0 on October 5, 2026, with the revised interface, Preferences dialog, fullscreen, volume, and single-instance startup. All 10 packaged application checks passed. A Windows ZIP is also present locally, but its source revision and validation of the new features have not been verified here.
The source update passed 12 single-instance checks, 9 preview-control checks, 5 desktop layout checks, and 9 Preferences dialog checks. The existing X preview and settings checks also passed. Native macOS playback checks confirm video frames continue through fullscreen transitions.
The audio-download update was rebuilt for macOS on October 6, 2026. All 11 packaged checks passed, including Original audio / MP3 / M4A output from direct links, HLS, and separate HLS audio renditions. The source update passed 14 real-media audio checks, 6 audio UI/worker checks, and the existing 18 HLS byte-range, 10 download-name, 4 X-download, and 5 desktop layout checks. The new Windows audio feature has not been built or tested here.
The About dialog update was rebuilt for macOS on October 6, 2026. All 12 packaged checks passed, including the bundled logo and About controls. The dialog displays the application name, version, and GitHub project link without implementation details.

## Features

- Paste a webpage link to search for videos on the page.
- Paste an `x.com` or `twitter.com` post link. Posts containing multiple videos show a separate entry for each video.
- Preview videos in the app and view thumbnails, duration, resolution, and source information.
- Use **Full screen** or double-click the preview to enlarge it, including before selecting a video. The empty preview displays a selection prompt. Press **Esc** or **Exit full screen** to return; playback and the current position are preserved.
- A toolbar with monochrome icons and text, a **More** menu for **Preferences / About**, a separate seek row, and two rows for quality, download, and folder controls.
- **More → About** displays the application logo and version. Use **GitHub project** to open the project homepage in your browser.
- Compact video thumbnails and a wider preview area. Long titles and metadata are limited to two visible lines, with full text in tooltips; folder paths adapt to the available width. Small windows expand when necessary to keep controls visible.
- Adjust the preview volume from 0 to 100%, or use **Mute / Unmute**. These controls affect the app's player, not the system volume. Volume starts at 60% each time the app opens.
- Repeated launches activate the existing window, including a minimized or fullscreen window, instead of opening another copy for the same user.
- Select one video and download the current selection.
- Choose best, 1080p, 720p, 480p, or audio only. Audio-only downloads offer **Original audio**, **MP3**, or **M4A**; available source formats depend on the source.
- Download progress, logs, and a custom output folder.
- Parallel downloads for direct links, HLS segment downloads, and FFmpeg merging.
- HTTP byte-range validation to prevent incorrect response data from being written to segment files. Downloads with the same name use separate temporary files and avoid overwriting existing completed files.
- Adjustable playback buffer in **More → Preferences**: 60 seconds by default, configurable from 0 to 300 seconds. Set it to 0 to disable read-ahead.
- Black-and-white themes: dark mode by default, with **Light mode / Dark mode** in the top-right toolbar. The choice is saved across restarts.
- A browser extraction entry in the toolbar for pages requiring manual verification or login.

Extraction tries yt-dlp, HTML scanning, page script decoding, and WebEngine rendering through the existing workflow.

## Usage

1. Paste a link and click **Scrape**, or press Enter.
2. Select a video in the list on the left.
3. Click **Play** to preview it in the app.
4. Choose a quality setting and use **Choose…** to select the output folder if needed.
5. Click **Download current** to download the selected video.

### Audio-only Downloads

Select **audio only** under **Quality** to reveal the **Audio format** control:

| Format | Output behavior |
| --- | --- |
| Original audio (default) | Extracts the audio without recompressing it. The source codec determines the appropriate container and extension, such as Opus (`.opus`), M4A, or MP3. |
| MP3 | Produces an audio-only `.mp3` file. Conversion may reduce quality when the source uses another codec. |
| M4A | Produces an audio-only `.m4a` file using AAC. Existing compatible AAC can be copied; other codecs require conversion, which may reduce quality. |

All download routes, including direct media links and HLS, apply the audio-only selection. If the source has no audio track, the task fails with a clear message instead of returning a video-only file. Sources without a separate audio stream may require the complete media to be downloaded first before extracting its audio. Intermediate media is temporary and is cleaned up after processing, cancellation, or failure; a forced process termination can leave temporary files behind.
For an HLS master with separate audio, the app selects the default audio rendition from the selected video variant's audio group. It retains the default embedded track when that rendition has no separate URL. Audio segment downloads use the same byte-range checks as video downloads.

Audio extraction and conversion require FFmpeg; the packaged app includes it. Each queued task keeps the format selected when you clicked **Download current**, even if you change the controls afterward. A processing message is shown while extraction or conversion runs.
Use the macOS package rebuilt on October 6, 2026, for this feature. Earlier version 0.2.0 packages lack these audio-format controls; rebuild on Windows to include the update there.

Fullscreen mode retains playback, seek, volume, and mute controls. Fullscreen switching also works while paused and does not restart the video.
Close all older app windows before using a rebuilt version: older binaries do not participate in single-instance protection.

### X / Twitter Login

If a post requires login, first sign in to X in a supported browser, select that browser under **X session**, and scrape again.
The app reads the session only when you select the browser. It does not request or save your X password, or export cookies to a file.
Access to restricted posts depends on whether the account has permission to view them.

### File Locations

| Run mode | Default download folder |
| --- | --- |
| Running from source | `downloads/` in the current working directory |
| macOS application | `~/Downloads/Video Scraper` |
| Windows application | `%USERPROFILE%\Downloads\Video Scraper` |

Settings are stored at `~/Library/Application Support/VideoScraper/settings.json` on macOS and `%APPDATA%\VideoScraper\settings.json` on Windows.
The playback cache uses a temporary directory and is cleared on normal app exit. No database or Redis configuration is required.

## Get and Update the Repository

For a new checkout:

```bash
git clone https://github.com/XXD051030/video-scrap.git
cd video-scrap
```

To update an existing checkout, run this from the project root:

```bash
git pull --ff-only
```

`Logo.png`, build scripts, and build configurations are included in the repository. `build/`, `dist/`, virtual environments, and downloaded files are excluded by `.gitignore` and must be generated on the target system.

## Run from Source

Python 3.10 or newer is required; 64-bit Python 3.12 is recommended.
Runtime dependencies are listed in `requirements.txt`.

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

When running from source, HLS merging and some video/audio merging operations require the FFmpeg command-line tool.
On macOS, install it with:

```bash
brew install ffmpeg
```

The build scripts below bundle FFmpeg with the application. Packaged applications do not require a separate Python or FFmpeg installation.

## Build the macOS Application

Run these commands on macOS. For the first build, create a separate build environment:

```bash
python3.12 -m venv build/.venv
build/.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
build/.venv/bin/python scripts/build_macos.py
```

For subsequent builds using an existing `build/.venv`, run the final command again.
The application generates an `.icns` icon from `Logo.png` and includes Python, the player, Qt WebEngine, and FFmpeg.

Output:

```text
dist/
├── Video Scraper.app
├── VideoScraper-macOS-arm64.zip
└── build-info.json
```

The current application targets Apple Silicon. The script builds for the architecture of the Python runtime used to execute it, and the ZIP filename varies accordingly.
Double-click the `.app` to run it, or move it to Applications.

### System Requirements and Validation Scope

The current application binaries require macOS 13 or newer. Actual execution was tested on macOS 27.0.1.
The build uses Python 3.12.14 with a macOS 11 deployment target; Qt raises the final minimum requirement to macOS 13.

The builder reads the deployment requirements of Python, Qt, FFmpeg, and the binaries in the completed application.
If Python was compiled only for macOS 27, rebuilding with that runtime also produces an application requiring macOS 27.
The current application uses ad-hoc signing. Developer ID signing and notarization are not configured.

## Build the Windows Application

Use 64-bit Python 3.12 in a Windows x64 environment. For the first build:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

There is no need to activate the virtual environment or change the PowerShell execution policy.
The script generates an `.ico` icon from `Logo.png` and collects Windows versions of Python, Qt WebEngine, the player, yt-dlp, and FFmpeg.

Output:

```text
dist/
├── Video Scraper/
│   ├── Video Scraper.exe
│   └── _internal/
├── VideoScraper-Windows-x64.zip
└── build-info-Windows.json
```

Double-click `Video Scraper.exe` to run it. To use it on another computer, send the complete ZIP and extract it before launching.
The `.exe` and `_internal/` folder must remain together in the same application directory. Copying only the `.exe` is insufficient.

A Windows x64 ZIP has been produced and tested for startup, playback, and downloads. An automated Windows smoke-test report has not been collected. Code signing is not configured.
See the [Windows build guide (Chinese)](WINDOWS_BUILD.md) for detailed instructions and troubleshooting.
PyInstaller requires separate builds on each [target operating system](https://pyinstaller.org/en/stable/); macOS cannot directly produce the Windows build.

## Offline Application Checks

The checks use temporary settings, a local test webpage, and generated sample videos. They do not read browser login information.
They verify downloaded bytes, HLS merging, audio-only extraction and conversion (including separate HLS audio renditions), playback through fullscreen transitions, volume and mute, webpage JavaScript, themes, and settings persistence.
The playback check briefly opens the app and fullscreen preview using a generated sample video.

### macOS

```bash
"dist/Video Scraper.app/Contents/MacOS/Video Scraper" --smoke-test /tmp/video-scraper-smoke.json
cat /tmp/video-scraper-smoke.json
```

### Windows PowerShell

```powershell
$report = Join-Path $PWD "build\windows\smoke-report.json"
$appProcess = Start-Process -FilePath ".\dist\Video Scraper\Video Scraper.exe" -ArgumentList @("--smoke-test", ('"' + $report + '"')) -Wait -PassThru
$appProcess.ExitCode
Get-Content $report
```

Successful checks return exit code `0` and set `ok` to `true` in the report.
After the offline checks pass, use your own webpage or X links to verify real usage scenarios.

### Audio Source Regression Checks

With the project build environment and FFmpeg installed, run:

```bash
python tests/test_audio_downloads.py
python tests/test_audio_download_controls.py
```

These checks generate temporary media and use localhost instead of external websites. They verify pure audio output, unchanged compressed packets for Original audio, HLS audio selection and range failures, cancellation and cleanup, concurrent filenames, queued format selection, and control layout. They do not establish Windows compatibility when run on macOS.

## Logs and Build Records

| Item | Path |
| --- | --- |
| macOS build log | `build/macos/build.log` |
| Windows build log | `build/windows/build.log` |
| macOS build record | `dist/build-info.json` |
| Windows build record | `dist/build-info-Windows.json` |

Build records include Python and dependency versions, SHA-256 hashes of source files and ZIP archives, and Git commit information to help identify where an application build came from.
Build tools are pinned in `requirements-build.txt`; FFmpeg comes from [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg).
Third-party license texts are bundled in the `third-party/` resource directory.

## Project Layout

```text
video-scrap/
├── main.py                         # Application entry point
├── Logo.png                        # Shared macOS / Windows icon
├── requirements.txt                # Runtime dependencies
├── requirements-build.txt          # Build tools
├── README.md
├── WINDOWS_BUILD.md
├── app_bundle/
│   ├── VideoScraper.spec            # macOS configuration
│   ├── VideoScraper-Windows.spec    # Windows configuration
│   └── runtime_hook.py             # Exposes the bundled FFmpeg executable
├── scripts/
│   ├── build_macos.py
│   ├── build_windows.py
│   └── smoke_app.py                # Offline application checks
├── src/
│   ├── scraper.py                  # Webpage / X video extraction
│   ├── downloader.py               # Downloads and HLS merging
│   ├── parallel_downloader.py      # Parallel direct downloads and file publishing
│   ├── media_proxy.py              # Playback proxy and cache
│   ├── settings.py                 # Persistent settings
│   ├── single_instance.py          # Process lock and existing-window activation
│   ├── js_decoder.py
│   ├── net.py
│   ├── utils.py
│   └── gui/                        # Interface, player, browser, and background tasks
└── tests/                          # Existing regression tests
```

## Known Limitations

- Extraction, playback, and downloads depend on the media formats provided by the source, account permissions, and network conditions.
- Some yt-dlp sources provide streams that cannot be previewed directly in Qt, although downloading may still work.
- The current HLS implementation does not support some combinations of initialization segments and byte ranges, or special encrypted range formats. It reports an error rather than continuing when it cannot handle them safely.
- macOS test results do not establish Windows compatibility. Windows validation currently covers startup, playback, and downloads; automated regression and smoke-test results have not been collected on Windows.

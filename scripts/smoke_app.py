"""Offline checks invoked by the packaged app's --smoke-test command."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def run_smoke_test(report_path: str) -> int:
    """Also record dependency import failures before Qt can be created."""
    try:
        return _run_smoke_test(report_path)
    except Exception:
        report = {"ok": False, "python": sys.version,
                  "frozen": bool(getattr(sys, "frozen", False)),
                  "bundle_path": str(getattr(sys, "_MEIPASS", "")),
                  "traceback": traceback.format_exc()}
        destination = Path(report_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1


def _run_smoke_test(report_path: str) -> int:
    """Exercise bundled dependencies and media paths using only localhost."""
    from PyQt6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QUrl
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from Crypto.Cipher import AES
    import certifi
    import yt_dlp.extractor
    import src.settings as settings_module
    import src.gui.main_window as window_module
    from src.downloader import VideoDownloader
    from src.scraper import VideoItem

    report_file = Path(report_path).expanduser().resolve()
    report: dict = {
        "ok": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version,
        "bundle_path": str(getattr(sys, "_MEIPASS", "")),
        "checks": {},
        "network": "127.0.0.1 only; no browser credentials",
    }
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("Video Scraper Smoke Test")
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    report["qt_platform"] = app.platformName()
    temp = tempfile.TemporaryDirectory(prefix="video-scrap-smoke-")
    root = Path(temp.name).resolve()
    old_settings = settings_module.SETTINGS_PATH
    old_downloads = window_module.DEFAULT_DOWNLOAD_DIR
    settings_module.SETTINGS_PATH = root / "settings.json"
    window_module.DEFAULT_DOWNLOAD_DIR = root / "downloads"
    window = profile = page = server = server_thread = downloader = None
    requests_seen: list[tuple[str, str | None]] = []

    def check(name, operation):
        try:
            result = operation()
            report["checks"][name] = {"ok": True, "detail": result}
            return result
        except Exception:
            report["checks"][name] = {
                "ok": False, "traceback": traceback.format_exc()
            }
            raise

    def wait_until(predicate, message: str, timeout: int = 15000):
        loop = QEventLoop()
        deadline, poll = QTimer(), QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline.start(timeout)
        poll.start(20)
        app.processEvents()
        if not predicate():
            loop.exec()
        poll.stop()
        deadline.stop()
        app.processEvents()
        if not predicate():
            raise RuntimeError(message)

    def run_ffmpeg(*args):
        result = subprocess.run(
            [report["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr or f"ffmpeg exited {result.returncode}")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                # Playback may close its keep-alive socket during cleanup.
                pass

        def log_message(self, *_args):
            pass

        def do_HEAD(self):  # noqa: N802
            self.respond(False)

        def do_GET(self):  # noqa: N802
            self.respond(True)

        def respond(self, body):
            path = (root / unquote(urlsplit(self.path).path).lstrip("/")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            range_header = self.headers.get("Range")
            requests_seen.append((path.name, range_header))
            start, end, status = 0, len(data) - 1, 200
            if range_header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
                if not match or int(match[1]) >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else end, end)
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0]
                             or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            if body:
                try:
                    self.wfile.write(data[start:end + 1])
                except (BrokenPipeError, ConnectionResetError):
                    pass

    try:
        def dependencies():
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("Bundled ffmpeg is missing from PATH")
            report["ffmpeg"] = ffmpeg
            extractor = yt_dlp.extractor.get_info_extractor("Twitter")
            assert extractor is not None
            assert extractor().ie_key() == "Twitter"
            assert AES.new(bytes(16), AES.MODE_CBC, bytes(16)).encrypt(bytes(16))
            ca_file = Path(certifi.where())
            assert ca_file.is_file() and ca_file.stat().st_size > 0
            return {"twitter_extractor": extractor.__name__, "certifi": str(ca_file)}

        check("dependencies", dependencies)
        window = check("main_window", lambda: window_module.MainWindow())
        # The report contains plain JSON, not the QWidget returned by construction.
        report["checks"]["main_window"]["detail"] = {
            "proxy_port": window.media_proxy.port,
            "theme": window._theme_name,
        }

        def themes():
            initial = window._theme_name
            window.theme_action.trigger()
            assert window._theme_name != initial
            assert settings_module.AppSettings.load().theme == window._theme_name
            window.theme_action.trigger()
            assert window._theme_name == initial
            assert settings_module.AppSettings.load().theme == initial
            return "Both themes switched and persisted in temporary settings"

        check("themes", themes)

        def preferences():
            from PyQt6.QtWidgets import QDialogButtonBox
            from src.gui.settings_dialog import SettingsDialog

            initial = window.settings_store.get()
            cancelled = SettingsDialog(window.settings_store, window)
            cancelled.show()
            app.processEvents()
            cancelled.buffer_spin.setValue(0)
            cancelled.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
            assert window.settings_store.get() == initial
            cancelled.deleteLater()

            saved = SettingsDialog(window.settings_store, window)
            saved.show()
            app.processEvents()
            assert saved.save_button.height() == 38
            assert not saved.save_button.icon().isNull()
            saved.buffer_spin.setValue(85)
            saved.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
            current = window.settings_store.get()
            assert current.playback_buffer_seconds == 85 and current.theme == initial.theme
            assert settings_module.AppSettings.load() == current
            saved.deleteLater()
            window.settings_store.update(initial)
            app.processEvents()
            return "Preferences Save persists buffer; Cancel preserves values; theme retained"

        check("preferences_dialog", preferences)

        def about():
            from src.gui.about_dialog import AboutDialog, GITHUB_URL

            dialog = AboutDialog(window)
            dialog.show()
            app.processEvents()
            assert not dialog.logo_label.pixmap().isNull()
            assert dialog.logo_label.isVisible()
            assert dialog.github_button.toolTip() == GITHUB_URL
            assert dialog.github_button.isVisible()
            assert dialog.version_label.text().startswith("Version ")
            dialog.close_button.click()
            assert not dialog.isVisible()
            dialog.deleteLater()
            app.processEvents()
            return {"logo_loaded": True, "github_url": GITHUB_URL, "close_button": True}

        check("about_dialog", about)

        def empty_fullscreen():
            panel = window.preview_panel
            panel.show_video(None)
            window.show()
            assert panel.fullscreen_button.isEnabled()
            assert panel.player.source().isEmpty()
            assert panel.thumbnail_label.text() == "Select a video to preview"
            assert not panel.play_button.isEnabled()
            assert window.prefs_action in window.actions()
            assert window.about_action in window.more_menu.actions()
            for fullscreen in (True, False):
                panel.fullscreen_button.click()
                app.processEvents()
                assert panel.is_fullscreen() == fullscreen
                assert panel.player.source().isEmpty()
                assert panel.fullscreen_button.isEnabled()
            window.hide()
            return "Fullscreen works without a link or media; More menu actions retained"

        check("empty_preview_fullscreen", empty_fullscreen)

        def fixtures():
            run_ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=15",
                       "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                       "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                       "-g", "15", "-c:a", "aac", "-movflags", "+faststart",
                       str(root / "sample.mp4"))
            run_ffmpeg("-i", str(root / "sample.mp4"), "-c", "copy", "-hls_time", "1",
                       "-hls_list_size", "0", "-hls_flags", "single_file",
                       "-hls_segment_filename", str(root / "media.ts"),
                       str(root / "sample.m3u8"))
            run_ffmpeg("-i", str(root / "sample.mp4"), "-map", "0:a:0", "-c", "copy",
                       "-hls_time", "1", "-hls_list_size", "0", "-hls_flags", "single_file",
                       "-hls_segment_filename", str(root / "audio.ts"),
                       str(root / "sample-audio.m3u8"))
            (root / "master-audio.m3u8").write_text(
                '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Original",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="sample-audio.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=160x90,AUDIO="sound"\n'
                'missing-video.m3u8\n', encoding="utf-8"
            )
            manifest = (root / "sample.m3u8").read_text()
            assert manifest.count("#EXT-X-BYTERANGE:") >= 2
            (root / "index.html").write_text(
                "<!doctype html><title>Offline smoke</title>"
                "<script>window.smokeReady = 6 * 7;</script>", encoding="utf-8"
            )
            return "8 second H.264/AAC sample; multi-segment byte-range HLS"

        check("media_fixtures", fixtures)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        downloader = VideoDownloader(root / "downloads", parallel_connections=2)
        direct = VideoItem("Smoke direct", base + "/sample.mp4", base,
                           ext="mp4", is_direct=True, referer=base)

        def direct_download():
            downloaded = downloader.download(direct)
            assert downloaded.read_bytes() == (root / "sample.mp4").read_bytes()
            return {"bytes": downloaded.stat().st_size, "exact_match": True}

        check("direct_download", direct_download)

        def hls_download():
            item = VideoItem("Smoke HLS", base + "/sample.m3u8", base,
                             ext="m3u8", is_direct=True, is_hls=True)
            downloaded = downloader.download(item)
            run_ffmpeg("-xerror", "-i", str(downloaded), "-f", "null", "-")
            ranges = [header for name, header in requests_seen
                      if name == "media.ts" and header]
            assert len(ranges) >= 2
            assert downloaded.stat().st_size > 0
            return {"ranges": ranges, "decoded": True}

        check("hls_byte_range_download", hls_download)

        def audio_downloads():
            window.quality_combo.setCurrentText("audio only")
            window.show()
            app.processEvents()
            assert window.audio_format_combo.isVisible()
            assert window.audio_format_combo.currentData() == "original"
            results = []
            hls = VideoItem("Smoke HLS audio", base + "/sample.m3u8", base,
                            ext="m3u8", is_direct=True, is_hls=True)
            external_audio = VideoItem("Smoke separate audio", base + "/master-audio.m3u8", base,
                                       ext="m3u8", is_direct=True, is_hls=True)
            for item in (direct, hls, external_audio):
                for audio_format, extension, codec in (
                    ("original", ".m4a", "aac"),
                    ("mp3", ".mp3", "mp3"),
                    ("m4a", ".m4a", "aac"),
                ):
                    downloaded = downloader.download(
                        item, quality="audio only", audio_format=audio_format
                    )
                    assert downloaded.suffix == extension
                    probe = subprocess.run(
                        [report["ffmpeg"], "-hide_banner", "-nostdin", "-i", str(downloaded)],
                        capture_output=True, text=True, errors="replace", timeout=30,
                    )
                    assert probe.returncode == 1
                    streams = [line for line in probe.stderr.splitlines()
                               if re.match(r"\s*Stream #", line)]
                    assert len(streams) == 1 and f"Audio: {codec}" in streams[0]
                    run_ffmpeg("-xerror", "-i", str(downloaded), "-f", "null", "-")
                    route = ("hls_external_audio" if item is external_audio
                             else "hls" if item.is_hls else "direct")
                    results.append({"route": route,
                                    "format": audio_format, "codec": codec,
                                    "audio_only": True, "decoded": True})
            assert not list((root / "downloads").glob(".video-scrap-job-*"))
            window.quality_combo.setCurrentText("best")
            app.processEvents()
            assert window.audio_format_row.isHidden()
            window.hide()
            return results

        check("audio_only_downloads", audio_downloads)

        def preview():
            panel = window.preview_panel
            from PyQt6.QtMultimedia import QMediaPlayer

            frames = []

            def record_frame(frame):
                if frame.isValid():
                    frames.append(frame.startTime())

            panel.video_widget.videoSink().videoFrameChanged.connect(record_frame)
            panel.audio_output.setMuted(True)
            panel.show_video(direct)
            window.show()
            panel.play_button.click()
            wait_until(lambda: panel.player.position() > 0 and bool(frames),
                       f"Playback did not advance: {panel.player.errorString()}")
            result = {"position_ms": panel.player.position(),
                      "proxy_url": panel.player.source().toString()}
            assert result["proxy_url"].startswith(window.media_proxy.base_url)

            player, audio = panel.player, panel.audio_output
            source = player.source()
            for fullscreen in (True, False):
                before_position, before_frames = player.position(), len(frames)
                panel.fullscreen_button.click()
                assert panel.is_fullscreen() == fullscreen
                assert panel.player is player and panel.audio_output is audio
                assert player.source() == source
                assert player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                wait_until(
                    lambda: player.position() > before_position + 120
                    and len(frames) > before_frames,
                    "Playback or video frames stopped after fullscreen switch",
                )

            panel.play_button.click()
            assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            paused_position = player.position()
            for fullscreen in (True, False):
                panel.fullscreen_button.click()
                app.processEvents()
                assert panel.is_fullscreen() == fullscreen
                assert player.playbackState() == QMediaPlayer.PlaybackState.PausedState
                assert player.source() == source and player.position() == paused_position

            initial_volume = audio.volume()
            panel.volume_slider.setValue(25)
            assert abs(audio.volume() - 0.25) < 0.001
            assert not audio.isMuted()
            panel.mute_button.click()
            assert audio.isMuted() and abs(audio.volume() - 0.25) < 0.001
            panel.mute_button.click()
            assert not audio.isMuted()
            # Keep the offline fixture silent and restore the default level.
            audio.setMuted(True)
            panel.volume_slider.setValue(round(initial_volume * 100))
            audio.setMuted(True)
            result.update({"fullscreen_playback": True, "fullscreen_paused": True,
                           "video_frames": len(frames), "volume_and_mute": True})
            panel.release_stream()
            panel.video_widget.videoSink().videoFrameChanged.disconnect(record_frame)
            window.hide()
            return result

        check("qt_multimedia_proxy_playback", preview)
        profile = QWebEngineProfile()
        assert profile.isOffTheRecord()
        page = QWebEnginePage(profile)

        def browser():
            loaded, values = [], []
            page.loadFinished.connect(loaded.append)
            page.load(QUrl(base + "/index.html"))
            wait_until(lambda: bool(loaded), "QtWebEngine page load timed out")
            assert loaded[-1], "QtWebEngine failed to load localhost page"
            page.runJavaScript("window.smokeReady", values.append)
            wait_until(lambda: bool(values), "QtWebEngine JavaScript timed out")
            assert values[-1] == 42
            return {"javascript": values[-1], "off_the_record": True}

        check("qt_webengine", browser)
        report["ok"] = True
    except Exception:
        report["traceback"] = traceback.format_exc()
    finally:
        def cleanup(operation):
            try:
                operation()
            except Exception:
                report["ok"] = False
                report.setdefault("cleanup_tracebacks", []).append(traceback.format_exc())

        for qt_object in (page, profile):
            if qt_object is not None:
                cleanup(qt_object.deleteLater)
                cleanup(lambda: QCoreApplication.sendPostedEvents(
                    None, QEvent.Type.DeferredDelete))
        if window is not None:
            cleanup(window.close)
            cleanup(window.deleteLater)
            cleanup(lambda: QCoreApplication.sendPostedEvents(
                None, QEvent.Type.DeferredDelete))
        if downloader is not None:
            cleanup(downloader.close)
        if server is not None:
            cleanup(server.shutdown)
            cleanup(server.server_close)
        if server_thread is not None:
            cleanup(lambda: server_thread.join(timeout=2))
        cleanup(app.processEvents)
        settings_module.SETTINGS_PATH = old_settings
        window_module.DEFAULT_DOWNLOAD_DIR = old_downloads
        cleanup(temp.cleanup)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1

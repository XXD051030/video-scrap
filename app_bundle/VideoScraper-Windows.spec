"""Windows x64 onedir app; build on Windows with scripts/build_windows.py."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


if sys.platform != "win32":
    raise SystemExit("Build the Windows application on Windows.")
project = Path(SPECPATH).parent
assets = project / "build" / "windows-app-assets"
ffmpeg = assets / "bin" / "ffmpeg.exe"
icon = assets / "VideoScraper.ico"
if not ffmpeg.is_file() or not icon.is_file():
    raise SystemExit("Prepare build assets with scripts/build_windows.py first.")

a = Analysis(
    [str(project / "main.py")],
    pathex=[str(project)],
    binaries=[(str(ffmpeg), "bin")],
    datas=[
        (str(project / "Logo.png"), "."),
        (str(assets / "third-party"), "third-party"),
    ] + copy_metadata("yt-dlp"),
    # Let PyInstaller's Qt hooks collect WebEngine helpers/resources and codecs.
    hiddenimports=collect_submodules("yt_dlp.extractor") + [
        "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project / "app_bundle" / "runtime_hook.py")],
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Video Scraper",
    icon=str(icon),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Video Scraper")

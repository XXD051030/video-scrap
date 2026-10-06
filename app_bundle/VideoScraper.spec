"""Native macOS app; build with scripts/build_macos.py."""

from pathlib import Path
import os
import platform

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


project = Path(SPECPATH).parent
assets = project / "build" / "app-assets"
ffmpeg = assets / "bin" / "ffmpeg"
if not ffmpeg.is_file():
    raise SystemExit("Prepare build assets with scripts/build_macos.py first.")

a = Analysis(
    [str(project / "main.py")],
    pathex=[str(project)],
    binaries=[(str(ffmpeg), "bin")],
    datas=[(str(assets / "third-party"), "third-party"), (str(project / "Logo.png"), ".")] + copy_metadata("yt-dlp"),
    hiddenimports=collect_submodules("yt_dlp.extractor"),
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=platform.machine(),
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Video Scraper")
app = BUNDLE(
    coll,
    name="Video Scraper.app",
    icon=str(assets / "Logo.icns"),
    bundle_identifier="local.videoscraper.app",
    version="0.2.0",
    info_plist={
        "CFBundleDisplayName": "Video Scraper",
        "CFBundleShortVersionString": "0.2.0",
        "LSMinimumSystemVersion": os.environ["VIDEO_SCRAPER_MINIMUM_MACOS"],
        "NSHighResolutionCapable": True,
        "NSDownloadsFolderUsageDescription": "Save the videos you select to your Downloads folder.",
        "NSHumanReadableCopyright": "Third-party licenses are included in Contents/Resources/third-party.",
    },
)

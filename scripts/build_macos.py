"""Build a native .app and a ZIP that preserves its framework symlinks."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[1]
ASSETS = PROJECT / "build" / "app-assets"
RUNTIME_DISTRIBUTIONS = (
    "PyQt6", "PyQt6-Qt6", "PyQt6-sip", "PyQt6-WebEngine", "PyQt6-WebEngine-Qt6",
    "yt-dlp", "requests", "urllib3", "certifi", "charset-normalizer", "idna",
    "beautifulsoup4", "soupsieve", "lxml", "Pillow", "pycryptodome", "imageio-ffmpeg",
)


def minimum_macos(paths: list[Path]) -> str:
    """Read the deployment target from the actual bundled Mach-O files."""
    versions = []
    for start in range(0, len(paths), 50):
        output = subprocess.run(
            ["otool", "-l", *(str(path) for path in paths[start:start + 50])],
            text=True, capture_output=True, check=True,
        ).stdout
        versions.extend(re.findall(r"\bminos\s+(\d+(?:\.\d+)+)", output))
        versions.extend(re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+)+)", output))
    if not versions:
        raise RuntimeError("Could not determine minimum macOS version")
    return max(versions, key=lambda version: tuple(int(part) for part in version.split(".")))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_assets() -> dict:
    import imageio_ffmpeg
    from PIL import Image, ImageOps

    bin_dir = ASSETS / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    source = Path(imageio_ffmpeg.get_ffmpeg_exe())
    ffmpeg = bin_dir / "ffmpeg"
    shutil.copy2(source, ffmpeg)
    ffmpeg.chmod(0o755)

    iconset = ASSETS / "VideoScraper.iconset"
    iconset.mkdir(exist_ok=True)
    with Image.open(PROJECT / "Logo.png") as source_icon:
        logo = source_icon.convert("RGBA")
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                image = Image.new("RGBA", (pixels, pixels))
                resized = ImageOps.contain(logo, (pixels, pixels), Image.Resampling.LANCZOS)
                image.alpha_composite(resized, ((pixels - resized.width) // 2, (pixels - resized.height) // 2))
                suffix = "@2x" if scale == 2 else ""
                image.save(iconset / f"icon_{size}x{size}{suffix}.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "Logo.icns")], check=True)

    third_party = ASSETS / "third-party"
    third_party.mkdir(exist_ok=True)
    versions = {}
    for name in RUNTIME_DISTRIBUTIONS:
        dist = metadata.distribution(name)
        versions[name] = dist.version
        for file in dist.files or ():
            if not any(word in str(file).lower() for word in ("license", "copying", "notice")):
                continue
            license_source = Path(dist.locate_file(file))
            if license_source.is_file():
                target = third_party / name / Path(str(file)).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(license_source, target)
    ffmpeg_license = subprocess.run([str(ffmpeg), "-L"], capture_output=True, text=True, check=True)
    (third_party / "FFmpeg-license.txt").write_text(ffmpeg_license.stdout, encoding="utf-8")
    # This FFmpeg build permits GPL v3; include the complete text already supplied by PyQt6.
    shutil.copy2(third_party / "PyQt6" / "LICENSE", third_party / "FFmpeg-COPYING.GPLv3")
    (third_party / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    return versions


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("Build this app on macOS.")
    if platform.machine() not in {"arm64", "x86_64"}:
        raise SystemExit(f"Unsupported build architecture: {platform.machine()}")
    versions = prepare_assets()
    build_dir = PROJECT / "build" / "macos"
    build_dir.mkdir(parents=True, exist_ok=True)
    from PyInstaller.depend.bindepend import get_python_library_path
    from PyQt6.QtCore import QLibraryInfo

    minimum_os = minimum_macos([
        Path(get_python_library_path()), ASSETS / "bin" / "ffmpeg",
        Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath)) / "QtCore.framework" / "Versions" / "A" / "QtCore",
    ])
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(PROJECT / "build" / "pyinstaller-cache")
    env["VIDEO_SCRAPER_MINIMUM_MACOS"] = minimum_os
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(PROJECT / "dist"),
        "--workpath", str(build_dir / "pyinstaller"),
        str(PROJECT / "app_bundle" / "VideoScraper.spec"),
    ]
    print("Building Video Scraper.app...", flush=True)
    with (build_dir / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(f"Build failed. See {build_dir / 'build.log'}", file=sys.stderr)
        return result.returncode

    app = PROJECT / "dist" / "Video Scraper.app"
    macho_files = []
    macho_magic = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as binary:
                if binary.read(4) in macho_magic:
                    macho_files.append(path)
    actual_minimum_os = minimum_macos(macho_files)
    if actual_minimum_os != minimum_os:
        raise RuntimeError(f"Bundled dependency requires macOS {actual_minimum_os}, expected {minimum_os}")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    archive = PROJECT / "dist" / f"VideoScraper-macOS-{platform.machine()}.zip"
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], check=True)
    archive_sha256 = sha256_file(archive)
    source_paths = [PROJECT / "main.py", PROJECT / "requirements.txt", PROJECT / "requirements-build.txt", PROJECT / "Logo.png"]
    for directory in ("src", "scripts", "app_bundle", "assets"):
        source_paths.extend(path for path in (PROJECT / directory).rglob("*") if path.suffix in {".py", ".spec", ".svg"})
    report = {
        "app": str(app), "archive": str(archive),
        "architecture": platform.machine(), "macos_build_host": platform.mac_ver()[0],
        "minimum_macos": actual_minimum_os, "python": platform.python_version(),
        "pyinstaller": metadata.version("pyinstaller"), "dependencies": versions,
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT, text=True)),
        "source_sha256": {str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
        "archive_sha256": archive_sha256,
        "signing": "ad-hoc; not notarized",
    }
    (PROJECT / "dist" / "build-info.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"App: {app}\nArchive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

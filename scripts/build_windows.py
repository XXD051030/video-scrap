"""Build a Windows x64 application folder and its complete distribution ZIP."""

from __future__ import annotations

from datetime import datetime
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig


PROJECT = Path(__file__).resolve().parents[1]
ASSETS = PROJECT / "build" / "windows-app-assets"
RUNTIME_DISTRIBUTIONS = (
    "PyQt6", "PyQt6-Qt6", "PyQt6-sip", "PyQt6-WebEngine", "PyQt6-WebEngine-Qt6",
    "yt-dlp", "requests", "urllib3", "certifi", "charset-normalizer", "idna",
    "beautifulsoup4", "soupsieve", "lxml", "Pillow", "pycryptodome", "imageio-ffmpeg",
)
BUILD_DISTRIBUTIONS = ("pyinstaller", "pyinstaller-hooks-contrib")
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_build_host() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build the Windows version on Windows, using 64-bit Python.")
    if sysconfig.get_platform() != "win-amd64" or struct.calcsize("P") != 8:
        raise SystemExit("This build supports Windows x64 and requires 64-bit Python.")
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or later is required; Python 3.12 x64 is recommended.")
    if metadata.version("pyinstaller") != "6.22.3":
        raise SystemExit("Install requirements-build.txt to use PyInstaller 6.22.3.")
    if metadata.version("imageio-ffmpeg") != "0.6.0":
        raise SystemExit("Install requirements-build.txt to use imageio-ffmpeg 0.6.0.")


def check_x64_executable(path: Path) -> None:
    """Reject an environment override pointing at a foreign FFmpeg binary."""
    with path.open("rb") as binary:
        if binary.read(2) != b"MZ":
            raise RuntimeError(f"FFmpeg is not a Windows executable: {path}")
        binary.seek(0x3C)
        offset_bytes = binary.read(4)
        if len(offset_bytes) != 4:
            raise RuntimeError(f"FFmpeg has an invalid PE header: {path}")
        binary.seek(int.from_bytes(offset_bytes, "little"))
        header = binary.read(6)
        if header[:4] != b"PE\0\0" or header[4:] != b"\x64\x86":
            raise RuntimeError(f"FFmpeg must be a Windows x64 executable: {path}")


def prepare_icon(logo: Path, destination: Path) -> None:
    from PIL import Image, ImageOps

    if not logo.is_file():
        raise RuntimeError(f"Application icon is missing: {logo}")
    # Keep the complete image and its transparency, including non-square logos.
    with Image.open(logo) as source:
        image = ImageOps.contain(source.convert("RGBA"), (256, 256), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((256 - image.width) // 2, (256 - image.height) // 2))
    canvas.save(destination, format="ICO", sizes=[(size, size) for size in ICO_SIZES])


def prepare_assets() -> dict[str, str]:
    import imageio_ffmpeg

    ASSETS.mkdir(parents=True, exist_ok=True)
    prepare_icon(PROJECT / "Logo.png", ASSETS / "VideoScraper.ico")

    bin_dir = ASSETS / "bin"
    bin_dir.mkdir(exist_ok=True)
    source_ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    check_x64_executable(source_ffmpeg)
    ffmpeg = bin_dir / "ffmpeg.exe"
    shutil.copy2(source_ffmpeg, ffmpeg)

    third_party = ASSETS / "third-party"
    if third_party.exists():
        shutil.rmtree(third_party)
    third_party.mkdir()
    versions = {}
    for name in (*RUNTIME_DISTRIBUTIONS, *BUILD_DISTRIBUTIONS):
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
    license_result = subprocess.run([str(ffmpeg), "-L"], capture_output=True, text=True, check=True)
    (third_party / "FFmpeg-license.txt").write_text(
        license_result.stdout + license_result.stderr, encoding="utf-8",
    )
    # Include the full GPL text for the FFmpeg build supplied by imageio-ffmpeg.
    shutil.copy2(third_party / "PyQt6" / "LICENSE", third_party / "FFmpeg-COPYING.GPLv3")
    (third_party / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    return versions


def git_state() -> dict:
    """A source ZIP can be built without Git or a .git directory."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=PROJECT, text=True, stderr=subprocess.DEVNULL,
        ))
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": None, "working_tree_dirty": None}
    return {"git_commit": commit, "working_tree_dirty": dirty}


def main() -> int:
    check_build_host()
    versions = prepare_assets()
    build_dir = PROJECT / "build" / "windows"
    build_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(PROJECT / "build" / "pyinstaller-cache-windows")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(PROJECT / "dist"),
        "--workpath", str(build_dir / "pyinstaller"),
        str(PROJECT / "app_bundle" / "VideoScraper-Windows.spec"),
    ]
    log_path = build_dir / "build.log"
    print("Building Video Scraper for Windows x64...", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=PROJECT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(f"Build failed. See {log_path}", file=sys.stderr)
        return result.returncode

    app = PROJECT / "dist" / "Video Scraper"
    if not (app / "Video Scraper.exe").is_file():
        raise RuntimeError(f"Expected application executable is missing: {app}")
    # Every dependency in the onedir bundle must travel with the executable.
    archive = Path(shutil.make_archive(
        str(PROJECT / "dist" / "VideoScraper-Windows-x64"), "zip",
        root_dir=PROJECT / "dist", base_dir=app.name,
    ))
    source_paths = [PROJECT / name for name in ("main.py", "Logo.png", "requirements.txt", "requirements-build.txt")]
    for directory in ("src", "scripts", "app_bundle", "assets"):
        source_paths.extend(path for path in (PROJECT / directory).rglob("*") if path.suffix in {".py", ".spec", ".svg"})
    report = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": str(app), "archive": str(archive),
        "architecture": "x64", "windows_build_host": platform.platform(),
        "python": platform.python_version(), "pyinstaller": metadata.version("pyinstaller"),
        "dependencies": versions, **git_state(),
        "logo_sha256": sha256_file(PROJECT / "Logo.png"),
        "source_sha256": {str(path.relative_to(PROJECT)): sha256_file(path) for path in source_paths},
        "archive_sha256": sha256_file(archive),
        "signing": "not configured; unsigned executable",
        "validation": "build completed; application playback and downloads require Windows smoke testing",
    }
    (PROJECT / "dist" / "build-info-Windows.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Application folder: {app}\nArchive: {archive}\nBuild log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

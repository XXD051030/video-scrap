"""Expose bundled tools before application modules are imported."""

import os
from pathlib import Path
import sys


if getattr(sys, "frozen", False):
    tool_dir = Path(sys._MEIPASS) / "bin"
    tool_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    if (tool_dir / tool_name).is_file():
        os.environ["PATH"] = str(tool_dir) + os.pathsep + os.environ.get("PATH", "")

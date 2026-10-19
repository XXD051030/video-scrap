"""Produce PyInstaller VERSIONINFO from the application's version source."""
from __future__ import annotations
import ast
from pathlib import Path


def app_version(project: Path) -> str:
    tree = ast.parse((project / "src" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and len(version.split(".")) == 3 and all(p.isdigit() and 0 <= int(p) <= 65535 for p in version.split(".")):
                return version
    raise ValueError("Application version must contain three numeric components")


def write_version_resource(project: Path, destination: Path) -> Path:
    version = app_version(project)
    parts = tuple(int(p) for p in version.split(".")) + (0,)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts!r}, prodvers={parts!r},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Video Scraper'),
    StringStruct('FileDescription', 'Video Scraper'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'VideoScraper'),
    StringStruct('OriginalFilename', 'Video Scraper.exe'),
    StringStruct('ProductName', 'Video Scraper'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
''', encoding="utf-8")
    return destination

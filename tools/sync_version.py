r"""Regenerate the version-bearing build inputs from version.py (the single source).

    .venv\Scripts\python.exe tools\sync_version.py            write installer\version_info.txt + installer\version.iss
    .venv\Scripts\python.exe tools\sync_version.py --print    additionally print the version (build.bat reads it)
    .venv\Scripts\python.exe tools\sync_version.py --check    exit 1 if a generated file is stale (writes nothing)

build.bat runs this first, so bumping version.py is the only manual step of a release.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from version import __version__  # noqa: E402

VERSION_INFO = """# PyInstaller version resource for HermesController.exe
# GENERATED from version.py by tools/sync_version.py - do not edit by hand.
VSVersionInfo(
  ffi=FixedFileInfo(filevers={quad}, prodvers={quad}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("CompanyName", "warrenjbg"),
      StringStruct("FileDescription", "Hermes Controller - start/stop the local Strata endpoint"),
      StringStruct("FileVersion", "{version}"),
      StringStruct("InternalName", "HermesController"),
      StringStruct("OriginalFilename", "HermesController.exe"),
      StringStruct("ProductName", "Hermes Controller"),
      StringStruct("ProductVersion", "{version}")])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])])
  ]
)
"""

VERSION_ISS = """; GENERATED from version.py by tools/sync_version.py - do not edit by hand.
#define AppVersion "{version}"
"""


def outputs() -> dict[Path, str]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        raise SystemExit(f"version.py: {__version__!r} is not MAJOR.MINOR.PATCH")
    quad = "(" + ", ".join(__version__.split(".") + ["0"]) + ")"
    return {ROOT / "installer" / "version_info.txt": VERSION_INFO.format(quad=quad, version=__version__),
            ROOT / "installer" / "version.iss": VERSION_ISS.format(version=__version__)}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for path, text in outputs().items():
        try:
            current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError:
            current = None
        if current == text:
            continue
        stale.append(path.name)
        if not check:
            path.write_text(text, encoding="utf-8", newline="\r\n")
    if check and stale:
        print("stale: " + ", ".join(stale), file=sys.stderr)
        return 1
    if "--print" in argv:
        print(__version__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

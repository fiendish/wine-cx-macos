#!/usr/bin/env python3
from __future__ import annotations

import json
import plistlib
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config/versions.json").read_text())
APP = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "dist/Wine CX.app"
WINE = APP / "Contents/Resources/wine"
LEGAL = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "dist/LICENSES"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/System/Library/",
    "/Library/Apple/System/Library/",
)
EXTERNAL_RPATH = "/Library/Frameworks/GStreamer.framework/Libraries"
MEDIA_MODULES = {"winegstreamer.so", "winedmo.so"}
EXTERNAL_GSTREAMER_LIBRARIES = {
    "libavcodec.61.dylib",
    "libavformat.61.dylib",
    "libavutil.59.dylib",
    "libglib-2.0.0.dylib",
    "libgobject-2.0.0.dylib",
    "libgstaudio-1.0.0.dylib",
    "libgstbase-1.0.0.dylib",
    "libgstreamer-1.0.0.dylib",
    "libgsttag-1.0.0.dylib",
    "libgstvideo-1.0.0.dylib",
    "libintl.8.dylib",
}


def output(*args: str | Path) -> str:
    return subprocess.check_output([str(arg) for arg in args], text=True)


def is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO_MAGICS


def load_info(path: Path) -> tuple[list[str], list[str], list[str]]:
    commands = output("/usr/bin/otool", "-l", path)
    current = None
    dependencies: list[str] = []
    rpaths: list[str] = []
    sections: list[str] = []
    for raw in commands.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            current = line.split()[1]
        elif line.startswith("name ") and current in {
            "LC_LOAD_DYLIB",
            "LC_LOAD_WEAK_DYLIB",
            "LC_REEXPORT_DYLIB",
        }:
            dependencies.append(line[5:].rsplit(" (offset ", 1)[0])
        elif line.startswith("path ") and current == "LC_RPATH":
            rpaths.append(line[5:].rsplit(" (offset ", 1)[0])
        elif line.startswith("sectname "):
            sections.append(line.split()[1])
    return dependencies, rpaths, sections


def pe_debug_state(path: Path) -> tuple[list[str], int, int] | None:
    data = path.read_bytes()
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    symbol_table = struct.unpack_from("<I", data, coff + 8)[0]
    symbol_count = struct.unpack_from("<I", data, coff + 12)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        directory_count_offset = optional + 92
        directory_offset = optional + 96
    elif magic == 0x20B:
        directory_count_offset = optional + 108
        directory_offset = optional + 112
    else:
        raise RuntimeError(f"Unsupported PE optional header in {path}")
    directory_count = struct.unpack_from("<I", data, directory_count_offset)[0]
    debug_directory_size = 0
    if directory_count > 6:
        debug_directory_size = struct.unpack_from("<I", data, directory_offset + 6 * 8 + 4)[0]
    section_table = optional + optional_size
    debug_sections: list[str] = []
    for index in range(section_count):
        section = section_table + index * 40
        name = data[section : section + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        if name.startswith((".debug", ".zdebug")) or name in {".stab", ".stabstr", ".buildid"}:
            debug_sections.append(name)
    return debug_sections, debug_directory_size, symbol_table or symbol_count


if not APP.is_dir():
    raise RuntimeError(f"App is absent: {APP}")
app_root = APP.resolve()

with (APP / "Contents/Info.plist").open("rb") as stream:
    info = plistlib.load(stream)
if info.get("CFBundleName") != "Wine CX":
    raise RuntimeError("The app name is not Wine CX")
if info.get("CFBundleShortVersionString") != CONFIG["crossover"]["release"]:
    raise RuntimeError("The app version does not match config/versions.json")
if info.get("CFBundleIconFile") != "Wine-CX.icns":
    raise RuntimeError("The app icon is not configured")
if info.get("LSUIElement") is not True:
    raise RuntimeError("The launcher must not appear in the Dock")
if not (APP / "Contents/Resources/Wine-CX.icns").is_file():
    raise RuntimeError("The app icon is absent")
document_types = info.get("CFBundleDocumentTypes", [])
if len(document_types) != 1 or document_types[0].get("CFBundleTypeIconFile") != "Executable.icns":
    raise RuntimeError("The executable file icon is not configured")
if not (APP / "Contents/Resources/Executable.icns").is_file():
    raise RuntimeError("The executable file icon is absent")

for name in ("build-manifest.json", "import-manifest.json", "strip-manifest.json"):
    if (APP / "Contents/Resources" / name).exists():
        raise RuntimeError(f"Build-only metadata remains in the app: {name}")

for relative in (
    "Wine-CX-MIT.txt",
    "MacPorts-BSD-3-Clause.txt",
    "Wine-Mono-COPYING.txt",
    "MPL-2.0.txt",
    "components.json",
    "source.json",
    "wine/COPYING.LIB",
    "wine/LICENSE",
    "source/ports/emulators/wine-cx/Portfile",
    "source/ports/emulators/wine-cx/files/0001-win32u-Enable-host-Vulkan-portability-enumeration.diff",
):
    if not (LEGAL / relative).is_file():
        raise RuntimeError(f"Required legal file is absent: {relative}")

components = json.loads((LEGAL / "components.json").read_text())
component_names = {component["name"] for component in components}
for expected in ("wine-cx", "mingw-w64-wine-mono-10.4.1", "mingw-w64-wine-gecko-2.47.4"):
    if expected not in component_names:
        raise RuntimeError(f"Required component metadata is absent: {expected}")
for component in components:
    if not component.get("installed") or not component.get("license") or not component.get("homepage"):
        raise RuntimeError(f"Incomplete component metadata: {component}")

source = json.loads((LEGAL / "source.json").read_text())
if source.get("wine_source") != CONFIG["crossover"]:
    raise RuntimeError("The Wine source record does not match config/versions.json")
if source.get("macports_recipe") != CONFIG["macports"]:
    raise RuntimeError("The MacPorts source record does not match config/versions.json")

for path in APP.rglob("*"):
    lower_parts = {part.casefold() for part in path.relative_to(APP).parts}
    if "gstreamer.framework" in lower_parts or any(
        part.startswith("python") and part.endswith(".framework") for part in lower_parts
    ):
        raise RuntimeError(f"Excluded framework is bundled: {path}")
    if path.name.casefold().startswith("libpython"):
        raise RuntimeError(f"Excluded Python library is bundled: {path}")
    if path.name in EXTERNAL_GSTREAMER_LIBRARIES and path.name.startswith(
        ("libav", "libglib", "libgobject", "libgst", "libgstreamer")
    ):
        raise RuntimeError(f"External GStreamer library is bundled: {path}")
    if path.name.endswith(".dSYM") or path.name.endswith(".debug"):
        raise RuntimeError(f"Debug data is bundled: {path}")
    if path.is_symlink():
        target = path.resolve(strict=True)
        if not target.is_relative_to(app_root):
            raise RuntimeError(f"Symlink leaves the app: {path} -> {target}")

macho_count = 0
external_libraries_seen: set[str] = set()
for path in APP.rglob("*"):
    if not is_macho(path):
        continue
    macho_count += 1
    dependencies, rpaths, sections = load_info(path)
    if any(section.startswith(("__debug", "__zdebug")) for section in sections):
        raise RuntimeError(f"Mach-O debug section remains: {path}")
    if any("/opt/local" in value for value in dependencies + rpaths):
        raise RuntimeError(f"MacPorts path remains in native load commands: {path}")
    if path.name in MEDIA_MODULES:
        expected_rpaths = {"@loader_path/", EXTERNAL_RPATH}
        if set(rpaths) != expected_rpaths:
            raise RuntimeError(f"Incorrect GStreamer run paths: {path}: {rpaths}")
    elif rpaths:
        raise RuntimeError(f"Unexpected native run path: {path}: {rpaths}")
    for dependency in dependencies:
        if dependency.startswith(SYSTEM_PREFIXES):
            continue
        if dependency.startswith("@loader_path/"):
            candidate = (path.parent / dependency[len("@loader_path/") :]).resolve()
            if not candidate.exists() or not candidate.is_relative_to(app_root):
                raise RuntimeError(f"Broken bundled dependency: {path}: {dependency}")
            continue
        if dependency.startswith("@rpath/"):
            library = dependency[len("@rpath/") :]
            if path.name not in MEDIA_MODULES or library not in EXTERNAL_GSTREAMER_LIBRARIES:
                raise RuntimeError(f"Unexpected external dependency: {path}: {dependency}")
            external_libraries_seen.add(library)
            continue
        raise RuntimeError(f"Unsupported native dependency: {path}: {dependency}")

if external_libraries_seen != EXTERNAL_GSTREAMER_LIBRARIES:
    raise RuntimeError(f"Incorrect external GStreamer dependencies: {external_libraries_seen}")

pe_count = 0
for path in APP.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    state = pe_debug_state(path)
    if state is None:
        continue
    pe_count += 1
    debug_sections, debug_directory_size, symbol_state = state
    if debug_sections or debug_directory_size or symbol_state:
        raise RuntimeError(f"PE debug data remains: {path}: {state}")

if macho_count == 0 or pe_count == 0:
    raise RuntimeError(f"Unexpected native file counts: Mach-O={macho_count}, PE={pe_count}")

version = output(WINE / "bin/wine-real", "--version").strip()
if CONFIG["crossover"]["wine_version"] not in version:
    raise RuntimeError(f"Unexpected Wine version: {version}")
subprocess.run(
    ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(APP)],
    check=True,
)
size = sum(path.stat().st_size for path in APP.rglob("*") if path.is_file() and not path.is_symlink())
print(
    f"Validated {APP}: version={version!r}, Mach-O={macho_count}, PE={pe_count}, "
    f"bytes={size}"
)

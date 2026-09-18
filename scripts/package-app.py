#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tarfile
from collections import deque
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config/versions.json").read_text())
PREFIX = Path(os.environ.get("MACPORTS_PREFIX", "/opt/local")).resolve()
APP = ROOT / "dist/Wine CX.app"
WINE = APP / "Contents/Resources/wine"
GSTREAMER_BUILD = Path("/Library/Frameworks/GStreamer.framework").resolve()
GSTREAMER_RUNTIME = Path("/Library/Frameworks/GStreamer.framework/Libraries")
LEGAL = ROOT / "dist/LICENSES"
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
MEDIA_MODULES = {"winegstreamer.so", "winedmo.so"}
DYNAMIC_LIBRARY_PATTERN = re.compile(rb"(?<![A-Za-z0-9_.+-])([A-Za-z0-9_.+-]+\.dylib)\x00")


def run(*args: str | Path) -> str:
    return subprocess.check_output([str(arg) for arg in args], text=True)


def is_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACHO_MAGICS


def load_info(path: Path) -> tuple[list[str], list[str]]:
    commands = run("/usr/bin/otool", "-l", path)
    current = None
    dependencies: list[str] = []
    rpaths: list[str] = []
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
    return dependencies, rpaths


def expand_loader_path(path: str, loader: Path) -> Path:
    if path == "@loader_path":
        return loader
    if path.startswith("@loader_path/"):
        return loader / path[len("@loader_path/") :]
    return Path(path)


def resolve_dependency(dependency: str, source: Path, rpaths: list[str]) -> Path | None:
    if dependency.startswith(SYSTEM_PREFIXES):
        return None
    if dependency.startswith("@loader_path"):
        candidate = expand_loader_path(dependency, source.parent)
        if candidate.exists():
            return candidate.resolve()
    if dependency.startswith("@rpath/"):
        suffix = dependency[len("@rpath/") :]
        for rpath in rpaths:
            base = expand_loader_path(rpath, source.parent)
            candidate = base / suffix
            if candidate.exists():
                return candidate.resolve()
        for base in (
            source.parent,
            GSTREAMER_BUILD / "Libraries",
            GSTREAMER_BUILD / "Versions/1.0/Libraries",
            PREFIX / "lib",
            PREFIX / "lib/wine/x86_64-unix",
        ):
            candidate = base / suffix
            if candidate.exists():
                return candidate.resolve()
    candidate = Path(dependency)
    if candidate.is_absolute() and candidate.exists():
        resolved = candidate.resolve()
        if str(resolved).startswith(SYSTEM_PREFIXES):
            return None
        return resolved
    raise RuntimeError(f"Unresolved native dependency: {source}: {dependency}")


def copy_plain(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(source.stat().st_mode & 0o777)


def destination_for(source: Path) -> Path:
    source = source.resolve()
    if source == (PREFIX / "bin/wine").resolve():
        return WINE / "bin/wine-real"
    if source == (PREFIX / "bin/wineserver").resolve():
        return WINE / "bin/wineserver"
    wine_library = PREFIX / "lib/wine"
    if source.is_relative_to(wine_library):
        return WINE / "lib/wine" / source.relative_to(wine_library)
    if source.is_relative_to(PREFIX):
        return WINE / "lib" / source.name
    raise RuntimeError(f"Native dependency is outside the MacPorts prefix: {source}")


def digest(path: Path) -> bytes:
    return hashlib.sha256(path.read_bytes()).digest()


def dynamic_libraries(source: Path) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for match in DYNAMIC_LIBRARY_PATTERN.finditer(source.read_bytes()):
        name = match.group(1).decode("ascii")
        candidate = PREFIX / "lib" / name
        if candidate.exists():
            discovered[name] = candidate.resolve()
    return discovered


def port_owner(path: Path) -> str:
    result = run("port", "provides", path).strip()
    marker = " is provided by: "
    if marker not in result:
        raise RuntimeError(f"MacPorts did not identify the owner of {path}: {result!r}")
    return result.rsplit(marker, 1)[1].split("@", 1)[0].strip()


def port_metadata(name: str) -> dict[str, object]:
    installed = run("port", "-q", "installed", name).strip()
    if "(active)" not in installed:
        raise RuntimeError(f"MacPorts package is not active: {name}: {installed!r}")
    details = run("port", "info", name)
    license_match = re.search(r"^License:\s*(.+)$", details, re.MULTILINE)
    homepage_match = re.search(r"^Homepage:\s*(.+)$", details, re.MULTILINE)
    if not license_match or not homepage_match:
        raise RuntimeError(f"MacPorts metadata is incomplete for {name}")

    notice_files: list[str] = []
    destination = LEGAL / "macports" / name
    for raw in run("port", "-q", "contents", name).splitlines():
        source = Path(raw.strip())
        if not source.is_file():
            continue
        if not re.match(
            r"^(authors|copying|copyright|license|licence|notice)(\..*)?$",
            source.name,
            re.IGNORECASE,
        ):
            continue
        relative = source.relative_to(PREFIX) if source.is_relative_to(PREFIX) else Path(source.name)
        target = destination / relative
        copy_plain(source, target)
        notice_files.append(str(target.relative_to(LEGAL)))

    return {
        "name": name,
        "installed": installed,
        "license": license_match.group(1).strip(),
        "homepage": homepage_match.group(1).strip(),
        "notice_files": sorted(set(notice_files)),
    }


def copy_legal_files(port_sources: set[Path]) -> None:
    if LEGAL.exists():
        shutil.rmtree(LEGAL)
    LEGAL.mkdir(parents=True)
    copy_plain(ROOT / "LICENSE", LEGAL / "Wine-CX-MIT.txt")
    copy_plain(ROOT / "legal/MacPorts-BSD-3-Clause.txt", LEGAL / "MacPorts-BSD-3-Clause.txt")
    copy_plain(ROOT / "legal/Wine-Mono-COPYING.txt", LEGAL / "Wine-Mono-COPYING.txt")
    copy_plain(ROOT / "legal/MPL-2.0.txt", LEGAL / "MPL-2.0.txt")
    copy_plain(
        ROOT / "ports/emulators/wine-cx/Portfile",
        LEGAL / "source/ports/emulators/wine-cx/Portfile",
    )
    copy_plain(
        ROOT / "ports/emulators/wine-cx/files/0001-win32u-Enable-host-Vulkan-portability-enumeration.diff",
        LEGAL / "source/ports/emulators/wine-cx/files/0001-win32u-Enable-host-Vulkan-portability-enumeration.diff",
    )

    source_archive = ROOT / "build/crossover-sources.tar.gz"
    with tarfile.open(source_archive, "r:gz") as archive:
        copied = 0
        for member in archive.getmembers():
            path = Path(member.name)
            if not member.isfile() or not path.is_relative_to("sources/wine"):
                continue
            if not re.match(
                r"^(authors|copying|copyright|license|licence|notice)(\..*)?$",
                path.name,
                re.IGNORECASE,
            ):
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Could not read source notice: {member.name}")
            target = LEGAL / "wine" / path.relative_to("sources/wine")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stream.read())
            copied += 1
    if copied == 0:
        raise RuntimeError("No Wine license files were copied from the source archive")

    owners = {port_owner(path) for path in port_sources}
    owners.add(port_owner(next((PREFIX / "share/wine/mono").rglob("*.dll"))))
    owners.add(port_owner(next((PREFIX / "share/wine/gecko").rglob("xul.dll"))))
    components = [port_metadata(name) for name in sorted(owners)]
    (LEGAL / "components.json").write_text(json.dumps(components, indent=2) + "\n")
    (LEGAL / "source.json").write_text(
        json.dumps(
            {
                "wine_source": CONFIG["crossover"],
                "wine_source_release_asset": (
                    f"crossover-sources-{CONFIG['crossover']['release']}.tar.gz"
                ),
                "macports_recipe": CONFIG["macports"],
                "build_repository": "https://github.com/fiendish/wine-cx-macos",
            },
            indent=2,
        )
        + "\n"
    )


for required in (
    ROOT / "launcher/main.m",
    ROOT / "launcher/wine-terminal-help",
    ROOT / "build/crossover-sources.tar.gz",
    PREFIX / "bin/wine",
    PREFIX / "bin/wineserver",
    PREFIX / "lib/wine",
    PREFIX / "share/wine",
):
    if not required.exists():
        raise RuntimeError(f"Required build input is absent: {required}")

if APP.exists():
    shutil.rmtree(APP)
(APP / "Contents/MacOS").mkdir(parents=True)
(APP / "Contents/Resources").mkdir(parents=True)
subprocess.run(
    [
        "/usr/bin/xcrun",
        "clang",
        "-mmacosx-version-min=14.0",
        "-fobjc-arc",
        "-framework",
        "Cocoa",
        str(ROOT / "launcher/main.m"),
        "-o",
        str(APP / "Contents/MacOS/Wine CX"),
    ],
    check=True,
)
copy_plain(ROOT / "launcher/wine-terminal-help", APP / "Contents/Resources/wine-terminal-help")
(APP / "Contents/Resources/wine-terminal-help").chmod(0o755)
copy_plain(ROOT / "assets/Wine-CX.icns", APP / "Contents/Resources/Wine-CX.icns")
copy_plain(ROOT / "assets/Executable.icns", APP / "Contents/Resources/Executable.icns")
(APP / "Contents/PkgInfo").write_text("APPLWINE")

info = {
        "CFBundleName": "Wine CX",
        "CFBundleDisplayName": "Wine CX",
        "CFBundleIdentifier": "com.github.fiendish.wine-cx",
        "CFBundleExecutable": "Wine CX",
        "CFBundleIconFile": "Wine-CX.icns",
        "LSUIElement": True,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": f"{CONFIG['crossover']['release']}.{CONFIG['package_revision']}",
        "CFBundleShortVersionString": CONFIG["crossover"]["release"],
        "LSMinimumSystemVersion": "14.0",
        "NSAppleEventsUsageDescription": (
            "Wine CX controls Terminal to open a shell with Wine commands in PATH."
        ),
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeExtensions": ["exe"],
                "CFBundleTypeName": "Windows executable",
                "CFBundleTypeRole": "Viewer",
                "CFBundleTypeIconFile": "Executable.icns",
            }
        ],
}
with (APP / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump(info, stream)

shutil.copytree(PREFIX / "lib/wine", WINE / "lib/wine", symlinks=True)
shutil.copytree(PREFIX / "share/wine", WINE / "share/wine", symlinks=True)
copy_plain(PREFIX / "bin/wine", WINE / "bin/wine-real")
copy_plain(PREFIX / "bin/wineserver", WINE / "bin/wineserver")

mono = WINE / f"share/wine/mono/wine-mono-{CONFIG['wine_mono']}"
gecko = WINE / "share/wine/gecko"
if not mono.is_dir():
    raise RuntimeError(f"Required Wine Mono directory is absent: {mono}")
if not gecko.is_dir() or not any(gecko.iterdir()):
    raise RuntimeError(f"Required Wine Gecko directory is absent: {gecko}")

wrapper = """#!/bin/bash
set -euo pipefail
wine_cx_bindir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
export DYLD_FALLBACK_LIBRARY_PATH="$wine_cx_bindir/../lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
if [[ -z "${VK_DRIVER_FILES:-}${VK_ICD_FILENAMES:-}" ]]; then
    export VK_DRIVER_FILES="$wine_cx_bindir/../lib/wine/x86_64-unix/vulkan/icd.d/MoltenVK_icd.json"
fi
exec -a "$0" "$wine_cx_bindir/wine-real" "$@"
"""
(WINE / "bin/wine").write_text(wrapper)
(WINE / "bin/wine").chmod(0o755)
for command in (
    "msidb",
    "msiexec",
    "notepad",
    "regedit",
    "regsvr32",
    "wineboot",
    "winecfg",
    "wineconsole",
    "winedbg",
    "winefile",
    "winemine",
    "winepath",
):
    link = WINE / "bin" / command
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to("wine")

initial_sources = [(PREFIX / "bin/wine").resolve(), (PREFIX / "bin/wineserver").resolve()]
initial_sources.extend(path.resolve() for path in (PREFIX / "lib/wine").rglob("*") if is_macho(path))
moltenvk_candidates = [
    PREFIX / "lib/libMoltenVK.dylib",
    PREFIX / "libexec/MoltenVK/libMoltenVK.dylib",
]
moltenvk = next((path.resolve() for path in moltenvk_candidates if path.exists()), None)
if moltenvk is None:
    raise RuntimeError("MacPorts did not install libMoltenVK.dylib")
initial_sources.append(moltenvk)

queue = deque(initial_sources)
source_to_destination: dict[Path, Path] = {}
plans: dict[Path, tuple[list[tuple[str, Path | None]], list[str]]] = {}
dynamic_aliases: dict[str, Path] = {}
while queue:
    source = queue.popleft().resolve()
    if source in source_to_destination:
        continue
    destination = destination_for(source)
    existing = next((old for old, target in source_to_destination.items() if target == destination), None)
    if existing is not None:
        if digest(existing) != digest(source):
            raise RuntimeError(f"Native library name collision: {existing} and {source}")
        source_to_destination[source] = destination
        continue
    source_to_destination[source] = destination
    if not destination.exists():
        copy_plain(source, destination)
    if destination.name not in MEDIA_MODULES:
        for alias, dynamic_source in dynamic_libraries(source).items():
            if dynamic_source.name.casefold().startswith("libpython"):
                raise RuntimeError(f"Unexpected Python dependency: {source}: {dynamic_source}")
            dynamic_aliases[alias] = dynamic_source
            queue.append(dynamic_source)
    dependencies, rpaths = load_info(source)
    resolved_dependencies: list[tuple[str, Path | None]] = []
    for dependency in dependencies:
        resolved = resolve_dependency(dependency, source, rpaths)
        if (
            resolved is not None
            and destination.name in MEDIA_MODULES
            and not resolved.is_relative_to(PREFIX / "lib/wine")
        ):
            if destination.name not in MEDIA_MODULES:
                raise RuntimeError(
                    f"Unexpected external GStreamer dependency: {destination}: {dependency}"
                )
            resolved_dependencies.append((dependency, None))
        else:
            resolved_dependencies.append((dependency, resolved))
            if resolved is not None:
                if not resolved.is_relative_to(PREFIX):
                    raise RuntimeError(f"Unsupported native dependency: {source}: {resolved}")
                if resolved.name.casefold().startswith("libpython"):
                    raise RuntimeError(f"Unexpected Python dependency: {source}: {resolved}")
                queue.append(resolved)
    plans[source] = (resolved_dependencies, rpaths)

for source, destination in source_to_destination.items():
    dependency_plan, rpaths = plans.get(source, ([], []))
    arguments: list[str] = []
    for dependency, resolved in dependency_plan:
        if resolved is None:
            if dependency.startswith(SYSTEM_PREFIXES):
                continue
            replacement = "@rpath/" + Path(dependency).name
        else:
            target = source_to_destination.get(resolved)
            if target is None:
                raise RuntimeError(f"No bundled destination for {source}: {dependency} -> {resolved}")
            replacement = "@loader_path/" + os.path.relpath(target, destination.parent)
        if replacement != dependency:
            arguments.extend(("-change", dependency, replacement))
    desired_rpaths: list[str] = []
    if destination.name in MEDIA_MODULES:
        desired_rpaths = ["@loader_path/", str(GSTREAMER_RUNTIME)]
    for old, new in zip(rpaths, desired_rpaths):
        if old != new:
            arguments.extend(("-rpath", old, new))
    for old in rpaths[len(desired_rpaths) :]:
        arguments.extend(("-delete_rpath", old))
    for new in desired_rpaths[len(rpaths) :]:
        arguments.extend(("-add_rpath", new))
    if arguments:
        subprocess.run(["/usr/bin/install_name_tool", *arguments, str(destination)], check=True)

for alias, source in dynamic_aliases.items():
    target = source_to_destination.get(source)
    if target is None:
        raise RuntimeError(f"No bundled destination for dynamic library: {alias} -> {source}")
    link = WINE / "lib" / alias
    if link == target:
        continue
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target.name)

icd = WINE / "lib/wine/x86_64-unix/vulkan/icd.d"
icd.mkdir(parents=True, exist_ok=True)
(icd / "MoltenVK_icd.json").write_text(
    json.dumps(
        {
            "file_format_version": "1.0.0",
            "ICD": {
                "library_path": "../../../../libMoltenVK.dylib",
                "api_version": "1.4.0",
                "is_portability_driver": True,
            },
        },
        indent=2,
    )
    + "\n"
)

copy_legal_files(set(source_to_destination))

subprocess.run(["/usr/bin/xattr", "-cr", str(APP)], check=True)
for path in APP.rglob("*"):
    if not is_macho(path):
        continue
    subprocess.run(["/usr/bin/strip", "-S", "-x", str(path)], check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)
print(f"Prepared {APP} with {len(set(source_to_destination.values()))} native files")

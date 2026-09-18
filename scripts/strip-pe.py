#!/usr/bin/env python3
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "dist/Wine CX.app"
STRIP_I386 = Path(os.environ.get("MINGW_STRIP_I386", "/opt/local/bin/i686-w64-mingw32-strip"))
STRIP_X86_64 = Path(
    os.environ.get("MINGW_STRIP_X86_64", "/opt/local/bin/x86_64-w64-mingw32-strip")
)


def pe_layout(
    data: bytes | bytearray, path: Path
) -> tuple[int, int, int, int, bool, list[tuple[int, str, int, int, int, int]]]:
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("not PE")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("not PE")
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
    managed = False
    if directory_count > 14:
        cli_rva, cli_size = struct.unpack_from("<II", data, directory_offset + 14 * 8)
        managed = bool(cli_rva and cli_size)
    debug_directory_rva = 0
    debug_directory_size = 0
    if directory_count > 6:
        debug_entry = directory_offset + 6 * 8
        if debug_entry + 8 > optional + optional_size:
            raise RuntimeError(f"Truncated PE data directory in {path}")
        debug_directory_rva, debug_directory_size = struct.unpack_from("<II", data, debug_entry)
    section_table = optional + optional_size
    sections: list[tuple[int, str, int, int, int, int]] = []
    for index in range(section_count):
        section = section_table + index * 40
        if section + 40 > len(data):
            raise RuntimeError(f"Truncated PE section table in {path}")
        name = data[section : section + 8].split(b"\0", 1)[0].decode("ascii", "replace")
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, section + 8)
        sections.append((section, name, virtual_address, virtual_size, raw_offset, raw_size))
    return (
        directory_offset,
        debug_directory_rva,
        debug_directory_size,
        symbol_table or symbol_count,
        managed,
        sections,
    )


def pe_debug_state(path: Path) -> tuple[list[str], int, int]:
    data = path.read_bytes()
    _, _, debug_directory_size, symbol_state, _, sections = pe_layout(data, path)
    debug_sections: list[str] = []
    for _, name, _, _, _, _ in sections:
        if name.startswith((".debug", ".zdebug")) or name in {".stab", ".stabstr", ".buildid"}:
            debug_sections.append(name)
    return debug_sections, debug_directory_size, symbol_state


def strip_tool(data: bytes, path: Path) -> Path:
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    if machine == 0x014C:
        return STRIP_I386
    if machine == 0x8664:
        return STRIP_X86_64
    raise RuntimeError(f"Unsupported PE machine 0x{machine:04x} in {path}")


def clear_debug_directory(path: Path) -> None:
    data = bytearray(path.read_bytes())
    directory_offset, debug_rva, debug_size, _, _, sections = pe_layout(data, path)
    if debug_size:
        debug_offset = None
        for _, _, virtual_address, virtual_size, raw_offset, raw_size in sections:
            if virtual_address <= debug_rva < virtual_address + max(virtual_size, raw_size):
                debug_offset = raw_offset + debug_rva - virtual_address
                break
        if debug_offset is None or debug_offset + debug_size > len(data) or debug_size % 28:
            raise RuntimeError(f"Invalid PE debug directory in {path}")
        for entry_offset in range(debug_offset, debug_offset + debug_size, 28):
            entry = struct.unpack_from("<IIHHIIII", data, entry_offset)
            payload_size = entry[5]
            payload_offset = entry[7]
            if payload_size:
                if not payload_offset:
                    payload_rva = entry[6]
                    for _, _, virtual_address, virtual_size, raw_offset, raw_size in sections:
                        if virtual_address <= payload_rva < virtual_address + max(virtual_size, raw_size):
                            payload_offset = raw_offset + payload_rva - virtual_address
                            break
                if not payload_offset or payload_offset + payload_size > len(data):
                    raise RuntimeError(f"Invalid PE debug payload in {path}")
                data[payload_offset : payload_offset + payload_size] = b"\0" * payload_size
        data[debug_offset : debug_offset + debug_size] = b"\0" * debug_size
        data[directory_offset + 6 * 8 : directory_offset + 7 * 8] = b"\0" * 8
    for section, name, _, _, raw_offset, raw_size in sections:
        if name == ".buildid":
            if raw_offset + raw_size > len(data):
                raise RuntimeError(f"Invalid .buildid section in {path}")
            data[raw_offset : raw_offset + raw_size] = b"\0" * raw_size
            data[section : section + 8] = b"\0" * 8
    path.write_bytes(data)


def clear_empty_symbol_table_pointer(path: Path) -> None:
    data = bytearray(path.read_bytes())
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    coff = pe_offset + 4
    symbol_table, symbol_count = struct.unpack_from("<II", data, coff + 8)
    if symbol_table and not symbol_count:
        struct.pack_into("<I", data, coff + 8, 0)
        path.write_bytes(data)


if not APP.is_dir():
    raise RuntimeError(f"App is absent: {APP}")
for required in (STRIP_I386, STRIP_X86_64):
    if not required.is_file():
        raise RuntimeError(f"MinGW strip tool is absent: {required}")

files: list[Path] = []
bytes_before = 0
for path in APP.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        pe_debug_state(path)
    except ValueError:
        continue
    files.append(path)
    bytes_before += path.stat().st_size

if not files:
    raise RuntimeError(f"No PE files were found in {APP}")

for path in files:
    _, _, _, _, managed, _ = pe_layout(path.read_bytes(), path)
    clear_debug_directory(path)
    if not managed:
        tool = strip_tool(path.read_bytes(), path)
        subprocess.run([str(tool), "--strip-all", str(path)], check=True)
        clear_debug_directory(path)
        clear_empty_symbol_table_pointer(path)
    debug_sections, debug_directory_size, symbol_state = pe_debug_state(path)
    if debug_sections or debug_directory_size or symbol_state:
        raise RuntimeError(
            f"PE debug data remains in {path}: sections={debug_sections}, "
            f"directory_size={debug_directory_size}, symbol_state={symbol_state}"
        )

for path in APP.rglob("*"):
    if path.name.endswith(".dSYM") or path.name.endswith(".debug"):
        raise RuntimeError(f"Separate debug data remains in the app: {path}")

bytes_after = sum(path.stat().st_size for path in files)
print(f"Stripped {len(files)} PE files and removed {bytes_before - bytes_after} bytes")

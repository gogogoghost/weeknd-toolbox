"""Reproducible initramfs (cpio "newc" + gzip) writer.

Unlike `find | cpio | gzip`, every timestamp is 0, owners are root, and
entries are sorted, so identical inputs always give identical bytes.
"""

from __future__ import annotations

import gzip
import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    path: str  # relative, no leading "/"
    mode: int  # including file type bits (stat.S_IFREG etc.)
    data: bytes = b""  # file contents, or symlink target


def tree_entries(root: Path, exclude: set[str] = frozenset()) -> list[Entry]:
    """Entries for every dir/file/symlink under root (paths in `exclude` skipped)."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if _rel(rel_dir, d) not in exclude]
        for name in dirnames + filenames:
            rel = _rel(rel_dir, name)
            if rel in exclude:
                continue
            p = Path(dirpath) / name
            st = p.lstat()
            perm = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entries.append(Entry(rel, stat.S_IFLNK | 0o777, os.readlink(p).encode()))
            elif stat.S_ISDIR(st.st_mode):
                entries.append(Entry(rel, stat.S_IFDIR | perm))
            elif stat.S_ISREG(st.st_mode):
                entries.append(Entry(rel, stat.S_IFREG | perm, p.read_bytes()))
            else:
                raise ValueError(f"unsupported file type in initramfs source: {p}")
    return entries


def _rel(rel_dir: str, name: str) -> str:
    return name if rel_dir == "." else f"{rel_dir}/{name}"


def _header(ino: int, mode: int, nlink: int, size: int, namesize: int) -> bytes:
    fields = [ino, mode, 0, 0, nlink, 0, size, 0, 0, 0, 0, namesize, 0]
    return b"070701" + b"".join(b"%08X" % f for f in fields)


def _pad4(n: int) -> bytes:
    return b"\0" * (-n % 4)


def cpio_newc(entries: list[Entry]) -> bytes:
    paths = [e.path for e in entries]
    if len(paths) != len(set(paths)):
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        raise ValueError(f"duplicate initramfs entries: {dupes}")
    out = bytearray()
    for ino, e in enumerate(sorted(entries, key=lambda e: e.path), start=1):
        name = e.path.encode() + b"\0"
        nlink = 2 if stat.S_ISDIR(e.mode) else 1
        out += _header(ino, e.mode, nlink, len(e.data), len(name)) + name
        out += _pad4(110 + len(name))
        out += e.data + _pad4(len(e.data))
    trailer = b"TRAILER!!!\0"
    out += _header(0, 0, 1, 0, len(trailer)) + trailer + _pad4(110 + len(trailer))
    return bytes(out)


def build(entries: list[Entry]) -> bytes:
    return gzip.compress(cpio_newc(entries), compresslevel=9, mtime=0)

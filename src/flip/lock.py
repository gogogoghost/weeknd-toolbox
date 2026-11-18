"""Load and validate pins.toml."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from flip import config
from flip.util import FlipError

TRUST_LEVELS = {"upstream-release", "pinned", "pinned-on-first-use"}
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT = re.compile(r"\d{8}T\d{6}Z")


def _url_override(name: str, url: str) -> str:
    var = "FLIP_URL_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    return os.environ.get(var, url)


@dataclass(frozen=True)
class GitPin:
    name: str
    url: str
    commit: str
    trust: str
    note: str = ""


@dataclass(frozen=True)
class FilePin:
    name: str
    url: str
    sha256: str
    trust: str
    note: str = ""


@dataclass(frozen=True)
class DebianPin:
    """A dated snapshot of the Debian archive (snapshot.debian.org)."""
    suite: str
    snapshot: str  # e.g. 20261117T000000Z
    trust: str
    note: str = ""

    @property
    def mirrors(self) -> list[str]:
        base = "https://snapshot.debian.org/archive"
        return [
            f"deb {base}/debian/{self.snapshot} {self.suite} main",
            f"deb {base}/debian/{self.snapshot} {self.suite}-updates main",
            f"deb {base}/debian-security/{self.snapshot} {self.suite}-security main",
        ]


@dataclass(frozen=True)
class Lock:
    git: dict[str, GitPin]
    file: dict[str, FilePin]
    debian: DebianPin | None = None


def load(path: Path | None = None) -> Lock:
    path = path or config.LOCKFILE
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise FlipError(f"cannot read lockfile {path}: {e}") from e

    unknown = set(data) - {"git", "file", "debian"}
    if unknown:
        raise FlipError(f"{path}: unknown sections {sorted(unknown)}")

    git: dict[str, GitPin] = {}
    for name, entry in data.get("git", {}).items():
        _check_keys(path, f"git.{name}", entry, {"url", "commit", "trust"}, {"note"})
        if not _HEX40.fullmatch(entry["commit"]):
            raise FlipError(f"{path}: git.{name}.commit must be a full 40-char lowercase SHA")
        git[name] = GitPin(
            name, _url_override(name, entry["url"]), entry["commit"], entry["trust"],
            entry.get("note", ""),
        )

    files: dict[str, FilePin] = {}
    for name, entry in data.get("file", {}).items():
        _check_keys(path, f"file.{name}", entry, {"url", "sha256", "trust"}, {"note"})
        if not _HEX64.fullmatch(entry["sha256"]):
            raise FlipError(f"{path}: file.{name}.sha256 must be 64 lowercase hex chars")
        files[name] = FilePin(
            name, _url_override(name, entry["url"]), entry["sha256"], entry["trust"],
            entry.get("note", ""),
        )
    debian = None
    if "debian" in data:
        entry = data["debian"]
        _check_keys(path, "debian", entry, {"suite", "snapshot", "trust"}, {"note"})
        if not _SNAPSHOT.fullmatch(entry["snapshot"]):
            raise FlipError(f"{path}: debian.snapshot must look like 20261117T000000Z")
        debian = DebianPin(entry["suite"], entry["snapshot"], entry["trust"],
                           entry.get("note", ""))
    return Lock(git, files, debian)


def _check_keys(path: Path, where: str, entry: dict, required: set, optional: set) -> None:
    missing = required - set(entry)
    extra = set(entry) - required - optional
    if missing:
        raise FlipError(f"{path}: {where} is missing {sorted(missing)}")
    if extra:
        raise FlipError(f"{path}: {where} has unknown keys {sorted(extra)}")
    if entry["trust"] not in TRUST_LEVELS:
        raise FlipError(f"{path}: {where}.trust must be one of {sorted(TRUST_LEVELS)}")

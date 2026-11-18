"""The AVB signing key the phone trusts, kept private outside the repo."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from flip import config, images
from flip.util import FlipError, run, say, sha256_file, warn, write_private


def ensure_key() -> Path:
    """Return the signing key, creating it (privately) only if none exists."""
    config.ensure_config_dir()
    key = config.avb_key()
    if not key.exists():
        say(f"generating new signing key: {key}")
        warn("the phone must be flashed with the new public key (`flip flash`) "
             "before it accepts images signed with it")
        old_umask = os.umask(0o077)
        try:
            run(["openssl", "genrsa", "-out", key, "2048"])
        finally:
            os.umask(old_umask)
    fix_permissions(key)
    return key


def fix_permissions(key: Path) -> None:
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        warn(f"{key} was readable by others; fixing permissions")
        os.chmod(key, 0o600)


def derive_pkmd(key: Path, out: Path, quiet: bool = False) -> str:
    """Write the public key metadata (what avb_custom_key holds); return its sha256."""
    out.parent.mkdir(parents=True, exist_ok=True)
    images.avb_extract_public_key(key, out, quiet=quiet)
    return sha256_file(out)


def flashed_pkmd() -> str | None:
    try:
        return config.flashed_pkmd_record().read_text().strip() or None
    except FileNotFoundError:
        return None


def record_flashed(pkmd_sha256: str) -> None:
    config.ensure_config_dir()
    write_private(config.flashed_pkmd_record(), pkmd_sha256 + "\n")


@dataclass
class KeyStatus:
    path: Path
    exists: bool
    private: bool
    pkmd_sha256: str | None
    flashed_sha256: str | None

    @property
    def matches_phone(self) -> bool | None:
        if not self.pkmd_sha256 or not self.flashed_sha256:
            return None
        return self.pkmd_sha256 == self.flashed_sha256


def status(scratch: Path) -> KeyStatus:
    key = config.avb_key()
    if not key.exists():
        return KeyStatus(key, False, False, None, flashed_pkmd())
    private = not (stat.S_IMODE(key.stat().st_mode) & 0o077)
    pkmd = derive_pkmd(key, scratch / "pkmd.bin", quiet=True)
    return KeyStatus(key, True, private, pkmd, flashed_pkmd())


def export(dest: Path) -> list[Path]:
    """Copy the key (and its public metadata) to dest/, readable only by you."""
    key = config.avb_key()
    if not key.exists():
        raise FlipError(f"no signing key at {key}")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / key.name
    if out.exists():
        raise FlipError(f"{out} already exists; refusing to overwrite")
    write_private(out, key.read_bytes())
    pkmd = dest / "avb-key.pkmd.bin"
    derive_pkmd(key, pkmd, quiet=True)
    shutil.copymode(out, pkmd)
    return [out, pkmd]

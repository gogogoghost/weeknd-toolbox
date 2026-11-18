"""Paths and user settings."""

from __future__ import annotations

import os
from pathlib import Path

from flip.util import say

# The repository root: the CLI lives in this repo next to the files it builds.
REPO = Path(__file__).resolve().parents[2]

ROOTFS = REPO / "rootfs"
TOOLS = REPO / "tools"
PATCHES = REPO / "patches"
LK2ND_DTS = REPO / "lk2nd-dts-msm8952"
LOCKFILE = REPO / "pins.toml"
DEFCONFIG = REPO / "defconfig"

LINUX_DIR = REPO / "linux"  # pinned kernel checkout + in-tree build
LK2ND_DIR = REPO / "lk2nd"  # pinned lk2nd checkout + build
OUT = REPO / "out"  # build outputs
STAMPS = OUT / ".stamps"  # input fingerprints of expensive steps
CACHE = REPO / "cache"  # verified downloads
BACKUPS = REPO / "backups"  # device backups (gitignored, sensitive)


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


LEGACY_CONFIG_DIR = _xdg_config() / "weeknd-toolbox"


def config_dir() -> Path:
    """Private per-user directory: signing key, own vbmeta, flash records."""
    return Path(os.environ.get("FLIP_CONFIG_DIR") or _xdg_config() / "flip")


def ensure_config_dir() -> Path:
    d = config_dir()
    if not d.exists() and LEGACY_CONFIG_DIR.is_dir() and "FLIP_CONFIG_DIR" not in os.environ:
        LEGACY_CONFIG_DIR.rename(d)
        say(f"moved {LEGACY_CONFIG_DIR} -> {d}")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def avb_key() -> Path:
    return config_dir() / "avb-key.pem"


def own_vbmeta() -> Path:
    return config_dir() / "stock-vbmeta.img"


def flashed_pkmd_record() -> Path:
    return config_dir() / "flashed-pkmd.sha256"


def cross_compile() -> str:
    return os.environ.get("CROSS_COMPILE", "aarch64-linux-gnu-")


def toolchain_prefix() -> str:
    return os.environ.get("TOOLCHAIN_PREFIX", "arm-none-eabi-")


def jobs_default() -> int:
    return os.cpu_count() or 1


def host_key() -> Path:
    """This computer's flip-link identity (the phone trusts its public half)."""
    return config_dir() / "host-key.pem"


def phone_key() -> Path:
    """The phone's flip-link public key, so `flip` can tell it's really your phone."""
    return config_dir() / "phone-key.pub"

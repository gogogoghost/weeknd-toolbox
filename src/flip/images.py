"""Android image tools (vendored in tools/) and the recovery image layout."""

from __future__ import annotations

import sys
from pathlib import Path

from flip import config, profile
from flip.util import FlipError, run


def _tool(script: str, *args, quiet: bool = False) -> None:
    run([sys.executable, "-I", config.TOOLS / script, *args], quiet=quiet, capture=quiet)


def mkbootimg(out: Path, kernel: Path, cmdline: str, *, ramdisk: Path | None = None,
              header_version: int | None = None, recovery_dtbo: Path | None = None) -> None:
    args = ["-o", out, "--kernel", kernel, "--cmdline", cmdline]
    if ramdisk:
        args += ["--ramdisk", ramdisk]
    if header_version is not None:
        args += ["--header_version", str(header_version)]
    if recovery_dtbo:
        args += ["--recovery_dtbo", recovery_dtbo]
    _tool("mkbootimg.py", *args)


def mkdtboimg(out: Path, dtbs: list[Path]) -> None:
    _tool("mkdtboimg.py", "create", out, *dtbs)


def avb_extract_public_key(key: Path, out: Path, quiet: bool = False) -> None:
    _tool("avbtool.py", "extract_public_key", "--key", key, "--output", out, quiet=quiet)


def avb_make_vbmeta(out: Path, key: Path, pkmd: Path, stock_vbmeta: Path) -> None:
    _tool(
        "avbtool.py", "make_vbmeta_image",
        "--padding_size", "4096",
        "--algorithm", "SHA512_RSA2048",
        "--key", key,
        "--public_key_metadata", pkmd,
        "--output", out,
        "--include_descriptors_from_image", stock_vbmeta,
        "--set_hashtree_disabled_flag",
    )


def assemble_recovery(lk2nd_img: bytes, boot_img: bytes) -> bytes:
    """lk2nd in the first LK2ND_SLOT bytes, the toolbox boot image after it."""
    if len(lk2nd_img) > profile.LK2ND_SLOT:
        raise FlipError(
            f"lk2nd image is {len(lk2nd_img)} bytes, larger than its "
            f"{profile.LK2ND_SLOT}-byte slot: the toolbox would overwrite it"
        )
    image = lk2nd_img.ljust(profile.LK2ND_SLOT, b"\0") + boot_img
    if len(image) > profile.RECOVERY_SIZE:
        raise FlipError(
            f"recovery image is {len(image)} bytes, larger than the "
            f"{profile.RECOVERY_SIZE}-byte recovery partition"
        )
    return image

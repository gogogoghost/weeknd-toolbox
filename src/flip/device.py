"""Talk to the phone: detect its mode, read bootloader info and logs."""

from __future__ import annotations

import os
import subprocess
import tempfile
from enum import Enum
from pathlib import Path

from flip import profile
from flip.util import FlipError


class Mode(Enum):
    STOCK_FASTBOOT = "stock fastboot (Nokia bootloader)"
    LK2ND_FASTBOOT = "lk2nd fastboot"
    TOOLBOX = "toolbox (USB storage)"
    OTHER_FASTBOOT = "fastboot, unknown device"
    NONE = "not detected"


def _fastboot_cmd() -> str:
    return os.environ.get("FLIP_FASTBOOT", "fastboot")


def fastboot(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run fastboot, combining stderr into stdout (fastboot reports there)."""
    try:
        return subprocess.run(
            [_fastboot_cmd(), *args], text=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except FileNotFoundError as e:
        raise FlipError("fastboot not found (Arch: sudo pacman -S android-tools)") from e
    except subprocess.TimeoutExpired as e:
        raise FlipError(f"fastboot {' '.join(args)} timed out") from e


def fastboot_checked(*args: str, timeout: int = 120) -> str:
    result = fastboot(*args, timeout=timeout)
    if result.returncode != 0:
        raise FlipError(f"fastboot {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout


def fastboot_serials() -> list[str]:
    out = fastboot("devices", timeout=10).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip().endswith("fastboot")]


def getvar(name: str) -> str | None:
    out = fastboot("getvar", name, timeout=10).stdout
    for line in out.splitlines():
        if line.startswith(f"{name}:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def usb_ids(sysfs: Path = Path("/sys/bus/usb/devices")) -> set[tuple[str, str]]:
    ids = set()
    for dev in sysfs.glob("*"):
        try:
            ids.add(((dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip()))
        except OSError:
            continue
    return ids


def detect_mode() -> Mode:
    if fastboot_serials():
        product = getvar("product")
        if product == profile.PRODUCT_STOCK:
            return Mode.STOCK_FASTBOOT
        if product == profile.PRODUCT_LK2ND:
            return Mode.LK2ND_FASTBOOT
        return Mode.OTHER_FASTBOOT
    if profile.TOOLBOX_USB_ID in usb_ids():
        return Mode.TOOLBOX
    return Mode.NONE


def getvar_all() -> str:
    return fastboot("getvar", "all").stdout


def lk2nd_log() -> str:
    """lk2nd's boot log (only available in lk2nd fastboot)."""
    fastboot_checked("oem", "log", timeout=20)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "log.txt"
        fastboot_checked("get_staged", str(path), timeout=20)
        return path.read_text(errors="replace")

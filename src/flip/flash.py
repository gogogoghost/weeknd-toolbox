"""Flash the phone, with guard rails."""

from __future__ import annotations

from enum import Enum
from typing import Callable

from flip import device, keys, profile
from flip.build import OUT
from flip.device import Mode
from flip.util import FlipError, say, sha256_file, warn


class Target(str, Enum):
    ALL = "all"  # signing key + vbmeta + recovery (first install)
    RECOVERY = "recovery"  # lk2nd + toolbox only


BOOT_HELP = (
    "To boot the toolbox: power off, hold volume-up while powering on, and "
    "release it when the 'custom operating system' warning appears."
)


def plan(target: Target) -> list[tuple[str, ...]]:
    """The fastboot commands a flash runs, in order."""
    steps = [("oem", "sudo")]
    if target is Target.ALL:
        steps += [
            ("flash", "avb_custom_key", str(OUT.pkmd)),
            ("flash", "vbmeta", str(OUT.vbmeta)),
        ]
    steps.append(("flash", "recovery", str(OUT.recovery)))
    return steps


def preflight(target: Target) -> list[str]:
    """Check outputs, device mode and key consistency. Returns warnings."""
    needed = [OUT.recovery] + ([OUT.pkmd, OUT.vbmeta] if target is Target.ALL else [])
    missing = [p.name for p in needed if not p.exists()]
    if missing:
        raise FlipError(f"missing build outputs {missing}: run `flip build` first")

    if OUT.recovery.stat().st_size > profile.RECOVERY_SIZE:
        raise FlipError("recovery.img is larger than the recovery partition")
    if target is Target.ALL and OUT.vbmeta.stat().st_size > profile.VBMETA_SIZE:
        raise FlipError("vbmeta.img is larger than the vbmeta partition")

    mode = device.detect_mode()
    if mode is Mode.LK2ND_FASTBOOT:
        raise FlipError(
            "the phone is in lk2nd's fastboot. Don't flash from here: lk2nd hides its "
            "own first 512 KiB, so 'recovery' would land in the wrong place.\n"
            "   Power off, then hold volume-down while plugging in USB (stock fastboot)."
        )
    if mode is not Mode.STOCK_FASTBOOT:
        raise FlipError(
            f"phone not in stock fastboot (detected: {mode.value}).\n"
            "   Power off, then hold volume-down while plugging in USB."
        )

    warnings = []
    pkmd_sha = sha256_file(OUT.pkmd) if OUT.pkmd.exists() else None
    flashed = keys.flashed_pkmd()
    if target is Target.RECOVERY and pkmd_sha and flashed and pkmd_sha != flashed:
        warnings.append(
            "your current signing key differs from the one the phone trusts; "
            "only recovery is being flashed, so the phone keeps the old key"
        )
    if target is Target.ALL and flashed and pkmd_sha and pkmd_sha != flashed:
        warnings.append("this replaces the signing key the phone trusts with your current one")
    return warnings


def flash(target: Target, confirm: Callable[[str], bool]) -> None:
    warnings = preflight(target)
    say(f"phone: {device.getvar('serialno') or 'unknown serial'} in stock fastboot")
    say("will run:")
    for cmd in plan(target):
        print("     fastboot " + " ".join(cmd))
    for w in warnings:
        warn(w)
    if not confirm("Flash the phone?"):
        raise FlipError("aborted, nothing was flashed")

    for cmd in plan(target):
        say("fastboot " + " ".join(cmd))
        print(device.fastboot_checked(*cmd), end="")
        if cmd[:2] == ("flash", "avb_custom_key"):
            keys.record_flashed(sha256_file(OUT.pkmd))
    say("flash complete.")
    say(BOOT_HELP)

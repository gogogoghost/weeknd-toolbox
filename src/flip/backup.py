"""Full eMMC backups via the toolbox's read-only USB storage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from flip import config, profile
from flip.util import FlipError, run, say, sha256_file, write_private

CHUNK = 4 << 20


@dataclass
class BlockDevice:
    path: str
    size: int
    ro: bool
    model: str
    vendor: str
    mounted: bool


def inspect_device(dev: str) -> BlockDevice:
    out = run(["lsblk", "-J", "-b", "-o", "NAME,SIZE,RO,MODEL,VENDOR,MOUNTPOINTS", dev],
              capture=True, quiet=True).stdout
    node = json.loads(out)["blockdevices"][0]

    def mounted(n: dict) -> bool:
        if any(m for m in (n.get("mountpoints") or []) if m):
            return True
        return any(mounted(c) for c in n.get("children", []))

    return BlockDevice(
        dev, int(node["size"]), bool(node["ro"]) and str(node["ro"]) not in ("0", "False"),
        (node.get("model") or "").strip(), (node.get("vendor") or "").strip(), mounted(node),
    )


def check_device(bd: BlockDevice) -> None:
    lo, hi = profile.EMMC_SIZE_RANGE
    if bd.model != profile.TOOLBOX_USB_MODEL:
        raise FlipError(f"{bd.path} is '{bd.vendor} {bd.model}', not the toolbox's "
                        f"'{profile.TOOLBOX_USB_MODEL}'. Wrong device?")
    if not lo <= bd.size <= hi:
        raise FlipError(f"{bd.path} is {bd.size} bytes; the phone's whole eMMC is ~3.4 GiB. "
                        "In the toolbox choose USB storage -> 5 (Whole eMMC).")
    if not bd.ro:
        raise FlipError(f"{bd.path} is writable. Switch the toolbox to read-only mode "
                        "(USB storage -> 7) before taking a backup.")
    if bd.mounted:
        raise FlipError(f"a partition of {bd.path} is mounted; unmount it first")


def create(dev: str) -> Path:
    if not Path(dev).is_block_device():
        raise FlipError(f"{dev} is not a block device (see lsblk)")
    bd = inspect_device(dev)
    say(f"{dev}: '{bd.vendor} {bd.model}', {bd.size} bytes, read-only={bd.ro}")
    check_device(bd)

    out = config.BACKUPS / datetime.now().strftime("%Y-%m-%d-%H%M%S")
    (out / "parts").mkdir(parents=True)
    os.chmod(out, 0o700)
    image = out / "emmc.img"
    say(f"reading {dev} -> {image} (sudo is needed for raw device access)")
    with open(image, "wb") as f:
        result = subprocess.run(
            ["sudo", "dd", f"if={dev}", "bs=4M", "iflag=direct", "status=progress"], stdout=f)
    if result.returncode != 0:
        raise FlipError(f"reading {dev} failed; partial backup left in {out}")
    if image.stat().st_size != bd.size:
        raise FlipError(f"image is {image.stat().st_size} bytes, expected {bd.size}: "
                        f"incomplete read, partial backup left in {out}")
    finish(out)
    return out


def finish(out: Path) -> None:
    """Extract partitions, hash everything and make the backup read-only."""
    image = out / "emmc.img"
    table = run(["sfdisk", "--json", image], capture=True, quiet=True).stdout
    (out / "gpt.json").write_text(table)
    for name, size in extract_partitions(image, json.loads(table), out / "parts"):
        print(f"     {name:14s} {size:>12d} bytes")
    write_sums(out)
    for p in [out, *out.rglob("*")]:
        os.chmod(p, stat.S_IMODE(p.stat().st_mode) & ~0o222)
    say(f"done: {out} (read-only)")


def extract_partitions(image: Path, gpt: dict, dest: Path) -> list[tuple[str, int]]:
    table = gpt["partitiontable"]
    sector = table.get("sectorsize", 512)
    done, seen = [], set()
    with open(image, "rb") as img:
        for p in table["partitions"]:
            name = p.get("name") or Path(p["node"]).name
            if name in seen or "/" in name:
                raise FlipError(f"unexpected partition name {name!r}")
            seen.add(name)
            img.seek(p["start"] * sector)
            remaining = p["size"] * sector
            with open(dest / f"{name}.img", "wb") as f:
                while remaining:
                    chunk = img.read(min(remaining, CHUNK))
                    if not chunk:
                        raise FlipError(f"short read in partition {name}")
                    f.write(chunk)
                    remaining -= len(chunk)
            done.append((name, p["size"] * sector))
    return done


def write_sums(out: Path) -> None:
    files = [out / "emmc.img", *sorted((out / "parts").glob("*.img"))]
    lines = [f"{sha256_file(f)}  {f.relative_to(out)}\n" for f in files]
    (out / "SHA256SUMS").write_text("".join(lines))


def list_backups() -> list[Path]:
    if not config.BACKUPS.is_dir():
        return []
    return sorted(p for p in config.BACKUPS.iterdir() if (p / "SHA256SUMS").is_file())


def resolve(name: str | None) -> Path:
    backups = list_backups()
    if not backups:
        raise FlipError("no backups found (create one with `flip backup create`)")
    if name is None:
        return backups[-1]
    path = config.BACKUPS / name
    if path not in backups:
        raise FlipError(f"no backup named {name!r}; see `flip backup list`")
    return path


def verify(backup: Path) -> list[str]:
    """Re-hash every file; return the names that don't match SHA256SUMS."""
    bad = []
    for line in (backup / "SHA256SUMS").read_text().splitlines():
        want, rel = line.split(maxsplit=1)
        f = backup / rel
        if not f.is_file() or sha256_file(f) != want:
            bad.append(rel)
    return bad


def adopt_vbmeta(backup: Path) -> Path:
    """Use this backup's vbmetabak (the untouched stock vbmeta) for builds."""
    src = backup / "parts" / "vbmetabak.img"
    if not src.is_file():
        raise FlipError(f"{src} not found")
    config.ensure_config_dir()
    dest = config.own_vbmeta()
    write_private(dest, src.read_bytes())
    return dest

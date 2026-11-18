"""The SD card system: build a Debian image, write it to a card, update a card.

`flip sd build` makes out/sd/sd.img without root:
  1. mmdebstrap installs pinned Debian packages for arm64 (ARM programs run
     under qemu emulation) into a tarball, adding sd/overlay/, the kernel
     modules, your phone's firmware and the flip-link keys, then runs
     sd/setup.sh inside.
  2. mke2fs -d turns the tarball into an ext4 root file system, and the boot
     files (kernel, device tree, extlinux.conf) into an ext2 one, the kind
     lk2nd can read.
  3. Both go into a GPT disk image with fixed partition IDs.

The image contains your phone's firmware and its flip-link identity key:
treat it like backups/ (private, never shared or committed).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from flip import backup, build, config, fetch, lock, phone, profile
from flip.link import keys as lkeys
from flip.link.secure import fingerprint
from flip.util import FlipError, run, say, sha256_file, warn

SD = config.OUT / "sd"
IMAGE = SD / "sd.img"
MANIFEST = SD / "manifest.json"
SD_SRC = config.REPO / "sd"
KEYRING = Path("/usr/share/keyrings/debian-archive-keyring.gpg")
HOST_TOOLS = ["perl", "apt", "dpkg", "mke2fs", "sfdisk", "mcopy", "debugfs", "newuidmap",
              "qemu-aarch64-static"]

MiB = 1 << 20
SECTOR = 512
# GPT partition type GUIDs
TYPE_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
TYPE_ROOT_ARM64 = "B921B045-1DF0-41C3-AF44-4C6F280D3FAE"
# Namespace for the image's partition and file system IDs (derived, not random,
# so identical inputs give identical IDs).
_NS = uuid.UUID("6c1f3a52-8d0e-4b8e-9a51-6f6c6970736f")


def missing_tools() -> list[str]:
    missing = [t for t in HOST_TOOLS if shutil.which(t) is None]
    if not KEYRING.exists():
        missing.append("debian-archive-keyring")
    binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
    if not binfmt.exists() or "enabled" not in binfmt.read_text():
        missing.append("qemu-user-static-binfmt (arm64 emulation not registered)")
    return missing


def check_tools() -> None:
    missing = missing_tools()
    if missing:
        raise FlipError(
            "missing tools for building the SD card: " + ", ".join(missing) + "\n"
            "   On Arch: sudo pacman -S --needed apt debian-archive-keyring mtools e2fsprogs \\\n"
            "            qemu-user-static qemu-user-static-binfmt")


# --- inputs ------------------------------------------------------------------

def phone_identity():
    """The phone's flip-link key. Kept (privately) in the config dir so that
    rebuilding a card keeps the identity your computers already know."""
    config.ensure_config_dir()
    path = config.config_dir() / "phone-identity.pem"
    key = lkeys.load_private(path) if path.exists() else lkeys.generate(path)
    pub = lkeys.encode_public(lkeys.public_bytes(key.public_key()))
    config.phone_key().write_text(pub + "\n")
    return key, path


def extract_firmware(backup_dir: Path, dest: Path) -> list[str]:
    """Copy the phone's own firmware out of a backup into dest (lib/firmware layout)."""
    parts = backup_dir / "parts"
    modem, persist = parts / "modem.img", parts / "persist.img"
    for p in (modem, persist):
        if not p.exists():
            raise FlipError(f"{p} missing: is {backup_dir.name} a complete backup?")
    dest.mkdir(parents=True, exist_ok=True)
    # The modem partition (FAT) holds the firmware for the modem, Wi-Fi
    # (wcnss), audio DSP (adsp) and more, as image/*.mdt + .bNN files.
    run(["mcopy", "-n", "-m", "-i", modem, "::image/*", dest], quiet=True,
        env={"MTOOLS_SKIP_CHECK": "1"})
    # Wi-Fi calibration data lives on the persist partition (ext4).
    nv = dest / "wlan/prima/WCNSS_qcom_wlan_nv.bin"
    nv.parent.mkdir(parents=True, exist_ok=True)
    run(["debugfs", "-R", f"dump /WCNSS_qcom_wlan_nv.bin {nv}", persist], quiet=True,
        capture=True)
    if not nv.exists() or nv.stat().st_size == 0:
        raise FlipError("WCNSS_qcom_wlan_nv.bin not found on the persist partition")
    # mcopy names: lowercase what FAT may report in upper case.
    for f in list(dest.iterdir()):
        if f.is_file() and f.name != f.name.lower():
            f.rename(f.with_name(f.name.lower()))
    files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    for name in ("mba.mbn", "modem.mdt", "wcnss.mdt", "adsp.mdt"):
        if name not in files:
            warn(f"firmware {name} not found in the modem partition")
    return files


# --- image building --------------------------------------------------------------

@dataclass(frozen=True)
class Ids:
    disk: str
    boot: str
    root: str
    boot_fs: str
    root_fs: str

    @classmethod
    def derive(cls, seed: str) -> "Ids":
        u = lambda what: str(uuid.uuid5(_NS, f"{what}:{seed}"))  # noqa: E731
        return cls(u("disk"), u("boot"), u("root"), u("boot-fs"), u("root-fs"))


def extlinux_conf(ids: Ids, debug: bool) -> str:
    cmdline = f"root=PARTUUID={ids.root} rootwait rw {profile.SD_CMDLINE}"
    if debug:
        cmdline += " loglevel=7 systemd.log_level=debug"
    return (
        "# Written by `flip sd build`. lk2nd boots the default label.\n"
        "default flip\n"
        "timeout 1\n\n"
        "label flip\n"
        "    linux /Image.gz\n"
        f"    fdt /{profile.KERNEL_DTB_NAME}\n"
        f"    append {cmdline}\n"
    )


def fstab(ids: Ids) -> str:
    return (
        "# Written by `flip sd build`.\n"
        f"PARTUUID={ids.root}  /      ext4  rw,noatime,x-systemd.growfs          0 1\n"
        f"PARTUUID={ids.boot}  /boot  ext2  rw,noatime,nodev,nosuid,noexec      0 2\n"
    )


def stage(stage_dir: Path, ids: Ids, identity_pem: Path, firmware_from: Path,
          debug: bool) -> dict:
    """Assemble the files added on top of Debian (copied in by mmdebstrap)."""
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    shutil.copytree(SD_SRC / "overlay", stage_dir, symlinks=True)
    if debug:
        shutil.copytree(SD_SRC / "debug", stage_dir, symlinks=True, dirs_exist_ok=True)
    (stage_dir / "etc/fstab").write_text(fstab(ids))

    # flipd and its keys
    flipd = stage_dir / "usr/lib/flipd/flip"
    shutil.copytree(Path(__file__).parent / "link", flipd / "link",
                    ignore=shutil.ignore_patterns("__pycache__", "client.py"))
    (flipd / "__init__.py").write_text('"""flip-link server, installed by flip sd build."""\n')
    etc_flipd = stage_dir / "etc/flipd"
    etc_flipd.mkdir(parents=True)
    shutil.copyfile(identity_pem, etc_flipd / "identity.pem")
    host = phone.ensure_host_key()
    lkeys.write_trusted(etc_flipd / "trusted-hosts", [
        lkeys.TrustedHost(lkeys.public_bytes(host.public_key()), phone.host_label())])

    # kernel modules + firmware
    release = build.OUT.kernel_release.read_text().strip()
    shutil.copytree(build.OUT.modules / "lib/modules" / release,
                    stage_dir / "usr/lib/modules" / release, symlinks=True)
    firmware = extract_firmware(firmware_from, stage_dir / "usr/lib/firmware")
    return {"kernel_release": release, "firmware": firmware}


def build_rootfs(pins: lock.Lock, stage_dir: Path, tarball: Path, debug: bool) -> None:
    mmdebstrap = fetch.fetch_file(pins.file["mmdebstrap"])
    deb = pins.debian
    epoch = _snapshot_epoch(deb.snapshot)
    setup = SD_SRC / "setup.sh"
    cmd = [
        "perl", mmdebstrap,
        "--mode=unshare", "--variant=apt", "--architectures=arm64",
        f"--keyring={KEYRING}",
        "--aptopt=Acquire::Check-Valid-Until \"false\"",  # snapshots are old by design
        "--aptopt=Acquire::Retries \"3\"",
        "--aptopt=APT::Install-Recommends \"false\"",
        "--include=" + ",".join(profile.SD_PACKAGES),
        f"--customize-hook=sync-in {stage_dir} /",
        f"--customize-hook=copy-in {setup} /tmp",
        f"--customize-hook=chroot \"$1\" /bin/sh /tmp/setup.sh {1 if debug else 0}",
        "--customize-hook=rm \"$1/tmp/setup.sh\"",
        deb.suite, tarball, *deb.mirrors,
    ]
    run(cmd, env={"SOURCE_DATE_EPOCH": str(epoch)})


def _snapshot_epoch(snapshot: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.strptime(snapshot, "%Y%m%dT%H%M%SZ")
               .replace(tzinfo=timezone.utc).timestamp())


def _round_up(n: int, to: int) -> int:
    return (n + to - 1) // to * to


def root_fs_size(tarball: Path) -> int:
    with tarfile.open(tarball) as tf:
        content = sum(m.size for m in tf.getmembers() if m.isfile())
    # room for file system overhead plus some headroom; the partition and file
    # system grow to fill the card on first boot.
    return _round_up(int(content * 1.25) + 384 * MiB, 64 * MiB)


def make_ext(kind: str, out: Path, size: int, source: Path, label: str, fs_uuid: str,
             epoch: int) -> None:
    out.unlink(missing_ok=True)
    with open(out, "wb") as f:
        f.truncate(size)
    args = ["mke2fs", "-q", "-F", "-t", kind, "-L", label, "-U", fs_uuid,
            "-E", f"hash_seed={fs_uuid}", "-d", source, out]
    run(args, env={"E2FSPROGS_FAKE_TIME": str(epoch)})


def assemble(out: Path, boot_img: Path, root_img: Path, ids: Ids) -> None:
    boot_size, root_size = boot_img.stat().st_size, root_img.stat().st_size
    start_boot = MiB
    start_root = start_boot + boot_size
    total = start_root + root_size + MiB  # room for the backup GPT
    out.unlink(missing_ok=True)
    with open(out, "wb") as f:
        f.truncate(total)
    script = (
        "label: gpt\n"
        f"label-id: {ids.disk}\n"
        f"start={start_boot // SECTOR}, size={boot_size // SECTOR}, type={TYPE_LINUX}, "
        f"uuid={ids.boot}, name=\"{profile.SD_BOOT_LABEL}\"\n"
        f"start={start_root // SECTOR}, size={root_size // SECTOR}, type={TYPE_ROOT_ARM64}, "
        f"uuid={ids.root}, name=\"{profile.SD_ROOT_LABEL}\"\n"
    )
    run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", out], input_=script)
    with open(out, "r+b") as dst:
        for img, offset in ((boot_img, start_boot), (root_img, start_root)):
            dst.seek(offset)
            with open(img, "rb") as src:
                shutil.copyfileobj(src, dst, 4 * MiB)


def build_image(debug: bool = False, backup_name: str | None = None) -> Path:
    check_tools()
    pins = lock.load()
    if pins.debian is None:
        raise FlipError("pins.toml has no [debian] section")
    for out in (build.OUT.kernel_image, build.OUT.dtb, build.OUT.kernel_release):
        if not out.exists():
            raise FlipError(f"{out.name} missing: run `flip build` first")
    firmware_from = backup.resolve(backup_name)

    SD.mkdir(parents=True, exist_ok=True)
    os.chmod(SD, 0o700)
    identity, identity_path = phone_identity()
    phone_pub = lkeys.public_bytes(identity.public_key())
    seed_src = json.dumps([pins.debian.snapshot, profile.SD_PACKAGES, debug,
                           lkeys.encode_public(phone_pub)])
    ids = Ids.derive(hashlib.sha256(seed_src.encode()).hexdigest())
    epoch = _snapshot_epoch(pins.debian.snapshot)

    say("[1] staging files (modules, firmware from backup "
        f"{firmware_from.name}, flip-link keys)")
    stage_dir = SD / "stage"
    staged = stage(stage_dir, ids, identity_path, firmware_from, debug)

    say(f"[2] Debian {pins.debian.suite} (snapshot {pins.debian.snapshot}) for arm64; "
        "this downloads ~200 MB and takes a while under emulation")
    tarball = SD / "rootfs.tar"
    build_rootfs(pins, stage_dir, tarball, debug)

    say("[3] file systems")
    boot_dir = SD / "boot"
    if boot_dir.exists():
        shutil.rmtree(boot_dir)
    (boot_dir / "extlinux").mkdir(parents=True)
    shutil.copyfile(build.OUT.kernel_image, boot_dir / "Image.gz")
    shutil.copyfile(build.OUT.dtb, boot_dir / profile.KERNEL_DTB_NAME)
    (boot_dir / "extlinux/extlinux.conf").write_text(extlinux_conf(ids, debug))
    boot_img, root_img = SD / "boot.ext2", SD / "root.ext4"
    make_ext("ext2", boot_img, profile.SD_BOOT_SIZE, boot_dir, profile.SD_BOOT_LABEL,
             ids.boot_fs, epoch)
    make_ext("ext4", root_img, root_fs_size(tarball), tarball, profile.SD_ROOT_LABEL,
             ids.root_fs, epoch)

    say("[4] disk image")
    assemble(IMAGE, boot_img, root_img, ids)
    os.chmod(IMAGE, 0o600)
    for tmp in (boot_img, root_img, tarball):
        tmp.unlink()
    shutil.rmtree(stage_dir)
    digest = sha256_file(IMAGE)
    manifest = {
        "image": IMAGE.name, "size": IMAGE.stat().st_size, "sha256": digest,
        "debug": debug, "debian": {"suite": pins.debian.suite,
                                   "snapshot": pins.debian.snapshot},
        "packages": profile.SD_PACKAGES, "kernel_release": staged["kernel_release"],
        "firmware_from_backup": firmware_from.name, "firmware": staged["firmware"],
        "ids": ids.__dict__, "phone_key": fingerprint(phone_pub),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    say(f"done: {IMAGE.relative_to(config.REPO)} ({IMAGE.stat().st_size / 2**30:.2f} GiB, "
        f"sha256 {digest[:16]}...)")
    if debug:
        warn("DEBUG image: root shell for anyone on the USB serial line")
    return IMAGE


def read_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except FileNotFoundError:
        raise FlipError("no SD image yet: run `flip sd build`") from None


# --- writing a card ------------------------------------------------------------

@dataclass
class Card:
    path: str
    size: int
    tran: str
    hotplug: bool
    model: str
    mounts: list[str]
    holds_system: bool  # contains / or /boot of this computer


SD_SIZE_RANGE = (3_800_000_000, 130_000_000_000)  # "4 GB" .. "128 GB" cards
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}


def inspect_card(dev: str) -> Card:
    out = run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,TRAN,HOTPLUG,MODEL,MOUNTPOINTS",
               dev], capture=True, quiet=True).stdout
    node = json.loads(out)["blockdevices"][0]
    if node.get("type") != "disk":
        raise FlipError(f"{dev} is a {node.get('type')}, not a whole disk (use e.g. "
                        "/dev/mmcblk0, not /dev/mmcblk0p1)")
    mounts: list[str] = []

    def walk(n: dict) -> None:
        mounts.extend(m for m in (n.get("mountpoints") or []) if m)
        for c in n.get("children", []):
            walk(c)

    walk(node)
    return Card(dev, int(node["size"]), (node.get("tran") or "").lower(),
                node.get("hotplug") in (True, "1", 1),
                (node.get("model") or "").strip(), mounts,
                any(m in SYSTEM_MOUNTS or m.startswith("/boot") for m in mounts))


def check_card(card: Card, image_size: int) -> None:
    if card.model == profile.TOOLBOX_USB_MODEL:
        raise FlipError(f"{card.path} is the PHONE'S OWN STORAGE shared by the toolbox, not an "
                        "SD card. Writing it would wipe the phone. Refusing.")
    if card.holds_system:
        raise FlipError(f"{card.path} holds this computer's system ({', '.join(card.mounts)}). "
                        "Refusing.")
    if card.tran not in ("mmc", "usb"):
        raise FlipError(f"{card.path} is connected by '{card.tran or 'unknown'}', not an SD "
                        "slot or USB card reader. Refusing.")
    if card.tran == "usb" and not card.hotplug:
        raise FlipError(f"{card.path} is not hot-pluggable; doesn't look like a card reader")
    lo, hi = SD_SIZE_RANGE
    if not lo <= card.size <= hi:
        raise FlipError(f"{card.path} is {card.size / 1e9:.1f} GB; expected an SD card "
                        "between 4 and 128 GB")
    if card.size < image_size:
        raise FlipError(f"{card.path} ({card.size} bytes) is smaller than the image "
                        f"({image_size} bytes)")


def unmount_all(card: Card) -> None:
    out = run(["lsblk", "-J", "-o", "PATH,MOUNTPOINTS", card.path], capture=True,
              quiet=True).stdout
    for n in json.loads(out)["blockdevices"][0].get("children", []):
        if any(n.get("mountpoints") or []):
            run(["udisksctl", "unmount", "-b", n["path"]])


def write_card(dev: str, confirm) -> None:
    manifest = read_manifest()
    if not IMAGE.exists() or IMAGE.stat().st_size != manifest["size"]:
        raise FlipError("out/sd/sd.img is missing or incomplete: run `flip sd build`")
    if not Path(dev).is_block_device():
        raise FlipError(f"{dev} is not a block device (see lsblk)")
    card = inspect_card(dev)
    say(f"{dev}: {card.model or 'SD card'}, {card.size / 1e9:.1f} GB, via {card.tran}")
    check_card(card, manifest["size"])
    say("verifying the image ...")
    if sha256_file(IMAGE) != manifest["sha256"]:
        raise FlipError("sd.img doesn't match its manifest (changed or corrupted): rebuild it")
    if manifest["debug"]:
        warn("this is a DEBUG image (root shell on the USB serial line, no flipd)")
    if not confirm(f"ERASE everything on {dev} ({card.size / 1e9:.1f} GB) and write the "
                   "flip SD image?"):
        raise FlipError("aborted")
    if card.mounts:
        unmount_all(card)
    say(f"writing {IMAGE.name} -> {dev} (sudo is needed for raw device access)")
    run(["sudo", "dd", f"if={IMAGE}", f"of={dev}", "bs=4M", "conv=fsync", "oflag=direct",
         "status=progress"])
    say("reading it back to verify ...")
    run(["sudo", "blockdev", "--flushbufs", dev])  # compare the card, not the page cache
    result = run(["sudo", "cmp", "-n", str(manifest["size"]), IMAGE, dev], check=False,
                 capture=True)
    if result.returncode != 0:
        raise FlipError(f"the card doesn't read back what was written ({result.stdout.strip()}"
                        "). The card may be faulty or fake-capacity.")
    say("card written and verified. Eject it, put it in the phone and power on.")


# --- updating a card in place -----------------------------------------------------

def update_card(dev: str, confirm) -> None:
    """Replace kernel, device tree and modules on a flip card; keep everything else."""
    if not Path(dev).is_block_device():
        raise FlipError(f"{dev} is not a block device (see lsblk)")
    card = inspect_card(dev)
    check_card(card, 0)
    out = run(["lsblk", "-J", "-o", "PATH,PARTLABEL,PARTUUID", dev], capture=True,
              quiet=True).stdout
    parts = {c.get("partlabel"): c for c in json.loads(out)["blockdevices"][0].get("children", [])}
    if profile.SD_BOOT_LABEL not in parts or profile.SD_ROOT_LABEL not in parts:
        raise FlipError(f"{dev} is not a flip SD card (no {profile.SD_BOOT_LABEL}/"
                        f"{profile.SD_ROOT_LABEL} partitions)")
    release = build.OUT.kernel_release.read_text().strip()
    if not confirm(f"update the kernel ({release}), device tree and modules on {dev}?"):
        raise FlipError("aborted")
    if card.mounts:
        unmount_all(card)
    mnt = SD / "mnt"
    boot_mnt, root_mnt = mnt / "boot", mnt / "root"
    boot_mnt.mkdir(parents=True, exist_ok=True)
    root_mnt.mkdir(parents=True, exist_ok=True)
    say("mounting the card (sudo)")
    run(["sudo", "mount", parts[profile.SD_BOOT_LABEL]["path"], boot_mnt])
    try:
        run(["sudo", "mount", parts[profile.SD_ROOT_LABEL]["path"], root_mnt])
        try:
            run(["sudo", "install", "-m", "644", build.OUT.kernel_image, boot_mnt / "Image.gz"])
            run(["sudo", "install", "-m", "644", build.OUT.dtb,
                 boot_mnt / profile.KERNEL_DTB_NAME])
            dest = root_mnt / "usr/lib/modules" / release
            run(["sudo", "rm", "-rf", dest])
            run(["sudo", "cp", "-a", build.OUT.modules / "lib/modules" / release, dest])
            run(["sudo", "chown", "-R", "root:root", dest])
            run(["sync"])
        finally:
            run(["sudo", "umount", root_mnt])
    finally:
        run(["sudo", "umount", boot_mnt])
    say("card updated")

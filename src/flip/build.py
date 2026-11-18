"""Build the kernel, lk2nd, the toolbox and the signed images."""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from flip import config, fetch, images, initramfs, keys, lock, profile
from flip.fingerprint import Stamp, fingerprint
from flip.util import FlipError, run, say, sha256_file, warn

HOST_TOOLS = ["gcc", "make", "git", "bc", "flex", "bison", "perl", "dtc", "openssl", "depmod"]


@dataclass(frozen=True)
class Outputs:
    kernel: Path = config.OUT / "Image.gz-dtb"  # toolbox: kernel + appended dtb
    kernel_image: Path = config.OUT / "Image.gz"  # SD card: kernel and dtb separate
    dtb: Path = config.OUT / "qm215-nokia-weeknd.dtb"
    modules: Path = config.OUT / "modules"  # lib/modules/<release>/ for the SD card
    kernel_release: Path = config.OUT / "kernel-release"
    initramfs: Path = config.OUT / "initramfs.cpio.gz"
    boot: Path = config.OUT / "boot.img"
    dtbo: Path = config.OUT / "dtbo.img"
    lk2nd_head: Path = config.OUT / "lk2nd-head.img"
    recovery: Path = config.OUT / "recovery.img"  # lk2nd + toolbox
    pkmd: Path = config.OUT / "pkmd.bin"
    vbmeta: Path = config.OUT / "vbmeta.img"


OUT = Outputs()
LK2ND_BUILD = config.LK2ND_DIR / f"build-{profile.LK2ND_TARGET}"


def missing_tools() -> list[str]:
    needed = [config.cross_compile() + "gcc", config.toolchain_prefix() + "gcc", *HOST_TOOLS]
    return [t for t in needed if shutil.which(t) is None]


def check_tools() -> None:
    missing = missing_tools()
    if missing:
        raise FlipError(
            "missing build tools: " + " ".join(missing) + "\n"
            "   On Arch: sudo pacman -S --needed aarch64-linux-gnu-gcc arm-none-eabi-gcc \\\n"
            "            base-devel git bc flex bison perl dtc openssl"
        )


def step(n: int, name: str) -> None:
    say(f"[{n}] {name}")


# --- kernel ------------------------------------------------------------------

def kernel_fingerprint(pins: lock.Lock) -> str:
    return fingerprint(
        f"kernel-v2 {pins.git['linux'].commit} {config.cross_compile()}",
        [config.DEFCONFIG],
    )


def build_kernel(pins: lock.Lock, jobs: int) -> None:
    fetch.fetch_git(pins.git["linux"], config.LINUX_DIR)
    stamp = Stamp("kernel")
    fp = kernel_fingerprint(pins)
    outputs = [OUT.kernel, OUT.kernel_image, OUT.dtb, OUT.modules, OUT.kernel_release]
    if all(o.exists() for o in outputs) and stamp.matches(fp):
        say(f"OK       {OUT.kernel.name} (inputs unchanged)")
        return
    if OUT.kernel.exists():
        say("kernel inputs changed, rebuilding")
    shutil.copyfile(config.DEFCONFIG, config.LINUX_DIR / ".config")
    env = {"ARCH": "arm64", "CROSS_COMPILE": config.cross_compile()}
    # defconfig was saved with GCC 12.2; fill in options a newer compiler adds
    # with their defaults instead of prompting.
    run(["make", "olddefconfig"], cwd=config.LINUX_DIR, env=env)
    run(["make", f"-j{jobs}"], cwd=config.LINUX_DIR, env=env)
    image = (config.LINUX_DIR / "arch/arm64/boot/Image.gz").read_bytes()
    dtb = (config.LINUX_DIR / profile.KERNEL_DTB).read_bytes()
    OUT.kernel.parent.mkdir(parents=True, exist_ok=True)
    OUT.kernel.write_bytes(image + dtb)
    OUT.kernel_image.write_bytes(image)
    OUT.dtb.write_bytes(dtb)

    # Modules (Wi-Fi, audio, modem, zram, ...) for the SD card system.
    release = run(["make", "-s", "kernelrelease"], cwd=config.LINUX_DIR, env=env,
                  capture=True, quiet=True).stdout.strip()
    if OUT.modules.exists():
        shutil.rmtree(OUT.modules)
    run(["make", "modules_install", f"INSTALL_MOD_PATH={OUT.modules}", "INSTALL_MOD_STRIP=1"],
        cwd=config.LINUX_DIR, env=env)
    # Drop the build/source symlinks back into this checkout.
    for link in ("build", "source"):
        (OUT.modules / "lib/modules" / release / link).unlink(missing_ok=True)
    OUT.kernel_release.write_text(release + "\n")
    stamp.record(fp)


# --- lk2nd -------------------------------------------------------------------

def lk2nd_patches() -> list[Path]:
    return sorted((config.PATCHES / "lk2nd").glob("*.patch"))


def lk2nd_fingerprint(pins: lock.Lock) -> str:
    dts = sorted(p for p in config.LK2ND_DTS.iterdir() if p.is_file())
    return fingerprint(
        f"lk2nd-v1 {pins.git['lk2nd'].commit} {config.toolchain_prefix()} "
        + " ".join(profile.LK2ND_MAKE_ARGS),
        lk2nd_patches() + dts,
    )


def build_lk2nd(pins: lock.Lock, jobs: int) -> Path:
    src = config.LK2ND_DIR
    fetch.fetch_git(pins.git["lk2nd"], src)
    out = LK2ND_BUILD / "lk.bin-dtb"
    stamp = Stamp("lk2nd")
    fp = lk2nd_fingerprint(pins)
    if out.exists() and stamp.matches(fp):
        say(f"OK       lk.bin-dtb (inputs unchanged)")
        return out
    if out.exists():
        say("lk2nd inputs changed, rebuilding")

    # Start from the pristine pinned source, so the result depends only on the
    # pin and files in this repo. Local edits in lk2nd/ are discarded: put
    # changes in patches/lk2nd/ instead.
    dts_link = src / "lk2nd/device/dts/msm8952"
    if dts_link.is_symlink():
        dts_link.unlink()
    run(["git", "checkout", "-q", "--", "."], cwd=src)
    for patch in lk2nd_patches():
        say(f"applying {patch.name}")
        run(["git", "apply", patch], cwd=src)
    shutil.rmtree(dts_link)
    dts_link.symlink_to(Path("../../../..") / config.LK2ND_DTS.name)

    # Clean build: compile-time options don't reliably trigger rebuilds in lk.
    if LK2ND_BUILD.exists():
        shutil.rmtree(LK2ND_BUILD)
    run(["make", f"-j{jobs}", profile.LK2ND_TARGET, *profile.LK2ND_MAKE_ARGS], cwd=src,
        env={"TOOLCHAIN_PREFIX": config.toolchain_prefix()})
    stamp.record(fp)
    return out


# --- toolbox -----------------------------------------------------------------

def toolbox_entries(busybox: Path) -> list[initramfs.Entry]:
    # rootfs/sbin is generated by the old shell build; ignore it if present.
    entries = initramfs.tree_entries(config.ROOTFS, exclude={"sbin"})
    entries.append(initramfs.Entry("sbin", stat.S_IFDIR | 0o755))
    entries.append(initramfs.Entry("sbin/busybox", stat.S_IFREG | 0o755, busybox.read_bytes()))
    for applet in profile.BUSYBOX_APPLETS:
        entries.append(initramfs.Entry(f"sbin/{applet}", stat.S_IFLNK | 0o777, b"busybox"))
    return entries


def build_toolbox(pins: lock.Lock) -> None:
    busybox = fetch.fetch_file(pins.file["busybox"])
    OUT.initramfs.write_bytes(initramfs.build(toolbox_entries(busybox)))
    images.mkbootimg(OUT.boot, OUT.kernel, profile.BOOT_CMDLINE, ramdisk=OUT.initramfs)


def build_dtbo() -> None:
    dtbs = []
    for name in profile.OVERLAYS:
        dtb = config.OUT / f"overlay-{name}.dtb"
        run(["dtc", "-q", "-o", dtb, config.REPO / f"overlay-{name}.dts"])
        dtbs.append(dtb)
    images.mkdtboimg(OUT.dtbo, dtbs)


def build_recovery(lk_bin: Path) -> None:
    images.mkbootimg(OUT.lk2nd_head, lk_bin, "lk2nd", header_version=1,
                     recovery_dtbo=OUT.dtbo)
    image = images.assemble_recovery(OUT.lk2nd_head.read_bytes(), OUT.boot.read_bytes())
    OUT.recovery.write_bytes(image)
    say(f"recovery image: lk2nd {OUT.lk2nd_head.stat().st_size}/{profile.LK2ND_SLOT} bytes, "
        f"total {len(image)}/{profile.RECOVERY_SIZE} bytes")


# --- vbmeta ------------------------------------------------------------------

def stock_vbmeta(pins: lock.Lock, override: Path | None) -> Path:
    author = pins.file["author-vbmeta"]
    if override:
        if not override.is_file():
            raise FlipError(f"stock vbmeta {override} does not exist")
        path = override
    elif config.own_vbmeta().is_file():
        path = config.own_vbmeta()
    else:
        path = fetch.fetch_file(author)
        warn("using the original author's stock vbmeta (firmware 00.2238.20.13). "
             "Run `flip backup adopt-vbmeta` after a backup to use your own.")
        return path
    sha = sha256_file(path)
    same = " (identical to the author's copy)" if sha == author.sha256 else ""
    say(f"stock vbmeta: {path}{same}")
    return path


def build_vbmeta(pins: lock.Lock, stock_override: Path | None) -> None:
    stock = stock_vbmeta(pins, stock_override)
    key = keys.ensure_key()
    pkmd_sha = keys.derive_pkmd(key, OUT.pkmd)
    say(f"signing key pkmd sha256: {pkmd_sha}")
    flashed = keys.flashed_pkmd()
    if flashed and flashed != pkmd_sha:
        warn("this signing key differs from the one last flashed to the phone. "
             "`flip flash` (all) installs it; until then the phone rejects these images.")
    images.avb_make_vbmeta(OUT.vbmeta, key, OUT.pkmd, stock)


# --- all ---------------------------------------------------------------------

def build_all(jobs: int, stock_override: Path | None = None) -> None:
    check_tools()
    pins = lock.load()
    config.ensure_config_dir()  # before looking up the key / own vbmeta in it
    config.OUT.mkdir(exist_ok=True)
    step(1, "kernel")
    build_kernel(pins, jobs)
    step(2, "lk2nd")
    lk_bin = build_lk2nd(pins, jobs)
    step(3, "toolbox (initramfs + boot image)")
    build_toolbox(pins)
    step(4, "device tree overlays")
    build_dtbo()
    step(5, "recovery image")
    build_recovery(lk_bin)
    step(6, "vbmeta")
    build_vbmeta(pins, stock_override)
    say("done. Outputs in out/: recovery.img, vbmeta.img, pkmd.bin")

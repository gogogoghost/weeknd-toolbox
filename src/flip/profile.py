"""Fixed facts about the Nokia 2780 Flip and the toolbox image layout."""

# fastboot "product" as reported by each bootloader
PRODUCT_STOCK = "WND"
PRODUCT_LK2ND = "lk2nd-msm8952"

# The toolbox's USB mass storage gadget (rootfs/init)
TOOLBOX_USB_ID = ("05c6", "9092")
TOOLBOX_USB_MODEL = "File-Stor Gadget"

# Partition sizes (from `fastboot getvar all` on the reference phone)
RECOVERY_SIZE = 0x3000000  # 48 MiB
VBMETA_SIZE = 0x10000  # 64 KiB

# lk2nd occupies the first 512 KiB of recovery (LK2ND_PARTITION_SIZE); the
# toolbox boot image follows at that offset.
LK2ND_SLOT = 512 * 1024

# Whole eMMC is 3623878656 bytes; accept a range so similar units still work.
EMMC_SIZE_RANGE = (3_000_000_000, 4_300_000_000)

KERNEL_DTB = "arch/arm64/boot/dts/qcom/qm215-nokia-weeknd.dtb"
KERNEL_DTB_NAME = "qm215-nokia-weeknd.dtb"
BOOT_CMDLINE = "console=tty1 rw loglevel=7 fbcon=font:10x18"

LK2ND_TARGET = "lk2nd-msm8952"
LK2ND_MAKE_ARGS = [
    "LK2ND_PARTITION_BASE=recovery",
    # patches/lk2nd/0001-*: boot the toolbox when no extlinux system is found
    "LK2ND_FALLBACK_RECOVERY=1",
]

# Device tree overlays in the recovery_dtbo section. Kept because that image
# layout is the one proven to boot from recovery on this phone.
OVERLAYS = ["wnd1", "wnd2e", "wnd2"]

BUSYBOX_APPLETS = [
    "sh", "ls", "mkdir", "ln", "cat", "dd", "grep", "sed", "clear", "resize",
    "fold", "tail", "head", "sleep", "dmesg", "mount", "umount", "sync",
    "reboot", "poweroff",
]


# --- SD card system (flip sd) -------------------------------------------------

SD_BOOT_LABEL = "flipboot"  # ext2: kernel, device tree, extlinux.conf (read by lk2nd)
SD_ROOT_LABEL = "fliproot"  # ext4: Debian
SD_BOOT_SIZE = 256 << 20
SD_CMDLINE = "console=tty1 fbcon=font:10x18 loglevel=4"

# Debian packages on top of the minimal base (no recommends). No SSH server, no
# sudo: administration is flipd over USB only.
SD_PACKAGES = [
    # system
    "systemd-sysv", "udev", "dbus", "libpam-systemd", "kmod", "systemd-repart",
    "systemd-zram-generator", "procps", "less", "nano", "e2fsprogs", "fdisk", "kbd",
    "tzdata", "ca-certificates", "unattended-upgrades",
    # flipd
    "python3", "python3-cryptography",
    # networking (outgoing only; nftables drops everything incoming)
    "network-manager", "wpasupplicant", "iw", "wireless-regdb", "rfkill", "nftables",
    "iputils-ping",
    # audio
    "alsa-utils", "alsa-ucm-conf",
    # modem (installed but switched off until it's brought up deliberately)
    "rmtfs", "tqftpserv", "qrtr-tools", "modemmanager",
]

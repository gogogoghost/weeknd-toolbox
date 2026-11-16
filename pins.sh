# SPDX-License-Identifier: MIT
#
# Every external input of the build, pinned by commit SHA or SHA-256.
# Sourced by build.sh, build-linux.sh and build-lk2nd.sh.
#
# The *_REPO variables may be overridden from the environment to fetch from
# your own mirror; the pins guarantee the same content either way.

# Linux kernel (originally branch wip/qm215/6.3-rc7 of affenull2345/linux)
LINUX_REPO="${LINUX_REPO:-https://github.com/affenull2345/linux}"
LINUX_COMMIT="c438fd63372aea125a063c5c3500f03a2ce552c5"

# lk2nd, official release tag 23.1 (the previously used branch
# experimental-tmp3 has been deleted upstream)
LK2ND_REPO="${LK2ND_REPO:-https://github.com/msm8916-mainline/lk2nd}"
LK2ND_COMMIT="e9c8b21735c13dd7ace808b1e35451638f0a0c6d"

# Prebuilt busybox 1.31.0, 32-bit ARM, static (no upstream checksum exists;
# pinned on first use 2026-11-11)
BUSYBOX_URL="https://busybox.net/downloads/binaries/1.31.0-defconfig-multiarch-musl/busybox-armv8l"
BUSYBOX_SHA256="141adb1b625a6f44c4b114f76b4387b4ea4f7ab802b88eb40e0d2f6adcccb1c3"

# Stock vbmeta published by the original author (from another device, security
# patch 2020-05-05). Only a bootstrap fallback: set STOCK_VBMETA to the path of
# a vbmeta dump from YOUR phone to use that instead.
AUTHOR_VBMETA_URL="https://storage.abscue.de/private/zImage/wnd-vbmeta.img"
AUTHOR_VBMETA_SHA256="9354c9a8c6f6df885d11ea30f884d90c290128c7477caf6c3d00ee9cbaa7028b"

# Cross-compilers (Arch package names: aarch64-linux-gnu-gcc, arm-none-eabi-gcc).
# Override from the environment to use a different toolchain.
CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}"   # Linux kernel (arm64)
TOOLCHAIN_PREFIX="${TOOLCHAIN_PREFIX:-arm-none-eabi-}"  # lk2nd (32-bit ARM)
export CROSS_COMPILE TOOLCHAIN_PREFIX

# require_tool <command> <what it is for>
require_tool() {
	if ! command -v "$1" >/dev/null 2>&1; then
		echo ">> ERROR: '$1' not found (needed for $2)." >&2
		echo ">> Install the cross toolchain, or set CROSS_COMPILE / TOOLCHAIN_PREFIX." >&2
		exit 1
	fi
}

# AVB signing key (private!). Kept outside the repo so `clean.sh` and git can't
# touch it. The phone trusts the public half of this key (flashed to
# avb_custom_key), so losing it means re-running `fastboot oem sudo` and
# re-flashing avb_custom_key. Back it up somewhere private.
KEY_DIR="${WEEKND_KEY_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/weeknd-toolbox}"
AVB_KEY="$KEY_DIR/avb-key.pem"
# SHA-256 of the pkmd.bin last flashed to the phone (written by flash.sh)
FLASHED_PKMD_RECORD="$KEY_DIR/flashed-pkmd.sha256"

# Stock vbmeta dumped from YOUR phone (the vbmetabak partition), used by
# default when present. For the reference phone this is byte-identical to the
# author's copy (AUTHOR_VBMETA_SHA256), which remains only as a fallback for
# machines without your backup.
OWN_VBMETA="$KEY_DIR/stock-vbmeta.img"

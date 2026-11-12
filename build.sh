#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT
set -e
cd "$(dirname "$0")"
. ./pins.sh

# Check all host tools up front, instead of failing partway through the build.
missing=""
for tool in "${CROSS_COMPILE}gcc" "${TOOLCHAIN_PREFIX}gcc" \
	gcc make git bc flex bison perl python3 dtc cpio gzip openssl sha256sum; do
	command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
	missing="$missing curl"
fi
if [ -n "$missing" ]; then
	echo ">> ERROR: missing build tools:$missing" >&2
	echo ">> On Arch: sudo pacman -S --needed aarch64-linux-gnu-gcc arm-none-eabi-gcc \\" >&2
	echo ">>          base-devel git bc flex bison perl python dtc cpio openssl curl" >&2
	exit 1
fi

step=0

step() {
	step=$((step+1))
	echo ">> [$step] $*"
}

needs() {
	if [ -f "$1" ]; then
		echo ">> OK       $1"
		return 1
	else
		echo ">> creating $1"
		return 0
	fi
}

sha256_of() {
	sha256sum "$1" | cut -d' ' -f1
}

# fetch_pinned <url> <dest> <sha256>
# Downloads <url> to <dest> unless already present, and always verifies the
# SHA-256 of <dest>. Aborts the build on mismatch.
fetch_pinned() {
	if [ ! -f "$2" ]; then
		echo ">> fetching $1"
		rm -f "$2.partial"
		if command -v curl >/dev/null 2>&1; then
			curl -fL --retry 3 -o "$2.partial" "$1" || fetch_failed=1
		elif command -v wget >/dev/null 2>&1; then
			wget -O"$2.partial" "$1" || fetch_failed=1
		else
			echo ">> ERROR: need curl or wget to download $1" >&2
			exit 1
		fi
		if [ -n "$fetch_failed" ]; then
			rm -f "$2.partial"
			echo ">> ERROR: download failed: $1" >&2
			exit 1
		fi
		mv "$2.partial" "$2" || exit 1
	fi
	actual=$(sha256_of "$2")
	if [ "$actual" != "$3" ]; then
		echo ">> ERROR: SHA-256 mismatch for $2" >&2
		echo ">>   expected $3" >&2
		echo ">>   actual   $actual" >&2
		echo ">> Delete $2 to re-download, or update the pin if intended." >&2
		exit 1
	fi
	echo ">> OK       $2 (sha256 verified)"
}

step linux

# Always verify the checkout matches the pin, even if outputs already exist.
[ ! -d linux ] || ./fetch-git.sh linux "$LINUX_REPO" "$LINUX_COMMIT"

if needs Image.gz-dtb; then
	./build-linux.sh
fi

step lk2nd

[ ! -d lk2nd ] || ./fetch-git.sh lk2nd "$LK2ND_REPO" "$LK2ND_COMMIT"

if needs lk2nd/build-lk2nd-msm8952/lk.bin-dtb; then
	./build-lk2nd.sh
fi

step busybox

mkdir -p rootfs/sbin

fetch_pinned "$BUSYBOX_URL" rootfs/sbin/busybox "$BUSYBOX_SHA256"

for cmd in \
	sh \
	ls \
	mkdir \
	ln \
	cat \
	dd \
	grep \
	sed \
	clear \
	resize \
	fold \
	tail \
	head \
	sleep \
	dmesg \
	mount \
	umount \
	sync \
	reboot \
	poweroff
do
	ln -sf busybox rootfs/sbin/$cmd
done

chmod +x rootfs/sbin/busybox

step initramfs

(cd rootfs && find . | cpio -o -H newc -R 0:0) | gzip > initramfs.cpio.gz

step boot image

python3 mkbootimg.py -o boot.img \
	--kernel Image.gz-dtb \
	--ramdisk initramfs.cpio.gz \
	--cmdline 'console=tty1 rw loglevel=7 fbcon=font:10x18'

step device tree overlay

overlays=""
for ovname in wnd1 wnd2e wnd2; do
	if needs overlay-$ovname.dtb; then
		dtc -o overlay-$ovname.dtb overlay-$ovname.dts
	fi
	overlays="$overlays overlay-$ovname.dtb"
done

if needs dtbo.img; then
	python3 mkdtboimg.py create dtbo.img $overlays
fi

step lk2nd image

python3 mkbootimg.py -o lk2nd.img \
	--kernel lk2nd/build-lk2nd-msm8952/lk.bin-dtb \
	--cmdline 'lk2nd' \
	--header_version 1 \
	--recovery_dtbo dtbo.img

dd if=boot.img of=lk2nd.img seek=512 bs=1024

step vbmeta image

if [ -n "$STOCK_VBMETA" ]; then
	if [ ! -f "$STOCK_VBMETA" ]; then
		echo ">> ERROR: STOCK_VBMETA='$STOCK_VBMETA' does not exist" >&2
		exit 1
	fi
	stock_vbmeta="$STOCK_VBMETA"
	echo ">> using your own vbmeta dump: $stock_vbmeta"
else
	fetch_pinned "$AUTHOR_VBMETA_URL" stock_vbmeta.img "$AUTHOR_VBMETA_SHA256"
	stock_vbmeta=stock_vbmeta.img
	echo ">> WARNING: using the original author's vbmeta (another device's"
	echo ">>          2020-05-05 firmware). Its boot/dtbo hashes may not match"
	echo ">>          your phone. Set STOCK_VBMETA to your own dump when you have it."
fi
stock_vbmeta_sha256=$(sha256_of "$stock_vbmeta")
echo ">> stock vbmeta sha256: $stock_vbmeta_sha256"

# Rebuild vbmeta.img whenever the stock vbmeta it was built from changes
# (e.g. switching from the author's file to your own dump).
if [ -f vbmeta.img ] && [ "$(cat vbmeta.img.src 2>/dev/null)" != "$stock_vbmeta_sha256" ]; then
	echo ">> stock vbmeta changed, rebuilding vbmeta.img"
	rm -f vbmeta.img
fi

if needs vbmeta.img; then
	# Reuse an existing signing key; only generate one if none exists.
	if [ ! -f key.pem ]; then
		openssl genrsa 2048 > key.pem
	fi
	python3 avbtool.py extract_public_key --key key.pem --output pkmd.bin
	python3 avbtool.py make_vbmeta_image \
		--padding_size 4096 \
		--algorithm SHA512_RSA2048 \
		--key key.pem \
		--public_key_metadata pkmd.bin \
		--output vbmeta.img \
		--include_descriptors_from_image "$stock_vbmeta" \
		--set_hashtree_disabled_flag
	echo "$stock_vbmeta_sha256" > vbmeta.img.src
fi

step "Done!"

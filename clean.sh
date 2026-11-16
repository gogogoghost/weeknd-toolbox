#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT
#
# Never deletes the signing key (see KEY_DIR in pins.sh): the phone trusts it.

rm -rf \
	boot.img \
	*.dtb \
	dtbo.img \
	lk2nd.img \
	vbmeta.img vbmeta.img.src \
	initramfs.cpio.gz \
	pkmd.bin \
	Image.gz-dtb Image.gz-dtb.src lk2nd.src

if [ "$1" = "--all" ]; then
	rm -rf linux lk2nd linux.partial lk2nd.partial rootfs/sbin stock_vbmeta.img stock_vbmeta.img.partial
fi

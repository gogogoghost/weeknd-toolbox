#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT

rm -rf \
	boot.img \
	*.dtb \
	dtbo.img \
	lk2nd.img \
	vbmeta.img vbmeta.img.src \
	initramfs.cpio.gz \
	key.pem pkmd.bin \
	Image.gz-dtb

if [ "$1" = "--all" ]; then
	rm -rf linux lk2nd linux.partial lk2nd.partial rootfs/sbin stock_vbmeta.img stock_vbmeta.img.partial
fi

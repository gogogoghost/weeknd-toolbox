#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT
set -e
cd "$(dirname "$0")"

. ./pins.sh
require_tool "${CROSS_COMPILE}gcc" "the Linux kernel"

./fetch-git.sh linux "$LINUX_REPO" "$LINUX_COMMIT"

cd linux
cp ../defconfig .config
# defconfig was saved with GCC 12.2. A different compiler exposes options it
# has no answer for, which would make `make` stop and ask interactively.
# olddefconfig fills those in with their defaults instead.
make ARCH=arm64 olddefconfig
make ARCH=arm64 ${JOBS:+-j$JOBS}
# make ARCH=arm64 modules_install INSTALL_MOD_PATH=../rootfs
cat arch/arm64/boot/Image.gz arch/arm64/boot/dts/qcom/qm215-nokia-weeknd.dtb > ../Image.gz-dtb

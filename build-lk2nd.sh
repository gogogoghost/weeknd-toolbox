#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT
set -e
cd "$(dirname "$0")"

. ./pins.sh
require_tool "${TOOLCHAIN_PREFIX}gcc" "lk2nd"
require_tool dtc "lk2nd device trees"

./fetch-git.sh lk2nd "$LK2ND_REPO" "$LK2ND_COMMIT"

cd lk2nd

# Start from the pristine pinned source every time, so the result only depends
# on the pin and the files in this repo. NOTE: this discards local edits made
# directly in lk2nd/ - put changes in patches/lk2nd/ instead.
if [ -L lk2nd/device/dts/msm8952 ]; then
	rm lk2nd/device/dts/msm8952
fi
git checkout -q -- .

for patch in ../patches/lk2nd/*.patch; do
	[ -e "$patch" ] || continue
	echo ">> applying $(basename "$patch")"
	git apply "$patch"
done

rm -rf lk2nd/device/dts/msm8952
ln -s ../../../../lk2nd-dts-msm8952 lk2nd/device/dts/msm8952

# Clean build: compile-time options don't reliably trigger rebuilds in lk.
rm -rf build-lk2nd-msm8952
# LK2ND_FALLBACK_RECOVERY (patches/lk2nd/0001-*): if no extlinux system is
# found, boot the toolbox in the recovery partition instead of stock "boot".
make lk2nd-msm8952 LK2ND_PARTITION_BASE=recovery LK2ND_FALLBACK_RECOVERY=1

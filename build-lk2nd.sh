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
rm -rf lk2nd/device/dts/msm8952
ln -s ../../../../lk2nd-dts-msm8952 lk2nd/device/dts/msm8952
make lk2nd-msm8952 LK2ND_PARTITION_BASE=recovery

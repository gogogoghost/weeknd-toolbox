#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Usage: ./backup.sh /dev/sdX
#
# Full backup of the phone's eMMC, exposed by the hacking toolbox via
# "USB storage" -> "Whole eMMC" (read-only mode).
#
# Creates backups/<date>-<time>/ containing:
#   emmc.img          raw image of the whole eMMC
#   parts/<name>.img  every GPT partition, extracted from emmc.img
#   SHA256SUMS        hashes of all of the above
#   gpt.json          partition table
#
# Only the raw read of the device uses sudo; everything else runs as you and
# only operates on files in backups/.
set -e
cd "$(dirname "$0")"

dev="$1"
if [ -z "$dev" ] || [ ! -b "$dev" ]; then
	echo "usage: $0 /dev/sdX   (the phone's exposed eMMC, see lsblk)" >&2
	exit 2
fi

# Safety checks: refuse anything that doesn't look like the phone's eMMC.
model=$(lsblk -dno MODEL "$dev" | sed 's/ *$//')
vendor=$(lsblk -dno VENDOR "$dev" | sed 's/ *$//')
ro=$(lsblk -dno RO "$dev" | tr -d ' ')
size=$(lsblk -dbno SIZE "$dev" | tr -d ' ')
echo ">> $dev: vendor='$vendor' model='$model' size=$size ro=$ro"
# The 2780's eMMC is 3623878656 bytes (3456 MiB) per lk2nd's log.
if [ "$size" -lt 3000000000 ] || [ "$size" -gt 4300000000 ]; then
	echo ">> ERROR: size doesn't match the phone's ~3.4 GiB eMMC. Wrong device?" >&2
	exit 1
fi
if [ "$ro" != 1 ]; then
	echo ">> ERROR: $dev is not read-only. Switch the toolbox to read-only" >&2
	echo ">>        mode (USB storage -> 7) before taking a backup." >&2
	exit 1
fi
if lsblk -no MOUNTPOINTS "$dev" | grep -q .; then
	echo ">> ERROR: a partition of $dev is mounted. Unmount it first." >&2
	exit 1
fi

out="backups/$(date +%F-%H%M%S)"
mkdir -p "$out/parts"
echo ">> reading $dev -> $out/emmc.img (sudo needed for raw device access)"
sudo dd if="$dev" bs=4M iflag=direct status=progress > "$out/emmc.img"

actual=$(stat -c%s "$out/emmc.img")
if [ "$actual" != "$size" ]; then
	echo ">> ERROR: image is $actual bytes, expected $size. Incomplete read." >&2
	exit 1
fi

echo ">> extracting partitions"
sfdisk --json "$out/emmc.img" > "$out/gpt.json"
python3 -I - "$out" <<'EOF'
import json, sys, os
out = sys.argv[1]
table = json.load(open(os.path.join(out, "gpt.json")))["partitiontable"]
sector = table.get("sectorsize", 512)
seen = set()
with open(os.path.join(out, "emmc.img"), "rb") as img:
    for p in table["partitions"]:
        name = p.get("name") or os.path.basename(p["node"])
        if name in seen:
            raise SystemExit(f"duplicate partition name {name!r}")
        seen.add(name)
        img.seek(p["start"] * sector)
        remaining = p["size"] * sector
        with open(os.path.join(out, "parts", name + ".img"), "wb") as f:
            while remaining:
                chunk = img.read(min(remaining, 4 << 20))
                if not chunk:
                    raise SystemExit(f"short read in partition {name}")
                f.write(chunk)
                remaining -= len(chunk)
        print(f"   {name:14s} {p['size'] * sector:>12d} bytes")
EOF

echo ">> hashing"
(cd "$out" && sha256sum emmc.img parts/*.img > SHA256SUMS)
chmod -R a-w "$out"
echo ">> done: $out (files made read-only to protect them)"

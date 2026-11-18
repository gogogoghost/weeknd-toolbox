#!/bin/sh
# Runs inside the new system (as root, under emulation) during `flip sd build`.
set -eu
DEBUG="${1:-0}"

# Files copied in from the build machine arrive owned by root with standard
# modes (sd.py root_owned_tar); only the flip-link keys need tightening.
chmod 700 /etc/flipd
chmod 600 /etc/flipd/*

mkdir -p /boot  # the boot partition is mounted here

echo flip > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\tflip\n::1\t\tlocalhost ip6-localhost ip6-loopback\n' > /etc/hosts
ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime

# Nobody logs in with a password: admin is flipd (root), and the agent runs as
# the unprivileged 'phone' user (no sudo installed).
groups=audio,video,input
getent group render >/dev/null && groups="$groups,render"
useradd --create-home --shell /bin/bash --uid 1000 --groups "$groups" phone
passwd --lock root
passwd --lock phone

# Live mirrors replace the build snapshot (see /etc/apt/sources.list.d/).
rm -f /etc/apt/sources.list

KVER="$(ls /usr/lib/modules)"
depmod "$KVER"

systemctl enable flip-gadget.service nftables.service NetworkManager.service
mkdir -p /etc/systemd/system/sysinit.target.wants
ln -sf /usr/lib/systemd/system/systemd-repart.service /etc/systemd/system/sysinit.target.wants/
if [ "$DEBUG" = 1 ]; then
	systemctl enable serial-getty@ttyGS0.service
else
	systemctl enable flipd.service
fi

# Modem helpers stay off until the modem is brought up deliberately: rmtfs
# writes the modem's own partitions (modemst1/2) on the phone's eMMC.
for unit in rmtfs.service tqftpserv.service ModemManager.service; do
	systemctl disable "$unit" 2>/dev/null || true
done

apt-get clean
rm -rf /var/lib/apt/lists/*

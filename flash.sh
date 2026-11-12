#!/bin/sh
# Author: Affe Null
# SPDX-License-Identifier: MIT

# Abort on the first failed command so that, e.g., a failed `fastboot oem sudo`
# does not lead to flashing a custom vbmeta/recovery on an unprepared device.
set -e

cd "$(dirname "$0")"

if [ ! -f pkmd.bin ] || [ ! -f vbmeta.img ] || [ ! -f lk2nd.img ]; then
	echo 'Please run build.sh first.'
	exit 1
fi

if [ "$(fastboot devices)" = "" ]; then
	echo
	echo '[!] Please power off the device and plug it in with the volume-down key pressed'
	echo
fi

fastboot oem sudo
fastboot flash avb_custom_key pkmd.bin
fastboot flash vbmeta vbmeta.img
fastboot flash recovery lk2nd.img
echo
echo '[!] Please hold the volume-up key... Will reboot in 5 seconds...'
sleep 5
fastboot reboot
sleep 3
echo
echo '[!] Please release the volume-up key now'
echo '[!] Please start holding it again after the "custom operating system" screen disappears'
echo

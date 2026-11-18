#!/bin/sh
# Present the phone to the computer as a USB serial device named "flip-link".
# flipd answers on the phone side (/dev/ttyGS0). No USB networking.
set -eu
G=/sys/kernel/config/usb_gadget/flip
mountpoint -q /sys/kernel/config || mount -t configfs configfs /sys/kernel/config
[ -e "$G/UDC" ] && [ -n "$(cat "$G/UDC")" ] && exit 0

i=0
while [ -z "$(ls /sys/class/udc 2>/dev/null)" ]; do
	i=$((i + 1))
	[ $i -gt 50 ] && { echo "no USB device controller found" >&2; exit 1; }
	sleep 0.2
done

mkdir -p "$G"
cd "$G"
echo 0x1d6b > idVendor   # Linux Foundation
echo 0x0104 > idProduct  # Multifunction Composite Gadget
echo 0x0200 > bcdUSB
mkdir -p strings/0x409
echo "flip" > strings/0x409/manufacturer
echo "flip-link" > strings/0x409/product
cut -c1-16 /etc/machine-id > strings/0x409/serialnumber

mkdir -p configs/c.1/strings/0x409
echo "flip-link" > configs/c.1/strings/0x409/configuration
echo 100 > configs/c.1/MaxPower
mkdir -p functions/acm.usb0
[ -e configs/c.1/acm.usb0 ] || ln -s functions/acm.usb0 configs/c.1/

ls /sys/class/udc | head -n1 > UDC

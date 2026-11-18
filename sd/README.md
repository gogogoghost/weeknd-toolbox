# SD card system files

`flip sd build` builds a Debian system for the SD card and adds these files:

- `overlay/`: copied into every image.
- `debug/`: copied only with `flip sd build --debug`. It puts a root login on the
  USB serial port instead of flipd. **Never use a debug card day to day**: anyone
  who plugs in a cable gets a root shell.
- `setup.sh`: runs inside the new system (under emulation) to finish the
  setup: users, services, permissions.

`flip sd build` generates a few files itself, because they hold keys or IDs:
`/etc/fstab`, `/boot/extlinux/extlinux.conf`, `/etc/flipd/identity.pem` and
`/etc/flipd/trusted-hosts`. It also adds the kernel modules and your phone's
firmware.

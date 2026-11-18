# flip: custom Linux for the Nokia 2780 Flip

Based on [weeknd-toolbox](https://git.abscue.de/affe_null/weeknd-toolbox/) by
Affe Null.

`flip` builds and installs a small Linux "hacking toolbox" in the phone's
recovery partition, using [lk2nd](https://github.com/msm8916-mainline/lk2nd)
and a close-to-mainline kernel. The toolbox exposes the internal storage over
USB (for backups and modifications), and lk2nd can boot a full Linux system
from an SD card. It does not work on the Nokia 2760 Flip (no
`fastboot oem sudo`).

## Setup

On Arch Linux:

```sh
sudo pacman -S --needed aarch64-linux-gnu-gcc arm-none-eabi-gcc \
    base-devel git bc flex bison perl dtc openssl android-tools uv
# for the SD card system:
sudo pacman -S --needed apt debian-archive-keyring mtools e2fsprogs \
    qemu-user-static qemu-user-static-binfmt
uv sync                # creates .venv with flip and its dependencies
uv run flip doctor     # checks tools, pins, key and phone connection
```

Run commands as `uv run flip ...`, or install it on your PATH with
`uv tool install --editable .`.

## Commands

| Command | What it does |
|---|---|
| `flip build` | Build the kernel, lk2nd, the toolbox and the signed images into `out/` |
| `flip flash` | Flash `recovery` (lk2nd + toolbox) |
| `flip flash all` | Also install your signing key and vbmeta (first install) |
| `flip backup create /dev/sdX` | Full eMMC backup via the toolbox (read-only mode) |
| `flip backup list` / `verify` | List backups / re-check their SHA-256 hashes |
| `flip backup adopt-vbmeta` | Build from your own phone's stock vbmeta |
| `flip keys show` / `export DIR` | Key fingerprint and phone trust / private copy |
| `flip device status` / `info` / `log` | Phone mode / `getvar all` / lk2nd boot log |
| `flip sd build [--debug]` | Build the SD card image `out/sd/sd.img` (Debian 13) |
| `flip sd write /dev/mmcblk0` | Erase a card, write the image, read it back to verify |
| `flip sd update /dev/mmcblk0` | New kernel and modules on a card, keeping its data |
| `flip phone shell` / `run CMD` | Root shell / command on the phone over USB |
| `flip phone push` / `pull` | Copy files to / from the phone (checksum verified) |
| `flip phone trust list` / `add` / `remove` | Which computers the phone accepts |
| `flip phone setup-host` | One-time udev rule so you can open the phone's port |
| `flip clean [--all]` | Delete build outputs (never backups or keys) |

## First install

1. `flip build`
2. Power off the phone, hold volume-down and plug in USB (stock fastboot).
3. `flip flash all`. This runs `fastboot oem sudo`, installs your signing key
   (`avb_custom_key`), a re-signed vbmeta, and lk2nd + the toolbox in
   recovery. The stock recovery is overwritten permanently.
4. Boot the toolbox (below), choose USB storage -> 5 (Whole eMMC), and run
   `flip backup create /dev/sdX` (see `lsblk`). Keep the backup private and
   keep a second copy elsewhere: `modemst1/2`, `fsg` and `persist` hold the
   IMEI and radio calibration and can't be recreated.
5. `flip backup adopt-vbmeta` so future builds use your own stock vbmeta.

`flip flash` refuses to run unless the phone is in **stock** fastboot. In
lk2nd's fastboot, "recovery" means only the part after lk2nd, so flashing
there would put the image in the wrong place.

## Booting

- **Toolbox:** hold volume-up while powering on and release it when the
  "custom operating system" warning appears.
- **Linux from SD card:** same keys. lk2nd first looks for an ext2 file
  system with `extlinux/extlinux.conf` on any partition of 16 MiB or more,
  including an SD card, and boots it if found, otherwise the toolbox.
- **Rescue:** hold volume-up again as soon as the warning disappears to skip
  that search and always get the toolbox.
- **KaiOS:** power on without holding any keys.

## Linux on the SD card

`flip sd build` makes a Debian 13 system for the SD card, entirely without root
on your computer: pinned Debian packages (a dated snapshot), our kernel and its
modules, the phone's firmware (taken from your newest backup), and `flipd`.
The image holds your firmware and the phone's identity key: like `backups/`,
keep it private.

```sh
flip build                     # kernel + modules
flip sd build                  # out/sd/sd.img
flip sd write /dev/mmcblk0     # see lsblk; refuses anything that isn't an SD card
flip phone setup-host          # once per computer
```

Put the card in the phone and power on holding volume-up (as for the toolbox);
lk2nd finds the card and boots it. On first boot the root
partition grows to fill the card.

**Access.** There is no SSH and nothing on the phone accepts network
connections (nftables drops all incoming traffic). Administration (a shell,
installing software, protected settings) works only over the USB cable,
through `flipd`, and only for computers in the phone's trusted list:

- The phone appears on your computer as a USB serial device called
  `flip-link`. `flip phone ...` connects to it.
- Every session starts with a handshake: the computer and the phone each prove
  their identity with an Ed25519 key, then the session is encrypted
  (X25519 + ChaCha20-Poly1305). An unknown computer gets no shell and no login
  prompt.
- `flip sd build` trusts the computer that built the card. To add another,
  run `flip phone key` on it, then `flip phone trust add KEY --label NAME` from a
  trusted computer, then the `flip phone pin ...` line it prints, on the new
  one. The phone refuses to remove its last trusted computer.
- Keys: this computer's in `~/.config/flip/host-key.pem`, the phone's in
  `~/.config/flip/phone-identity.pem` (reused for every card you build, so
  rebuilding doesn't change the phone's identity).
- Locked out anyway? Put the card in your computer's reader and fix it there.
  The phone's own storage, the toolbox and KaiOS are never touched.

Normal use (calls, the agent, Wi-Fi, mobile data) doesn't go through this. The
agent runs as the unprivileged user `phone`, with no sudo, so it can't reach
admin functions. Debian security updates install automatically.

`flip sd build --debug` replaces flipd with a root login on the USB serial line,
for first bring-up only. Anyone with a cable gets root on a debug card.

## Pinned inputs

Every external input is pinned in `pins.toml`, by commit SHA or SHA-256, with
a trust level. The build aborts if anything fetched doesn't match its pin. Set
`FLIP_URL_<NAME>` (e.g. `FLIP_URL_LINUX`) to fetch from your own mirror; the
pins still apply. lk2nd is built from its pinned release plus the patches in
`patches/lk2nd/`, re-applied to a pristine checkout on every build. The
toolbox image is reproducible: the same inputs give byte-identical output.

## Signing key and verified boot

The first build creates an AVB signing key at `~/.config/flip/avb-key.pem`
(override the directory with `FLIP_CONFIG_DIR`). `flip flash all` installs its
public half on the phone, and from then on the phone boots custom images
signed with it (verified boot state "yellow"). **Back it up privately**
(`flip keys export /path/to/encrypted/drive`). If it's lost you can generate a
new one, but you'll need `flip flash all` again before the phone accepts
newly built images.

Note: the vendored `tools/avbtool.py` is modified (marked LOCAL MODIFICATION)
to drop the stock vbmeta's hash descriptors when re-signing, and the hashtree
disabled flag is set. With your key installed, the phone therefore doesn't
verify `boot`, `dtbo`, `recovery`, `system` or `vendor` at all. That's what
lets lk2nd boot from recovery, but it also means anyone with physical access
could change those partitions.

## Development

```sh
uv run pytest
```

# Sideloading guide

### Preparation (only once)
- Perform the above steps to build and flash the toolkit
- Select "Disable encryption" and follow the on-screen instructions
- **WARNING**: Disabling encryption modifies the vendor partition. Please
  make a backup first using `dd` after exposing it via USB **without**
  mounting it.
- **NOTE**: USB storage is exposed **read-only** by default, which is what you
  want for backups. To modify a partition (as in the steps below), select
  "USB storage" -> "7. Mode" to switch to read-write *before* selecting the
  partition. The mode can only be changed while USB storage is off.

### Getting adb to work

**NOTE**: This won't give you root access, and further steps may be needed to enable devtools.

```sh
# Boot the hacking toolbox
# Select "USB storage" -> "7. Mode" (read-write) -> "userdata"
# Mount the exposed storage device on a Linux machine
# You might have to execute these commands as root to avoid permission errors
cd /path/to/mounted/userdata/

# If you don't have ~/.android/adbkey.pub
adb keygen ~/.android/adbkey

# Copy the key
cat ~/.android/adbkey.pub >> misc/adb/adb_keys

# Add SELinux metadata
setfattr -n security.selinux -v u:object_r:system_data_file:s0 misc/adb/adb_keys

# Unmount the storage device
# Select "USB storage" -> "system" (mode stays read-write)
# Mount the exposed storage device
cd /path/to/mounted/system/

# edit init.usb.configfs.rc
# change the on property:sys.usb.config=mtp part to look like this:
on property:sys.usb.config=mtp && property:sys.usb.configfs=1
    start adbd

on property:sys.usb.ffs.ready=1 && property:sys.usb.config=mtp && property:sys.usb.configfs=1
    write /config/usb_gadget/g1/configs/b.1/strings/0x409/configuration "mtp_adb"
    symlink /config/usb_gadget/g1/functions/mtp.gs0 /config/usb_gadget/g1/configs/b.1/f1   
    symlink /config/usb_gadget/g1/functions/ffs.adb /config/usb_gadget/g1/configs/b.1/f2
    write /config/usb_gadget/g1/UDC ${sys.usb.controller}
    setprop sys.usb.state ${sys.usb.config}

# unmount and reboot, make sure USB storage is enabled
```

### Sideloading apps (the hard way)

```sh
# Boot the hacking toolbox
# Select "USB storage" -> "7. Mode" (read-write) -> "userdata"
# Mount the exposed storage device on a Linux machine
# You might have to execute these commands as root to avoid permission errors
cd /path/to/mounted/userdata/

# If you have installed the app in the KaiOS simulator:
cp -r /path/to/kaiosrt/gaia/profile/webapps/installed/<app-name> local/webapps/installed
# Otherwise:
mkdir local/webapps/installed/<app-name>
cp /path/to/application.zip local/webapps/installed/<app-name>/

# Symlink the application directory
ln -s /data/local/webapps/installed/<app-name> local/webapps/vroot/<app-name>

# Ensure that the SELinux contexts are correct
setfattr -n security.selinux -v u:object_r:system_data_file:s0 local/webapps/installed/<app-name>
setfattr -n security.selinux -v u:object_r:system_data_file:s0 local/webapps/installed/<app-name>/application.zip
setfattr -h -n security.selinux -v u:object_r:system_data_file:s0 local/webapps/vroot/<app-name>

# Open the database:
sqlite3 local/webapps/db/apps.sqlite

sqlite> INSERT INTO apps VALUES ("<app-name>", "<version>", 1,
                                 "http://<app-name>.localhost/manifest.webmanifest",
                                 "", "", 0,
                                 "Enabled",
                                 "Installed",
                                 "Idle",
                                 0, 0, "", "", "");
sqlite> .exit

# Eject the storage device
# Power off and power on
```

# Known issues

- The font is ugly. Workaround: remove `fbcon=font:10x18` from
  `BOOT_CMDLINE` in `src/flip/profile.py`

# Nokia 2780 hacking toolbox

This repo is the unofficial mirror of [weeknd-toolbox](https://git.abscue.de/affe_null/weeknd-toolbox/) to provide prebuilt images.

---

This small Linux system replaces the stock "recovery" system and lets you expose
the internal storage of a Nokia 2780 Flip via USB. It is reported not to work on
the Nokia 2760 Flip due to the lack of a `fastboot oem sudo` command.

It uses [lk2nd](https://github.com/msm8916-mainline/lk2nd) and a
close-to-mainline kernel:
<https://github.com/affenull2345/linux/tree/wip/qm215/6.3-rc7>

### Building

#### Requirements

On Arch Linux:

```sh
sudo pacman -S --needed aarch64-linux-gnu-gcc arm-none-eabi-gcc \
    base-devel git bc flex bison perl python dtc cpio openssl curl android-tools
```

`build.sh` checks for these up front and lists anything missing.

To download and build all components, run:

```sh
./build.sh
```

This needs the `aarch64-linux-gnu-gcc` (kernel) and `arm-none-eabi-gcc` (lk2nd)
cross-compilers plus `dtc`. Set `CROSS_COMPILE` / `TOOLCHAIN_PREFIX` to use
differently named toolchains.

You can optionally set the JOBS environment variable to the number of make jobs
to use for building the Linux kernel.

All external inputs (kernel and lk2nd commits, busybox and stock vbmeta
SHA-256 hashes) are pinned in `pins.sh`. The build aborts if anything fetched
does not match its pin.

- The stock vbmeta (whose boot/dtbo hashes are re-signed with your key) is
  taken from `~/.config/weeknd-toolbox/stock-vbmeta.img` when present: copy
  `parts/vbmetabak.img` from a `backup.sh` backup there. `STOCK_VBMETA=path`
  overrides it. Without either, the original author's copy (firmware
  00.2238.20.13) is downloaded and verified against its pinned hash.
- `LINUX_REPO` / `LK2ND_REPO` fetch from a mirror you control; the commit pins
  still apply.

### Signing key

The first build creates an AVB signing key at
`~/.config/weeknd-toolbox/avb-key.pem` (override with `WEEKND_KEY_DIR`).
`flash.sh` installs its public half on the phone (`avb_custom_key`), and from
then on the phone only boots custom images signed with it. The key lives
outside the repo so `clean.sh` and git never touch it. **Back it up privately**:
if it's lost you can generate a new one, but you'll need to re-run `flash.sh`
(including `fastboot oem sudo`) before the phone accepts newly built images.
The build warns if the current key differs from the one last flashed.

### Booting
After building, you can run `./flash.sh` to boot the hacking toolbox.
This script permanently modifies the recovery partition by installing lk2nd
to it.

To boot the hacking toolbox, hold the volume-up key while turning on the
phone and release it when the "custom operating system" warning appears.

lk2nd first looks for an ext2/ext4 file system with `extlinux/extlinux.conf`
on any partition of 16 MiB or more, including an SD card, and boots it if
found. Otherwise it boots the toolbox. Hold volume-up again as soon as the
warning disappears to skip that search and always get the toolbox (rescue
route). Turning the phone on without any keys boots KaiOS as usual.

lk2nd is built from the pinned release plus the patches in `patches/lk2nd/`,
which are re-applied to a pristine checkout on every build.

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

- The font is ugly. Workaround: remove `fbcon=font:10x18` from cmdline
  in `build.sh`

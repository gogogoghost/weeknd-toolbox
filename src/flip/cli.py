"""flip: the command-line interface."""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

import typer

from flip import __version__, backup, build, config, device, flash, keys, lock, phone, sd
from flip.util import FlipError, say

app = typer.Typer(
    help="Build, flash and back up custom Linux for the Nokia 2780 Flip.",
    no_args_is_help=True,
    add_completion=False,
)
backup_app = typer.Typer(help="Full eMMC backups (via the toolbox's USB storage).",
                         no_args_is_help=True)
keys_app = typer.Typer(help="The signing key the phone trusts.", no_args_is_help=True)
device_app = typer.Typer(help="Inspect the connected phone (read-only).", no_args_is_help=True)
app.add_typer(backup_app, name="backup")
app.add_typer(keys_app, name="keys")
app.add_typer(device_app, name="device")
sd_app = typer.Typer(help="The SD card Linux system (Debian).", no_args_is_help=True)
phone_app = typer.Typer(
    help="Administer the phone over the USB cable (SD card Linux, trusted computers only).",
    no_args_is_help=True)
trust_app = typer.Typer(help="Which computers the phone accepts.", no_args_is_help=True)
app.add_typer(sd_app, name="sd")
app.add_typer(phone_app, name="phone")
phone_app.add_typer(trust_app, name="trust")


def _version(value: bool) -> None:
    if value:
        print(f"flip {__version__}")
        raise typer.Exit()


@app.callback()
def _root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True,
                                       help="Show the version and exit.")) -> None:
    pass


# --- build / flash / clean -------------------------------------------------

@app.command("build")
def cmd_build(
    jobs: int = typer.Option(config.jobs_default(), "-j", "--jobs", help="Parallel make jobs."),
    stock_vbmeta: Optional[Path] = typer.Option(
        None, help="Stock vbmeta to re-sign (default: your adopted one, else the author's)."),
) -> None:
    """Build the kernel, lk2nd, the toolbox, and the signed recovery + vbmeta images."""
    build.build_all(jobs, stock_vbmeta)


@app.command("flash")
def cmd_flash(
    target: flash.Target = typer.Argument(
        flash.Target.RECOVERY,
        help="'recovery': lk2nd + toolbox only. 'all': also the signing key and vbmeta "
             "(first install, or after changing keys)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
) -> None:
    """Flash the phone (must be in stock fastboot: hold volume-down while plugging in)."""
    flash.flash(target, confirm=lambda q: yes or typer.confirm(q, default=False))


@app.command("clean")
def cmd_clean(all_: bool = typer.Option(False, "--all", help="Also delete the kernel/lk2nd "
                                        "checkouts and the download cache.")) -> None:
    """Delete build outputs. Never touches backups/ or your key."""
    targets = [config.OUT]
    if all_:
        targets += [config.LINUX_DIR, config.LK2ND_DIR, config.CACHE]
    for t in targets:
        if t.exists():
            shutil.rmtree(t)
            say(f"removed {t.relative_to(config.REPO)}/")


@app.command("doctor")
def cmd_doctor() -> None:
    """Check build tools, the lockfile, your key and the phone connection."""
    ok = True
    missing = build.missing_tools()
    if shutil.which("fastboot") is None:
        missing.append("fastboot")
    say("build tools: " + ("all present" if not missing else "MISSING " + " ".join(missing)))
    ok &= not missing
    sd_missing = sd.missing_tools()
    say("SD card tools: " + ("all present" if not sd_missing else
                             "MISSING " + ", ".join(sd_missing)))
    for problem in phone.host_ready():
        say(f"phone link: {problem}")
    try:
        pins = lock.load()
        say(f"lockfile: {len(pins.git)} git + {len(pins.file)} file pins OK")
    except FlipError as e:
        say(f"lockfile: ERROR {e}")
        ok = False
    _print_key_status()
    say(f"own stock vbmeta: {'yes' if config.own_vbmeta().is_file() else 'no'}")
    try:
        say(f"phone: {device.detect_mode().value}")
    except FlipError as e:
        say(f"phone: {e}")
    if not ok:
        raise typer.Exit(1)


# --- backup ------------------------------------------------------------------

@backup_app.command("create")
def cmd_backup_create(dev: str = typer.Argument(..., help="The toolbox's disk, e.g. /dev/sda")):
    """Copy the whole eMMC, extract every partition and hash everything.

    In the toolbox: USB storage -> 5 (Whole eMMC), in read-only mode.
    """
    backup.create(dev)


@backup_app.command("list")
def cmd_backup_list() -> None:
    """List backups."""
    backups = backup.list_backups()
    if not backups:
        say("no backups yet")
    for b in backups:
        size = sum(f.stat().st_size for f in b.rglob("*") if f.is_file())
        print(f"{b.name}  {size / 2**30:.1f} GiB")


@backup_app.command("verify")
def cmd_backup_verify(name: Optional[str] = typer.Argument(None, help="Default: newest.")):
    """Re-check every file of a backup against its SHA256SUMS."""
    b = backup.resolve(name)
    say(f"verifying {b.name} ...")
    bad = backup.verify(b)
    if bad:
        raise FlipError(f"{len(bad)} file(s) don't match: {', '.join(bad)}")
    say("all files match")


@backup_app.command("adopt-vbmeta")
def cmd_backup_adopt(name: Optional[str] = typer.Argument(None, help="Default: newest.")):
    """Build from this backup's stock vbmeta (vbmetabak) instead of the author's."""
    dest = backup.adopt_vbmeta(backup.resolve(name))
    say(f"installed {dest}")


# --- keys --------------------------------------------------------------------

def _print_key_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        st = keys.status(Path(tmp))
    if not st.exists:
        say(f"signing key: none yet ({st.path}); `flip build` creates one")
        return
    say(f"signing key: {st.path} ({'private' if st.private else 'READABLE BY OTHERS'})")
    say(f"  pkmd sha256: {st.pkmd_sha256}")
    match = {True: "yes", False: "NO: run `flip flash all`", None: "unknown (no flash record)"}
    say(f"  trusted by phone: {match[st.matches_phone]}")


@keys_app.command("show")
def cmd_keys_show() -> None:
    """Show the key's location, fingerprint, and whether the phone trusts it."""
    _print_key_status()


@keys_app.command("export")
def cmd_keys_export(dest: Path = typer.Argument(..., help="Directory, e.g. an encrypted drive.")):
    """Copy the private key somewhere safe (readable only by you)."""
    for p in keys.export(dest):
        say(f"wrote {p}")
    say("keep this copy private: anyone with it can sign images your phone will boot")


# --- device ------------------------------------------------------------------

@device_app.command("status")
def cmd_device_status() -> None:
    """Which mode the phone is in."""
    say(device.detect_mode().value)


@device_app.command("info")
def cmd_device_info(save: bool = typer.Option(True, help="Also save to backups/.")) -> None:
    """`fastboot getvar all` (stock or lk2nd fastboot)."""
    mode = device.detect_mode()
    if mode not in (device.Mode.STOCK_FASTBOOT, device.Mode.LK2ND_FASTBOOT):
        raise FlipError(f"phone must be in fastboot (detected: {mode.value})")
    text = device.getvar_all()
    print(text, end="")
    if save:
        kind = "lk2nd" if mode is device.Mode.LK2ND_FASTBOOT else "stock"
        _save(f"getvar-{kind}-{date.today()}.txt", text)


@device_app.command("log")
def cmd_device_log(save: bool = typer.Option(True, help="Also save to backups/.")) -> None:
    """lk2nd's boot log (phone in lk2nd fastboot: hold volume-down as lk2nd starts)."""
    if device.detect_mode() is not device.Mode.LK2ND_FASTBOOT:
        raise FlipError("phone must be in lk2nd fastboot")
    text = device.lk2nd_log()
    print(text, end="")
    if save:
        _save(f"lk2nd-log-{date.today()}.txt", text)


def _save(name: str, text: str) -> None:
    config.BACKUPS.mkdir(exist_ok=True)
    path = config.BACKUPS / name
    path.write_text(text)
    say(f"saved {path.relative_to(config.REPO)}")


# --- sd ----------------------------------------------------------------------

@sd_app.command("build")
def cmd_sd_build(
    debug: bool = typer.Option(False, help="Root login on the USB serial line instead of "
                               "flipd. For first bring-up only; never for daily use."),
    backup_name: Optional[str] = typer.Option(None, "--backup",
                                              help="Backup to take firmware from (default: newest)."),
) -> None:
    """Build out/sd/sd.img: Debian, our kernel and modules, your firmware, flipd."""
    sd.build_image(debug=debug, backup_name=backup_name)


@sd_app.command("write")
def cmd_sd_write(dev: str = typer.Argument(..., help="The card, e.g. /dev/mmcblk0 (see lsblk)"),
                 yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask.")) -> None:
    """Erase a card and write the image to it, then read it back to verify."""
    sd.write_card(dev, confirm=lambda q: yes or typer.confirm(q, default=False))


@sd_app.command("update")
def cmd_sd_update(dev: str = typer.Argument(..., help="The card, e.g. /dev/mmcblk0"),
                  yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask.")) -> None:
    """Put the current kernel, device tree and modules on a card, keeping its data."""
    sd.update_card(dev, confirm=lambda q: yes or typer.confirm(q, default=False))


@sd_app.command("info")
def cmd_sd_info() -> None:
    """What's in the current image."""
    m = sd.read_manifest()
    say(f"{m['image']}: {m['size'] / 2**30:.2f} GiB, sha256 {m['sha256']}")
    say(f"Debian {m['debian']['suite']} snapshot {m['debian']['snapshot']}, "
        f"kernel {m['kernel_release']}{', DEBUG' if m['debug'] else ''}")
    say(f"firmware from backup {m['firmware_from_backup']} ({len(m['firmware'])} files)")
    say(f"phone key {m['phone_key']}")


# --- phone -------------------------------------------------------------------

PORT = typer.Option(None, "--port", help="Serial port (default: find the phone).")


@phone_app.command("info")
def cmd_phone_info(port: Optional[str] = PORT) -> None:
    """Connect and show basic facts about the phone."""
    with phone.session(port) as s:
        info = s.call("info")
    up = int(info["uptime"])
    say(f"{info['hostname']}: kernel {info['kernel']}, up {up // 3600}h{up % 3600 // 60:02d}m")
    say(f"phone key {info['phone_key']}; this computer {info['you']}")


@phone_app.command("shell")
def cmd_phone_shell(port: Optional[str] = PORT) -> None:
    """Root shell on the phone."""
    with phone.session(port) as s:
        code = s.shell()
    raise typer.Exit(code)


@phone_app.command("run", context_settings={"allow_extra_args": True,
                                            "ignore_unknown_options": True})
def cmd_phone_run(argv: list[str] = typer.Argument(..., help="Command and arguments "
                                                 "(put -- before options meant for it)."),
                  port: Optional[str] = PORT) -> None:
    """Run a command on the phone as root; its output and exit code come back."""
    stdin = None if sys.stdin.isatty() else sys.stdin.buffer
    with phone.session(port) as s:
        code = s.run(argv, stdin, sys.stdout.buffer, sys.stderr.buffer)
    raise typer.Exit(code)


@phone_app.command("push")
def cmd_phone_push(local: Path, remote: str, port: Optional[str] = PORT) -> None:
    """Copy a file to the phone (checksum verified)."""
    if not local.is_file():
        raise FlipError(f"{local} is not a file")
    with phone.session(port) as s:
        r = s.push(local, remote)
    say(f"wrote {r['written']} (sha256 {r['sha256'][:16]}...)")


@phone_app.command("pull")
def cmd_phone_pull(remote: str, local: Path, port: Optional[str] = PORT) -> None:
    """Copy a file from the phone (checksum verified)."""
    if local.is_dir():
        local = local / Path(remote).name
    with phone.session(port) as s:
        r = s.pull(remote, local)
    say(f"wrote {local} ({r['size']} bytes, sha256 {r['sha256'][:16]}...)")


@phone_app.command("key")
def cmd_phone_key() -> None:
    """This computer's flip-link public key (to add it on the phone from a trusted computer)."""
    print(phone.host_public())


@phone_app.command("pin")
def cmd_phone_pin(key: str = typer.Argument(..., help="The phone key, flip-ed25519:...")) -> None:
    """On a newly trusted computer: record the phone's key so flip can verify it."""
    say(f"pinned phone key {phone.pin_phone(key)}")


@phone_app.command("setup-host")
def cmd_phone_setup_host() -> None:
    """Install a udev rule: you may open the phone's port, ModemManager leaves it alone."""
    phone.setup_host()


@trust_app.command("list")
def cmd_trust_list(port: Optional[str] = PORT) -> None:
    """Computers the phone accepts."""
    with phone.session(port) as s:
        hosts = s.call("trust-list")["hosts"]
    for h in hosts:
        print(f"{h['fingerprint']}  {h['label']}{'  (this computer)' if h['you'] else ''}")


@trust_app.command("add")
def cmd_trust_add(key: str = typer.Argument(..., help="The other computer's `flip phone key`"),
                  label: str = typer.Option(..., help="A name for that computer."),
                  port: Optional[str] = PORT) -> None:
    """Let another computer administer the phone."""
    with phone.session(port) as s:
        r = s.call("trust-add", key=key, label=label)
    say(f"phone now trusts {r['added']} ({label})")
    say("on that computer, run:")
    print(f"    flip phone pin {r['phone_key']}")


@trust_app.command("remove")
def cmd_trust_remove(fingerprint: str = typer.Argument(..., help="From `flip phone trust list`"),
                     port: Optional[str] = PORT) -> None:
    """Stop accepting a computer."""
    with phone.session(port) as s:
        r = s.call("trust-remove", fingerprint=fingerprint)
    say(f"removed {r['removed']}")


def main() -> None:
    try:
        app()
    except FlipError as e:
        print(f">> ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n>> interrupted", file=sys.stderr)
        sys.exit(130)

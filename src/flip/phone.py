"""Talk to the phone running SD-card Linux over the wired flip-link connection."""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from pathlib import Path

from flip import config
from flip.link import client, keys as lkeys
from flip.link.secure import HandshakeError, Rejected, fingerprint
from flip.util import FlipError, run, say

UDEV_RULE_NAME = "70-flip-link.rules"
# Lets the logged-in user open the phone's port, and keeps ModemManager from
# probing it with AT commands.
UDEV_RULE = (
    f'SUBSYSTEM=="tty", ATTRS{{product}}=="{client.USB_PRODUCT}", '
    'ENV{ID_MM_DEVICE_IGNORE}="1", TAG+="uaccess"\n'
)


def ensure_host_key():
    config.ensure_config_dir()
    path = config.host_key()
    if not path.exists():
        say(f"creating this computer's flip-link key: {path}")
        return lkeys.generate(path)
    try:
        return lkeys.load_private(path)
    except lkeys.KeyFileError as e:
        raise FlipError(str(e)) from e


def host_public() -> str:
    return lkeys.encode_public(lkeys.public_bytes(ensure_host_key().public_key()))


def host_label() -> str:
    return f"{os.environ.get('USER', 'user')}@{socket.gethostname()}"


def phone_public() -> bytes:
    path = config.phone_key()
    try:
        return lkeys.decode_public(path.read_text())
    except FileNotFoundError:
        raise FlipError(
            f"no phone key at {path}. It is written by `flip sd build`; on another computer, "
            "run `flip phone pin <key>` with the key `flip phone trust add` printed.") from None
    except lkeys.KeyFileError as e:
        raise FlipError(f"{path}: {e}") from e


def pin_phone(key_text: str) -> str:
    try:
        raw = lkeys.decode_public(key_text)
    except lkeys.KeyFileError as e:
        raise FlipError(str(e)) from e
    path = config.phone_key()
    if path.exists() and path.read_text().strip() != key_text.strip():
        raise FlipError(f"{path} already holds a different phone key; delete it first if "
                        "you really replaced the phone")
    config.ensure_config_dir()
    path.write_text(lkeys.encode_public(raw) + "\n")
    return fingerprint(raw)


@contextmanager
def session(port: str | None = None):
    identity = ensure_host_key()
    expected = phone_public()
    try:
        s, fd = client.connect(identity, expected, port)
    except Rejected as e:
        raise FlipError(f"the phone refused this computer: {e}\n"
                        f"   This computer's key: {host_public()}\n"
                        "   Add it from a trusted computer with `flip phone trust add`.") from e
    except (HandshakeError, client.LinkError) as e:
        raise FlipError(str(e)) from e
    try:
        yield s
    except client.LinkError as e:
        raise FlipError(str(e)) from e
    except HandshakeError as e:
        raise FlipError(f"connection to the phone failed: {e}") from e
    finally:
        os.close(fd)


def setup_host() -> None:
    """Install the udev rule (needs sudo)."""
    rule = config.OUT / UDEV_RULE_NAME
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(UDEV_RULE)
    dest = Path("/etc/udev/rules.d") / UDEV_RULE_NAME
    say(f"installing {dest} (asks for your password)")
    run(["sudo", "install", "-m", "644", rule, dest])
    run(["sudo", "udevadm", "control", "--reload"])
    run(["sudo", "udevadm", "trigger", "--subsystem-match=tty"])
    say("done; replug the phone if it is connected")


def host_ready() -> list[str]:
    """Problems that would stop `flip phone` from working on this computer."""
    problems = []
    if not (Path("/etc/udev/rules.d") / UDEV_RULE_NAME).exists():
        problems.append("udev rule not installed (`flip phone setup-host`)")
    if not config.phone_key().exists():
        problems.append("no phone key yet (`flip sd build` or `flip phone pin`)")
    return problems

"""Identity keys and the phone's trusted-hosts list.

Public keys are written as `flip-ed25519:<base64 of the 32-byte key>`, one
line, easy to copy between computers. Private keys are PKCS#8 PEM files
readable only by their owner.

trusted-hosts (on the phone, root-only): one key per line, then a label:
    flip-ed25519:AAAA...  brayden's laptop
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "flip-ed25519:"


class KeyFileError(ValueError):
    pass


def public_bytes(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def encode_public(public: bytes) -> str:
    return PREFIX + base64.b64encode(public).decode()


def decode_public(text: str) -> bytes:
    text = text.strip()
    if not text.startswith(PREFIX):
        raise KeyFileError(f"not a flip public key (should start with {PREFIX})")
    try:
        raw = base64.b64decode(text[len(PREFIX):], validate=True)
    except binascii.Error as e:
        raise KeyFileError("public key is not valid base64") from e
    if len(raw) != 32:
        raise KeyFileError("public key has the wrong length")
    Ed25519PublicKey.from_public_bytes(raw)  # validates
    return raw


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def generate(path: Path) -> Ed25519PrivateKey:
    """Create a new private key file (refuses to overwrite)."""
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    _write_private_file(path, pem)
    return key


def load_private(path: Path) -> Ed25519PrivateKey:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise KeyFileError(f"{path} is readable by other users (mode {mode:o}); chmod 600 it")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError(f"{path} is not an Ed25519 key")
    return key


@dataclass(frozen=True)
class TrustedHost:
    key: bytes
    label: str


def read_trusted(path: Path) -> list[TrustedHost]:
    hosts = []
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, label = line.partition(" ")
        try:
            hosts.append(TrustedHost(decode_public(key), label.strip()))
        except KeyFileError:
            continue  # a bad line never grants access
    return hosts


def write_trusted(path: Path, hosts: list[TrustedHost]) -> None:
    text = "# Computers allowed to administer this phone over USB (flip).\n"
    text += "".join(f"{encode_public(h.key)} {h.label}\n" for h in hosts)
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    _write_private_file(tmp, text.encode())
    os.replace(tmp, path)

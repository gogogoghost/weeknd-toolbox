"""Handshake and encrypted channel.

Both ends have a long-term Ed25519 identity key. The computer knows the
phone's public key in advance (written when the SD card was built, or pinned
with `flip phone pin`); the phone keeps a list of trusted computers' keys.

    1. host  -> phone  H1 | version | host ephemeral X25519 | random(32)
    2. phone -> host   P1 | phone ephemeral X25519 | phone identity
                          | Sign_phone(LABEL_PHONE | hash(transcript))
    3. host  -> phone  H2 | host identity | Sign_host(LABEL_HOST | hash(transcript))
    4. phone -> host   (encrypted) "ready", or plaintext E1 | reason

Each signature covers every handshake byte sent so far, both ephemeral keys
and the signer's own identity, so neither side can be impersonated and a
recorded handshake can't be replayed. Session keys come from HKDF over the
X25519 shared secret, salted with the transcript hash, one key per direction
(forward secret: the ephemeral keys are discarded after the session).
Messages are ChaCha20-Poly1305 with a per-direction counter as nonce; a
message out of order, replayed or modified fails to decrypt and ends the
session.
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from flip.link import PROTOCOL_VERSION
from flip.link.frame import KIND_HANDSHAKE, KIND_SECURE, FdTransport, FrameReader, encode
from flip.link.keys import public_bytes

LABEL_PHONE = b"flip-link v1 phone signature\0"
LABEL_HOST = b"flip-link v1 host signature\0"
LABEL_KEYS = b"flip-link v1 session keys\0"
HANDSHAKE_TIMEOUT = 10.0


class HandshakeError(Exception):
    pass


class Rejected(HandshakeError):
    """The phone refused this computer (not trusted)."""


class Channel:
    """Encrypted, ordered messages in both directions."""

    def __init__(self, transport: FdTransport, reader: FrameReader,
                 send_key: bytes, recv_key: bytes, peer: bytes):
        self.t = transport
        self.reader = reader
        self.peer = peer  # the other side's identity public key (32 bytes)
        self._send = ChaCha20Poly1305(send_key)
        self._recv = ChaCha20Poly1305(recv_key)
        self._send_n = 0
        self._recv_n = 0
        self._lock = threading.Lock()

    @staticmethod
    def _nonce(n: int) -> bytes:
        return b"\0\0\0\0" + struct.pack(">Q", n)

    def send(self, msg_type: int, body: bytes = b"") -> None:
        with self._lock:
            ct = self._send.encrypt(self._nonce(self._send_n), bytes([msg_type]) + body, None)
            self._send_n += 1
            self.t.write(encode(KIND_SECURE, ct))

    def recv(self, timeout: float | None = None) -> tuple[int, bytes]:
        """Next (type, body). Raises HandshakeError if the peer restarted or tampering."""
        kind, payload = self.reader.read(timeout)
        if kind == KIND_HANDSHAKE:
            if payload[:2] == b"E1":
                raise Rejected(payload[2:].decode("utf-8", "replace"))
            # The other side started a new session: this one is over.
            raise SessionRestart(payload)
        return self.decrypt(payload)

    def decrypt(self, payload: bytes) -> tuple[int, bytes]:
        try:
            pt = self._recv.decrypt(self._nonce(self._recv_n), payload, None)
        except InvalidTag as e:
            raise HandshakeError("message failed authentication") from e
        self._recv_n += 1
        if not pt:
            raise HandshakeError("empty message")
        return pt[0], pt[1:]


class SessionRestart(HandshakeError):
    """A new handshake arrived in the middle of a session."""

    def __init__(self, hello: bytes):
        super().__init__("peer restarted the session")
        self.hello = hello


def _derive(shared: bytes, transcript: bytes) -> tuple[bytes, bytes]:
    okm = HKDF(algorithm=hashes.SHA256(), length=64,
               salt=hashlib.sha256(transcript).digest(), info=LABEL_KEYS).derive(shared)
    return okm[:32], okm[32:]  # host->phone, phone->host


def _hs_send(t: FdTransport, payload: bytes) -> None:
    t.write(encode(KIND_HANDSHAKE, payload))


def _hs_recv(reader: FrameReader, prefix: bytes, length: int, timeout: float) -> bytes:
    kind, payload = reader.read(timeout)
    if kind != KIND_HANDSHAKE:
        raise HandshakeError("expected a handshake message")
    if payload[:2] == b"E1":
        raise Rejected(payload[2:].decode("utf-8", "replace"))
    if payload[:2] != prefix or len(payload) != length:
        raise HandshakeError(f"malformed handshake message (expected {prefix!r})")
    return payload


# --- computer side -------------------------------------------------------------

def client_handshake(t: FdTransport, identity: Ed25519PrivateKey,
                     expected_phone: bytes) -> Channel:
    reader = FrameReader(t)
    eph = X25519PrivateKey.generate()
    m1 = b"H1" + bytes([PROTOCOL_VERSION]) + public_bytes(eph.public_key()) + os.urandom(32)
    _hs_send(t, m1)

    m2 = _hs_recv(reader, b"P1", 2 + 32 + 32 + 64, HANDSHAKE_TIMEOUT)
    phone_eph, phone_id, sig = m2[2:34], m2[34:66], m2[66:]
    if phone_id != expected_phone:
        raise HandshakeError(
            "the phone presented an unexpected identity key: this is not the phone you "
            f"set up (got {fingerprint(phone_id)}, expected {fingerprint(expected_phone)})")
    try:
        Ed25519PublicKey.from_public_bytes(phone_id).verify(
            sig, LABEL_PHONE + hashlib.sha256(m1 + m2[:66]).digest())
    except InvalidSignature as e:
        raise HandshakeError("the phone's signature is invalid") from e

    host_id = public_bytes(identity.public_key())
    m3_head = b"H2" + host_id
    m3 = m3_head + identity.sign(LABEL_HOST + hashlib.sha256(m1 + m2 + m3_head).digest())
    _hs_send(t, m3)

    shared = eph.exchange(X25519PublicKey.from_public_bytes(phone_eph))
    h2p, p2h = _derive(shared, m1 + m2 + m3)
    ch = Channel(t, reader, h2p, p2h, phone_id)
    try:
        msg_type, body = ch.recv(HANDSHAKE_TIMEOUT)
    except SessionRestart as e:
        raise HandshakeError("unexpected handshake message") from e
    if (msg_type, body) != (0, b"ready"):
        raise HandshakeError("phone did not confirm the session")
    return ch


# --- phone side ----------------------------------------------------------------

def server_handshake(t: FdTransport, reader: FrameReader, identity: Ed25519PrivateKey,
                     is_trusted, hello: bytes | None = None) -> Channel:
    """Answer one handshake. `hello` is an H1 already read (after a restart).

    is_trusted(host_public_key_bytes) -> bool
    """
    if hello is None:
        kind, hello = reader.read(None)
        if kind != KIND_HANDSHAKE:
            raise HandshakeError("ignored a message outside a session")
    m1 = hello
    if len(m1) != 2 + 1 + 32 + 32 or m1[:2] != b"H1":
        raise HandshakeError("malformed hello")
    if m1[2] != PROTOCOL_VERSION:
        _hs_send(t, b"E1" + f"unsupported protocol version {m1[2]}".encode())
        raise HandshakeError(f"client protocol version {m1[2]} unsupported")
    host_eph = m1[3:35]

    eph = X25519PrivateKey.generate()
    phone_id = public_bytes(identity.public_key())
    m2_head = b"P1" + public_bytes(eph.public_key()) + phone_id
    m2 = m2_head + identity.sign(LABEL_PHONE + hashlib.sha256(m1 + m2_head).digest())
    _hs_send(t, m2)

    m3 = _hs_recv(reader, b"H2", 2 + 32 + 64, HANDSHAKE_TIMEOUT)
    host_id, sig = m3[2:34], m3[34:]
    try:
        Ed25519PublicKey.from_public_bytes(host_id).verify(
            sig, LABEL_HOST + hashlib.sha256(m1 + m2 + m3[:34]).digest())
    except InvalidSignature as e:
        raise HandshakeError("host signature invalid") from e
    if not is_trusted(host_id):
        _hs_send(t, b"E1" + ("this computer is not trusted by the phone (key "
                             f"{fingerprint(host_id)})").encode())
        raise Rejected(f"untrusted host {fingerprint(host_id)}")

    shared = eph.exchange(X25519PublicKey.from_public_bytes(host_eph))
    h2p, p2h = _derive(shared, m1 + m2 + m3)
    ch = Channel(t, reader, p2h, h2p, host_id)
    ch.send(0, b"ready")
    return ch


def fingerprint(public: bytes) -> str:
    """Short, human-comparable fingerprint of an identity key."""
    h = hashlib.sha256(public).hexdigest()[:24]
    return ":".join(h[i:i + 4] for i in range(0, 24, 4))

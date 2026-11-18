"""Frames over a raw byte stream (the USB serial line).

    MAGIC(4) | kind(1) | length(4, big-endian) | payload | crc32(4)

The CRC covers kind, length and payload. It only catches line noise; the
encryption layer provides real integrity. A reader skips bytes until it finds
a valid frame, so junk on the line (a host program probing the port, a
half-sent frame from an interrupted session) is ignored rather than fatal.
"""

from __future__ import annotations

import os
import select
import struct
import zlib

MAGIC = b"\xf1\x1bLK"
MAX_PAYLOAD = 1 << 20
_HEAD = struct.Struct(">BI")

KIND_HANDSHAKE = ord("h")
KIND_SECURE = ord("s")


class LinkClosed(Exception):
    """The other end went away (EOF or I/O error)."""


class LinkTimeout(Exception):
    """No complete frame arrived in time."""


def encode(kind: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("frame payload too large")
    head = _HEAD.pack(kind, len(payload))
    crc = zlib.crc32(head + payload)
    return MAGIC + head + payload + struct.pack(">I", crc)


class FdTransport:
    """Blocking reads and writes on a file descriptor (tty or socket)."""

    def __init__(self, fd: int):
        self.fd = fd

    def read(self, n: int, timeout: float | None) -> bytes:
        try:
            if timeout is not None:
                ready, _, _ = select.select([self.fd], [], [], timeout)
                if not ready:
                    raise LinkTimeout()
            data = os.read(self.fd, n)
        except OSError as e:
            raise LinkClosed(str(e)) from e
        if not data:
            raise LinkClosed("end of stream")
        return data

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except OSError as e:
                raise LinkClosed(str(e)) from e
            view = view[n:]


class FrameReader:
    # Once a frame has started, the rest must follow within this many seconds;
    # otherwise the start was noise that happened to look like a header.
    PARTIAL_TIMEOUT = 2.0

    def __init__(self, transport: FdTransport):
        self.t = transport
        self.buf = bytearray()

    def _fill(self, need: int, timeout: float | None) -> None:
        while len(self.buf) < need:
            self.buf += self.t.read(65536, timeout)

    def read(self, timeout: float | None = None) -> tuple[int, bytes]:
        """Return the next valid (kind, payload), skipping anything invalid."""
        while True:
            self._fill(len(MAGIC), timeout)
            i = self.buf.find(MAGIC)
            if i < 0:
                # keep a possible partial magic at the end
                del self.buf[: len(self.buf) - (len(MAGIC) - 1)]
                continue
            del self.buf[:i]
            try:
                self._fill(len(MAGIC) + _HEAD.size, self.PARTIAL_TIMEOUT)
                kind, length = _HEAD.unpack_from(self.buf, len(MAGIC))
                if length > MAX_PAYLOAD or kind not in (KIND_HANDSHAKE, KIND_SECURE):
                    del self.buf[:1]
                    continue
                total = len(MAGIC) + _HEAD.size + length + 4
                self._fill(total, self.PARTIAL_TIMEOUT)
            except LinkTimeout:
                del self.buf[:1]  # not a real frame; rescan what we have
                continue
            body = bytes(self.buf[len(MAGIC): total - 4])
            (crc,) = struct.unpack_from(">I", self.buf, total - 4)
            if zlib.crc32(body) != crc:
                del self.buf[:1]
                continue
            del self.buf[:total]
            return kind, body[_HEAD.size:]

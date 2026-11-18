"""The computer side of flip-link: find the phone's serial port, connect, run operations."""

from __future__ import annotations

import fcntl
import hashlib
import os
import select
import shutil
import signal
import sys
import termios
import threading
import tty
from pathlib import Path
from typing import BinaryIO, Callable

from flip.link import messages as m
from flip.link.frame import FdTransport, LinkClosed, LinkTimeout
from flip.link.secure import Channel, client_handshake

# What the phone's USB gadget calls itself (set up by flip-gadget on the phone).
USB_PRODUCT = "flip-link"


class LinkError(Exception):
    pass


def find_ports(sys_class_tty: Path = Path("/sys/class/tty")) -> list[str]:
    """/dev/ttyACM* devices whose USB device is a flip phone."""
    ports = []
    for entry in sorted(sys_class_tty.glob("ttyACM*")):
        try:
            usb_dev = (entry / "device").resolve().parent  # interface -> device
            product = (usb_dev / "product").read_text().strip()
        except OSError:
            continue
        if product == USB_PRODUCT:
            ports.append(f"/dev/{entry.name}")
    return ports


def open_port(path: str) -> int:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    except PermissionError as e:
        raise LinkError(
            f"no permission to open {path}. Add yourself to the 'uucp' group "
            "(sudo usermod -aG uucp $USER, then log out and in), or run "
            "`flip phone setup-host`.") from e
    fcntl.ioctl(fd, termios.TIOCEXCL)  # nothing else (e.g. ModemManager) may open it
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


class Session:
    """One authenticated session carrying one operation."""

    def __init__(self, ch: Channel):
        self.ch = ch

    def _request(self, op: str, **fields) -> None:
        self.ch.send(m.REQUEST, m.dump({"op": op, **fields}))

    def _result(self, timeout: float = 30) -> dict:
        kind, body = self.ch.recv(timeout)
        if kind == m.ERROR:
            raise LinkError(m.load(body)["message"])
        if kind != m.RESULT:
            raise LinkError(f"unexpected reply type {kind}")
        return m.load(body)

    def call(self, op: str, **fields) -> dict:
        self._request(op, **fields)
        return self._result()

    def run(self, argv: list[str], stdin: BinaryIO | None, stdout: BinaryIO,
            stderr: BinaryIO) -> int:
        self._request("run", argv=argv)
        if stdin is None:
            self.ch.send(m.STDIN, b"")
        else:
            threading.Thread(target=self._feed, args=(stdin,), daemon=True).start()
        return self._drain(stdout, stderr)

    def _feed(self, src: BinaryIO) -> None:
        read = getattr(src, "read1", src.read)
        try:
            while chunk := read(m.CHUNK):
                self.ch.send(m.STDIN, chunk)
            self.ch.send(m.STDIN, b"")
        except (LinkClosed, OSError, ValueError):
            pass

    def _drain(self, stdout: BinaryIO, stderr: BinaryIO) -> int:
        while True:
            kind, body = self.ch.recv(None)
            if kind == m.STDOUT:
                stdout.write(body)
                stdout.flush()
            elif kind == m.STDERR:
                stderr.write(body)
                stderr.flush()
            elif kind == m.EXIT:
                return int(m.load(body)["code"])
            elif kind == m.ERROR:
                raise LinkError(m.load(body)["message"])

    def shell(self) -> int:
        """Interactive root shell on the phone, using this terminal."""
        in_fd = sys.stdin.fileno()
        if not os.isatty(in_fd):
            raise LinkError("`flip phone shell` needs a terminal; use `flip phone run` instead")
        cols, rows = shutil.get_terminal_size()
        self._request("shell", term=os.environ.get("TERM", "vt100"), rows=rows, cols=cols)

        def on_resize(*_):
            c, r = shutil.get_terminal_size()
            try:
                self.ch.send(m.RESIZE, m.dump({"rows": r, "cols": c}))
            except LinkClosed:
                pass

        saved = termios.tcgetattr(in_fd)
        old_handler = signal.signal(signal.SIGWINCH, on_resize)
        result: dict = {}

        def receive():
            try:
                result["code"] = self._drain(sys.stdout.buffer, sys.stderr.buffer)
            except Exception as e:  # reported after the terminal is restored
                result["error"] = e
            os.write(wake_w, b"x")

        wake_r, wake_w = os.pipe()
        try:
            tty.setraw(in_fd)
            threading.Thread(target=receive, daemon=True).start()
            while True:
                ready, _, _ = select.select([in_fd, wake_r], [], [])
                if wake_r in ready:
                    break
                data = os.read(in_fd, 4096)
                if data:
                    self.ch.send(m.STDIN, data)
        finally:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, old_handler)
            os.close(wake_r)
            os.close(wake_w)
        if "error" in result:
            raise result["error"]
        return result["code"]

    def push(self, local: Path, remote: str, progress: Callable[[int], None] | None = None) -> dict:
        st = local.stat()
        self._request("push", path=remote, mode=st.st_mode & 0o7777, size=st.st_size)
        self._result()  # ready
        h = hashlib.sha256()
        sent = 0
        with open(local, "rb") as f:
            while chunk := f.read(m.CHUNK):
                h.update(chunk)
                self.ch.send(m.DATA, chunk)
                sent += len(chunk)
                if progress:
                    progress(sent)
        self.ch.send(m.END, m.dump({"sha256": h.hexdigest()}))
        result = self._result(120)
        if result.get("sha256") != h.hexdigest():
            raise LinkError("phone reported a different checksum")
        return result

    def pull(self, remote: str, local: Path) -> dict:
        self._request("pull", path=remote)
        info = self._result()
        partial = local.with_name(f".{local.name}.flip-partial")
        h = hashlib.sha256()
        try:
            with open(partial, "wb") as f:
                while True:
                    kind, body = self.ch.recv(60)
                    if kind == m.DATA:
                        f.write(body)
                        h.update(body)
                    elif kind == m.END:
                        break
                    elif kind == m.ERROR:
                        raise LinkError(m.load(body)["message"])
            if m.load(body)["sha256"] != h.hexdigest():
                raise LinkError("checksum mismatch: transfer corrupted")
            os.chmod(partial, info["mode"] & 0o777)
            os.replace(partial, local)
        finally:
            partial.unlink(missing_ok=True)
        return {**info, "sha256": h.hexdigest()}


def connect(identity, phone_key: bytes, port: str | None = None) -> tuple[Session, int]:
    """Open the phone's port and authenticate. Returns (session, fd); close fd when done."""
    if port is None:
        ports = find_ports()
        if not ports:
            raise LinkError(
                "phone not found. Is it booted from the SD card and plugged in by USB? "
                "(It shows up as a 'flip-link' serial device.)")
        if len(ports) > 1:
            raise LinkError(f"several phones connected ({', '.join(ports)}); pick one with --port")
        port = ports[0]
    fd = open_port(port)
    try:
        ch = client_handshake(FdTransport(fd), identity, phone_key)
    except LinkTimeout:
        os.close(fd)
        raise LinkError(f"no answer from the phone on {port} (is flipd running?)") from None
    except BaseException:
        os.close(fd)
        raise
    return Session(ch), fd

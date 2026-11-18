"""flipd: the phone side of flip-link.

Runs as root on the phone, attached to the USB serial gadget (/dev/ttyGS0).
It is the only way to get a shell or change protected settings, and it only
serves computers listed in the trusted-hosts file. Nothing here listens on
the network.

    python3 -m flip.link.server [--device /dev/ttyGS0]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import platform
import pty
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

from flip.link import PROTOCOL_VERSION, messages as m
from flip.link.frame import FdTransport, FrameReader, LinkClosed, LinkTimeout
from flip.link.keys import (KeyFileError, TrustedHost, decode_public, encode_public,
                            load_private, public_bytes, read_trusted, write_trusted)
from flip.link.secure import (Channel, HandshakeError, SessionRestart, fingerprint,
                              server_handshake)

DEVICE = "/dev/ttyGS0"
IDENTITY = Path("/etc/flipd/identity.pem")
TRUSTED = Path("/etc/flipd/trusted-hosts")
SHELL_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root", "USER": "root", "LOGNAME": "root", "LANG": "C.UTF-8",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Server:
    def __init__(self, transport: FdTransport, identity, trusted_path: Path,
                 home: str = "/root"):
        self.home = home  # working directory for commands and shells
        self.t = transport
        self.reader = FrameReader(transport)
        self.identity = identity
        self.trusted_path = trusted_path

    def is_trusted(self, host: bytes) -> bool:
        return any(h.key == host for h in read_trusted(self.trusted_path))

    def serve_forever(self) -> None:
        hello = None
        while True:
            hello = self.serve_one(hello)

    def serve_one(self, hello: bytes | None = None) -> bytes | None:
        """Handle one session. Returns a new hello if the computer restarted mid-session."""
        try:
            ch = server_handshake(self.t, self.reader, self.identity, self.is_trusted, hello)
        except LinkTimeout:
            log("handshake timed out")
            return None
        except HandshakeError as e:
            log(f"handshake failed: {e}")
            return None
        who = fingerprint(ch.peer)
        try:
            kind, body = ch.recv(30)
            if kind != m.REQUEST:
                raise HandshakeError("expected a request")
            req = m.load(body)
            op = req.get("op")
            log(f"{who}: {op} {req.get('argv', req.get('path', ''))}")
            handler = OPS.get(op)
            if handler is None:
                ch.send(m.ERROR, m.dump({"message": f"unknown operation {op!r}"}))
                return None
            handler(self, ch, req)
        except SessionRestart as e:
            log(f"{who}: session interrupted by a new connection")
            return e.hello
        except (HandshakeError, LinkTimeout, ValueError, KeyError) as e:
            log(f"{who}: session ended: {e}")
            try:
                ch.send(m.ERROR, m.dump({"message": str(e)}))
            except LinkClosed:
                pass
        return None

    # --- operations --------------------------------------------------------

    def op_info(self, ch: Channel, req: dict) -> None:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        ch.send(m.RESULT, m.dump({
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "uptime": uptime,
            "phone_key": fingerprint(public_bytes(self.identity.public_key())),
            "you": fingerprint(ch.peer),
            "protocol": PROTOCOL_VERSION,
        }))

    def op_run(self, ch: Channel, req: dict) -> None:
        argv = req["argv"]
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=SHELL_ENV,
                                    cwd=self.home,
                                    start_new_session=True)
        except OSError as e:
            ch.send(m.ERROR, m.dump({"message": f"cannot run {argv[0]}: {e.strerror}"}))
            return
        pumps = [threading.Thread(target=_pump, args=(fd, ch, kind), daemon=True)
                 for fd, kind in ((proc.stdout.fileno(), m.STDOUT),
                                  (proc.stderr.fileno(), m.STDERR))]
        for p in pumps:
            p.start()
        stdin_open = True
        while proc.poll() is None or any(p.is_alive() for p in pumps):
            try:
                kind, body = ch.recv(0.2)
            except LinkTimeout:
                continue
            except (SessionRestart, HandshakeError, LinkClosed):
                _kill(proc)
                raise
            if kind == m.STDIN and stdin_open:
                try:
                    if body:
                        proc.stdin.write(body)
                        proc.stdin.flush()
                    else:
                        proc.stdin.close()
                        stdin_open = False
                except BrokenPipeError:
                    stdin_open = False
        for p in pumps:
            p.join()
        ch.send(m.EXIT, m.dump({"code": proc.wait()}))

    def op_shell(self, ch: Channel, req: dict) -> None:
        env = dict(SHELL_ENV, TERM=str(req.get("term", "vt100")))
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.home)
            except OSError:
                os.chdir("/")
            os.execvpe("/bin/bash", ["-bash"], env)
            os._exit(127)
        _set_winsize(master, req.get("rows", 24), req.get("cols", 80))
        pump = threading.Thread(target=_pump, args=(master, ch, m.STDOUT), daemon=True)
        pump.start()
        try:
            while pump.is_alive():
                try:
                    kind, body = ch.recv(0.2)
                except LinkTimeout:
                    continue
                if kind == m.STDIN and body:
                    os.write(master, body)
                elif kind == m.RESIZE:
                    size = m.load(body)
                    _set_winsize(master, size["rows"], size["cols"])
        except (SessionRestart, HandshakeError, LinkClosed):
            os.kill(pid, signal.SIGHUP)
            os.waitpid(pid, 0)
            raise
        _, status = os.waitpid(pid, 0)
        os.close(master)
        ch.send(m.EXIT, m.dump({"code": os.waitstatus_to_exitcode(status)}))

    def op_push(self, ch: Channel, req: dict) -> None:
        dest = Path(req["path"])
        mode = int(req.get("mode", 0o644)) & 0o7777
        partial = dest.with_name(f".{dest.name}.flip-partial")
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError as e:
            ch.send(m.ERROR, m.dump({"message": f"cannot write {dest}: {e.strerror}"}))
            return
        h = hashlib.sha256()
        ok = False
        try:
            ch.send(m.RESULT, m.dump({"ready": True}))
            with os.fdopen(fd, "wb") as f:
                while True:
                    kind, body = ch.recv(30)
                    if kind == m.DATA:
                        f.write(body)
                        h.update(body)
                    elif kind == m.END:
                        break
                    else:
                        raise HandshakeError("unexpected message during upload")
                f.flush()
                os.fsync(f.fileno())
            if m.load(body)["sha256"] != h.hexdigest():
                ch.send(m.ERROR, m.dump({"message": "checksum mismatch; file not written"}))
                return
            os.chmod(partial, mode)
            os.replace(partial, dest)
            ok = True
            ch.send(m.RESULT, m.dump({"written": str(dest), "sha256": h.hexdigest()}))
        finally:
            if not ok:
                partial.unlink(missing_ok=True)

    def op_pull(self, ch: Channel, req: dict) -> None:
        src = Path(req["path"])
        try:
            f = open(src, "rb")
            st = os.fstat(f.fileno())
        except OSError as e:
            ch.send(m.ERROR, m.dump({"message": f"cannot read {src}: {e.strerror}"}))
            return
        with f:
            ch.send(m.RESULT, m.dump({"size": st.st_size, "mode": st.st_mode & 0o7777}))
            h = hashlib.sha256()
            while chunk := f.read(m.CHUNK):
                h.update(chunk)
                ch.send(m.DATA, chunk)
        ch.send(m.END, m.dump({"sha256": h.hexdigest()}))

    def op_trust_list(self, ch: Channel, req: dict) -> None:
        hosts = read_trusted(self.trusted_path)
        ch.send(m.RESULT, m.dump({"hosts": [
            {"key": encode_public(h.key), "label": h.label, "fingerprint": fingerprint(h.key),
             "you": h.key == ch.peer} for h in hosts]}))

    def op_trust_add(self, ch: Channel, req: dict) -> None:
        try:
            key = decode_public(req["key"])
        except KeyFileError as e:
            ch.send(m.ERROR, m.dump({"message": str(e)}))
            return
        label = " ".join(str(req.get("label", "")).split())[:80]
        hosts = [h for h in read_trusted(self.trusted_path) if h.key != key]
        hosts.append(TrustedHost(key, label))
        write_trusted(self.trusted_path, hosts)
        log(f"{fingerprint(ch.peer)}: trusted {fingerprint(key)} ({label})")
        ch.send(m.RESULT, m.dump({
            "added": fingerprint(key),
            "phone_key": encode_public(public_bytes(self.identity.public_key())),
        }))

    def op_trust_remove(self, ch: Channel, req: dict) -> None:
        target = str(req["fingerprint"])
        hosts = read_trusted(self.trusted_path)
        keep = [h for h in hosts if not fingerprint(h.key).startswith(target)]
        removed = len(hosts) - len(keep)
        if removed == 0:
            ch.send(m.ERROR, m.dump({"message": f"no trusted computer matches {target}"}))
        elif removed > 1:
            ch.send(m.ERROR, m.dump({"message": f"{target} matches {removed} computers; "
                                                "give more of the fingerprint"}))
        elif not keep:
            ch.send(m.ERROR, m.dump({"message": "refusing to remove the last trusted computer "
                                                "(you would need a card reader to get back in)"}))
        else:
            write_trusted(self.trusted_path, keep)
            log(f"{fingerprint(ch.peer)}: untrusted {target}")
            ch.send(m.RESULT, m.dump({"removed": target}))


OPS = {
    "info": Server.op_info,
    "run": Server.op_run,
    "shell": Server.op_shell,
    "push": Server.op_push,
    "pull": Server.op_pull,
    "trust-list": Server.op_trust_list,
    "trust-add": Server.op_trust_add,
    "trust-remove": Server.op_trust_remove,
}


def _pump(fd: int, ch: Channel, msg_type: int) -> None:
    """Forward everything read from fd as msg_type until EOF."""
    while True:
        try:
            data = os.read(fd, m.CHUNK)
        except OSError:  # EIO on a pty when the shell exits
            data = b""
        if not data:
            return
        try:
            ch.send(msg_type, data)
        except LinkClosed:
            return


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))


def open_serial(path: str) -> int:
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL  # ignore modem control lines
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", default=DEVICE)
    ap.add_argument("--identity", type=Path, default=IDENTITY)
    ap.add_argument("--trusted", type=Path, default=TRUSTED)
    args = ap.parse_args()
    identity = load_private(args.identity)
    log(f"flipd: phone key {fingerprint(public_bytes(identity.public_key()))}, "
        f"{len(read_trusted(args.trusted))} trusted computer(s)")
    while True:
        try:
            fd = open_serial(args.device)
        except OSError as e:
            log(f"waiting for {args.device}: {e.strerror}")
            time.sleep(2)
            continue
        log(f"listening on {args.device}")
        try:
            Server(FdTransport(fd), identity, args.trusted).serve_forever()
        except LinkClosed as e:
            log(f"serial line closed ({e}); reopening")
        finally:
            os.close(fd)
        time.sleep(1)


if __name__ == "__main__":
    main()

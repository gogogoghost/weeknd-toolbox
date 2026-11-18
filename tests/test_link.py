import io
import socket
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flip.link import frame, keys
from flip.link.client import LinkError, Session
from flip.link.frame import FdTransport, FrameReader, encode
from flip.link.keys import TrustedHost, public_bytes
from flip.link.secure import HandshakeError, Rejected, client_handshake
from flip.link.server import Server


class Phone:
    """A flipd server on one end of a socket pair, in a thread."""

    def __init__(self, tmp_path: Path, trusted: list[bytes]):
        self.identity = Ed25519PrivateKey.generate()
        self.key = public_bytes(self.identity.public_key())
        self.trusted = tmp_path / "trusted-hosts"
        keys.write_trusted(self.trusted, [TrustedHost(k, f"host{i}") for i, k in enumerate(trusted)])
        self.host_sock, self.phone_sock = socket.socketpair()
        self.server = Server(FdTransport(self.phone_sock.fileno()), self.identity,
                             self.trusted, home=str(tmp_path))
        self.thread = None

    def serve(self, sessions: int = 1):
        def loop():
            hello = None
            try:
                for _ in range(sessions):
                    hello = self.server.serve_one(hello)
            except frame.LinkClosed:
                pass  # test finished and closed the socket
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def connect(self, identity, expect=None) -> Session:
        t = FdTransport(self.host_sock.fileno())
        return Session(client_handshake(t, identity, expect or self.key))

    def join(self):
        self.thread.join(5)
        assert not self.thread.is_alive()


@pytest.fixture
def host():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def phone(tmp_path, host):
    p = Phone(tmp_path, [public_bytes(host.public_key())])
    yield p
    p.host_sock.close()
    p.phone_sock.close()


def test_info(phone, host):
    phone.serve()
    info = phone.connect(host).call("info")
    assert info["protocol"] == 1 and "kernel" in info
    phone.join()


def test_untrusted_computer_is_rejected(phone):
    phone.serve()
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(Rejected, match="not trusted"):
        phone.connect(stranger)
    phone.join()


def test_wrong_phone_is_detected(phone, host):
    phone.serve()
    impostor_key = public_bytes(Ed25519PrivateKey.generate().public_key())
    with pytest.raises(HandshakeError, match="unexpected identity"):
        phone.connect(host, expect=impostor_key)


def test_run_with_stdin_and_exit_code(phone, host):
    phone.serve()
    out, err = io.BytesIO(), io.BytesIO()
    code = phone.connect(host).run(["sh", "-c", "cat; echo oops >&2; exit 3"],
                                   io.BytesIO(b"hello phone\n"), out, err)
    assert (code, out.getvalue(), err.getvalue()) == (3, b"hello phone\n", b"oops\n")
    phone.join()


def test_run_missing_program(phone, host):
    phone.serve()
    with pytest.raises(LinkError, match="cannot run"):
        phone.connect(host).run(["/nonexistent"], None, io.BytesIO(), io.BytesIO())


def test_push_and_pull(phone, host, tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(256)) * 1000)  # several chunks
    src.chmod(0o750)
    phone.serve(2)
    remote = tmp_path / "remote.bin"
    phone.connect(host).push(src, str(remote))
    assert remote.read_bytes() == src.read_bytes() and remote.stat().st_mode & 0o777 == 0o750
    back = tmp_path / "back.bin"
    phone.connect(host).pull(str(remote), back)
    assert back.read_bytes() == src.read_bytes()
    phone.join()


def test_pull_missing_file(phone, host, tmp_path):
    phone.serve()
    with pytest.raises(LinkError, match="cannot read"):
        phone.connect(host).pull(str(tmp_path / "nope"), tmp_path / "x")


def test_trust_add_list_remove(phone, host):
    other = Ed25519PrivateKey.generate()
    other_pub = public_bytes(other.public_key())
    phone.serve(5)
    added = phone.connect(host).call("trust-add", key=keys.encode_public(other_pub),
                                     label="desktop")
    assert keys.decode_public(added["phone_key"]) == phone.key
    hosts = phone.connect(other).call("trust-list")["hosts"]  # the new one can connect
    assert [h["label"] for h in hosts] == ["host0", "desktop"]
    assert [h["you"] for h in hosts] == [False, True]
    phone.connect(other).call("trust-remove", fingerprint=hosts[0]["fingerprint"])
    with pytest.raises(LinkError, match="last trusted computer"):
        phone.connect(other).call("trust-remove", fingerprint=hosts[1]["fingerprint"])
    with pytest.raises(Rejected):
        phone.connect(host)  # removed
    phone.join()


def test_tampered_message_ends_session(phone, host):
    phone.serve()
    s = phone.connect(host)
    real_write = s.ch.t.write

    def flip_bit(data):
        data = bytearray(data)
        data[20] ^= 1  # inside the ciphertext
        # fix the frame CRC so only the encryption layer can notice
        body = bytes(data[4:-4])
        real_write(bytes(data[:-4]) + __import__("struct").pack(">I", __import__("zlib").crc32(body)))

    s.ch.t.write = flip_bit
    s._request("info")
    phone.join()  # server gave up on the session instead of answering


def test_new_connection_interrupts_running_command(phone, host):
    phone.serve(2)
    s = phone.connect(host)
    s._request("run", argv=["sleep", "30"])
    s.ch.send(0x02, b"")
    info = phone.connect(host).call("info")  # second connection takes over
    assert info["protocol"] == 1
    phone.join()


# --- framing ---------------------------------------------------------------

def _reader_for(data: bytes) -> FrameReader:
    r, w = socket.socketpair()
    w.sendall(data)
    w.close()
    reader = FrameReader(FdTransport(r.fileno()))
    reader._sock = r  # keep the socket (and its fd) alive
    return reader


def test_frames_survive_junk_and_corruption():
    good = encode(frame.KIND_SECURE, b"payload")
    corrupt = bytearray(encode(frame.KIND_SECURE, b"other"))
    corrupt[-1] ^= 0xFF
    reader = _reader_for(b"AT\r\nATZ\r\n" + bytes(corrupt) + b"\x00junk" + good)
    assert reader.read(1) == (frame.KIND_SECURE, b"payload")


def test_fake_header_does_not_stall(monkeypatch):
    monkeypatch.setattr(FrameReader, "PARTIAL_TIMEOUT", 0.2)
    fake = frame.MAGIC + bytes([frame.KIND_SECURE]) + (500_000).to_bytes(4, "big")
    r, w = socket.socketpair()
    reader = FrameReader(FdTransport(r.fileno()))
    reader._sock = (r, w)
    w.sendall(fake + encode(frame.KIND_HANDSHAKE, b"hi"))  # real frame right after
    assert reader.read(5) == (frame.KIND_HANDSHAKE, b"hi")


def test_key_format_roundtrip(tmp_path):
    k = keys.generate(tmp_path / "id.pem")
    assert (tmp_path / "id.pem").stat().st_mode & 0o077 == 0
    pub = public_bytes(k.public_key())
    assert keys.decode_public(keys.encode_public(pub)) == pub
    with pytest.raises(keys.KeyFileError):
        keys.decode_public("ssh-ed25519 AAAA")
    with pytest.raises(FileExistsError):
        keys.generate(tmp_path / "id.pem")
    (tmp_path / "id.pem").chmod(0o644)
    with pytest.raises(keys.KeyFileError, match="readable"):
        keys.load_private(tmp_path / "id.pem")


@pytest.mark.filterwarnings("ignore:This process.*multi-threaded:DeprecationWarning")
def test_shell_runs_in_a_pty(phone, host):
    phone.serve()
    s = phone.connect(host)
    s._request("shell", term="dumb", rows=30, cols=100)
    s.ch.send(0x02, b"stty size; tty >/dev/null && echo has-tty; exit 7\n")
    out = io.BytesIO()
    assert s._drain(out, io.BytesIO()) == 7
    assert b"30 100" in out.getvalue() and b"has-tty" in out.getvalue()
    phone.join()


def test_works_over_a_raw_tty(tmp_path, host):
    """Serial lines mangle bytes unless in raw mode (CR/LF, ^C, ^S...). Use a real pty."""
    import os
    from flip.link.server import open_serial

    master, slave = os.openpty()
    phone_fd = open_serial(os.ttyname(slave))
    os.close(slave)
    p = Phone(tmp_path, [public_bytes(host.public_key())])
    p.server = Server(FdTransport(phone_fd), p.identity, p.trusted, home=str(tmp_path))
    p.host_sock = socket.socket()  # unused; the fixture-less Phone needs these attrs
    p.phone_sock = socket.socket()
    p.serve(2)
    import termios, tty
    tty.setraw(master)
    t = FdTransport(master)
    payload = bytes(range(256)) * 300  # every byte value, incl. \r \n ^C ^S ^Q ^D
    src = tmp_path / "all-bytes"
    src.write_bytes(payload)
    Session(client_handshake(t, host, p.key)).push(src, str(tmp_path / "copy"))
    assert (tmp_path / "copy").read_bytes() == payload
    out = io.BytesIO()
    code = Session(client_handshake(t, host, p.key)).run(["printf", "a\\r\\nb"], None, out,
                                                          io.BytesIO())
    assert (code, out.getvalue()) == (0, b"a\r\nb")
    p.join()
    os.close(master)
    os.close(phone_fd)

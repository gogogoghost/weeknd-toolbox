import hashlib

import pytest

from flip import fetch, lock
from flip.lock import FilePin
from flip.util import FlipError

GOOD = """
[git.linux]
url = "https://example.invalid/linux"
commit = "c438fd63372aea125a063c5c3500f03a2ce552c5"
trust = "pinned"

[file.thing]
url = "https://example.invalid/thing"
sha256 = "%s"
trust = "pinned-on-first-use"
""" % ("a" * 64)


def test_repo_lockfile_loads():
    pins = lock.load()
    assert {"linux", "lk2nd"} <= set(pins.git)
    assert {"busybox", "author-vbmeta"} <= set(pins.file)


def test_lockfile_validation(tmp_path):
    p = tmp_path / "pins.toml"
    p.write_text(GOOD)
    assert lock.load(p).git["linux"].commit.startswith("c438")
    p.write_text(GOOD.replace("c438fd63372aea125a063c5c3500f03a2ce552c5", "c438fd6"))
    with pytest.raises(FlipError, match="40-char"):
        lock.load(p)
    p.write_text(GOOD.replace('"pinned"', '"trust-me"'))
    with pytest.raises(FlipError, match="trust"):
        lock.load(p)
    p.write_text(GOOD + 'oops = "x"\n')
    with pytest.raises(FlipError, match="unknown keys"):
        lock.load(p)


def test_url_override(tmp_path, monkeypatch):
    p = tmp_path / "pins.toml"
    p.write_text(GOOD)
    monkeypatch.setenv("FLIP_URL_LINUX", "https://mirror.invalid/linux")
    assert lock.load(p).git["linux"].url == "https://mirror.invalid/linux"


def _pin(src, data: bytes) -> FilePin:
    src.write_bytes(data)
    return FilePin("x", src.as_uri(), hashlib.sha256(data).hexdigest(), "pinned")


def test_fetch_file_verifies(tmp_path):
    pin = _pin(tmp_path / "src.bin", b"hello")
    dest = tmp_path / "cache" / "x"
    assert fetch.fetch_file(pin, dest).read_bytes() == b"hello"
    assert fetch.fetch_file(pin, dest) == dest  # cached, re-verified
    dest.write_bytes(b"tampered")
    with pytest.raises(FlipError, match="mismatch"):
        fetch.fetch_file(pin, dest)


def test_fetch_file_rejects_bad_download(tmp_path):
    pin = _pin(tmp_path / "src.bin", b"hello")
    (tmp_path / "src.bin").write_bytes(b"evil")  # upstream changed after pinning
    dest = tmp_path / "cache" / "x"
    with pytest.raises(FlipError, match="mismatch"):
        fetch.fetch_file(pin, dest)
    assert not dest.exists() and not dest.with_name("x.partial").exists()


def test_fetch_file_download_failure(tmp_path):
    pin = FilePin("x", (tmp_path / "missing").as_uri(), "0" * 64, "pinned")
    with pytest.raises(FlipError, match="download failed"):
        fetch.fetch_file(pin, tmp_path / "x")

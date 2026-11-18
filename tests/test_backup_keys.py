import json
import shutil
import subprocess

import pytest

from flip import backup, config, keys, profile
from flip.backup import BlockDevice
from flip.util import FlipError

GOOD_DEV = BlockDevice("/dev/sdz", 3623878656, True, profile.TOOLBOX_USB_MODEL, "Linux", False)


def test_check_device_accepts_phone():
    backup.check_device(GOOD_DEV)


@pytest.mark.parametrize("change,msg", [
    ({"model": "Samsung SSD"}, "Wrong device"),
    ({"size": 0}, "Whole eMMC"),
    ({"ro": False}, "read-only"),
    ({"mounted": True}, "mounted"),
])
def test_check_device_rejects(change, msg):
    bad = BlockDevice(**{**GOOD_DEV.__dict__, **change})
    with pytest.raises(FlipError, match=msg):
        backup.check_device(bad)


@pytest.mark.skipif(shutil.which("sfdisk") is None, reason="sfdisk not installed")
def test_finish_extracts_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUPS", tmp_path)
    out = tmp_path / "2026-01-01-000000"
    (out / "parts").mkdir(parents=True)
    img = out / "emmc.img"
    with open(img, "wb") as f:
        f.truncate(8 << 20)
    subprocess.run(["sfdisk", "-q", img], check=True, text=True, input=(
        'label: gpt\nstart=2048, size=128, name="vbmetabak"\nsize=2048, name="boot"\n'))
    with open(img, "r+b") as f:
        f.seek(2048 * 512)
        f.write(b"VBMETA")
    backup.finish(out)

    assert (out / "parts/vbmetabak.img").read_bytes()[:6] == b"VBMETA"
    assert (out / "parts/boot.img").stat().st_size == 2048 * 512
    assert backup.verify(out) == []
    assert backup.list_backups() == [out]
    assert not (out / "emmc.img").stat().st_mode & 0o222  # read-only

    # adopt-vbmeta installs it privately in the config dir
    monkeypatch.setenv("FLIP_CONFIG_DIR", str(tmp_path / "cfg"))
    dest = backup.adopt_vbmeta(out)
    assert dest.read_bytes()[:6] == b"VBMETA" and dest.stat().st_mode & 0o077 == 0


def test_verify_detects_corruption(tmp_path):
    (tmp_path / "parts").mkdir()
    f = tmp_path / "parts/a.img"
    f.write_bytes(b"data")
    (tmp_path / "emmc.img").write_bytes(b"whole")
    backup.write_sums(tmp_path)
    assert backup.verify(tmp_path) == []
    f.write_bytes(b"DATA")
    assert backup.verify(tmp_path) == ["parts/a.img"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_key_created_private_and_exported(tmp_path, isolated_config):
    key = keys.ensure_key()
    assert key.parent == isolated_config
    assert key.stat().st_mode & 0o077 == 0
    assert isolated_config.stat().st_mode & 0o077 == 0
    st = keys.status(tmp_path)
    assert st.exists and st.private and st.matches_phone is None
    keys.record_flashed(st.pkmd_sha256)
    assert keys.status(tmp_path).matches_phone is True

    exported = keys.export(tmp_path / "usb")
    assert exported[0].read_bytes() == key.read_bytes()
    assert exported[0].stat().st_mode & 0o077 == 0
    with pytest.raises(FlipError, match="refusing to overwrite"):
        keys.export(tmp_path / "usb")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_key_permissions_fixed(isolated_config):
    key = keys.ensure_key()
    key.chmod(0o644)
    keys.ensure_key()
    assert key.stat().st_mode & 0o077 == 0

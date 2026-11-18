import pytest

from flip import build, device, flash, keys, profile
from flip.device import Mode
from flip.flash import Target
from flip.util import FlipError


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    """Fake build outputs in a temp dir."""
    out = build.Outputs(
        pkmd=tmp_path / "pkmd.bin", vbmeta=tmp_path / "vbmeta.img",
        recovery=tmp_path / "recovery.img",
    )
    out.pkmd.write_bytes(b"pkmd")
    out.vbmeta.write_bytes(b"v" * 4096)
    out.recovery.write_bytes(b"r" * 1024)
    monkeypatch.setattr(flash, "OUT", out)
    return out


@pytest.mark.parametrize("product,mode", [
    ("WND", Mode.STOCK_FASTBOOT),
    ("lk2nd-msm8952", Mode.LK2ND_FASTBOOT),
    ("something", Mode.OTHER_FASTBOOT),
])
def test_detect_fastboot_modes(fake_fastboot, product, mode):
    fake_fastboot(product)
    assert device.detect_mode() is mode


def test_detect_toolbox_and_none(fake_fastboot, monkeypatch):
    fake_fastboot(None)
    monkeypatch.setattr(device, "usb_ids", lambda: {profile.TOOLBOX_USB_ID})
    assert device.detect_mode() is Mode.TOOLBOX
    monkeypatch.setattr(device, "usb_ids", lambda: set())
    assert device.detect_mode() is Mode.NONE


def test_flash_refuses_lk2nd_fastboot(fake_fastboot, outputs, isolated_config):
    log = fake_fastboot("lk2nd-msm8952")
    with pytest.raises(FlipError, match="lk2nd's fastboot"):
        flash.flash(Target.RECOVERY, confirm=lambda q: True)
    assert "flash" not in log.read_text()


def test_flash_refuses_without_phone(fake_fastboot, outputs, isolated_config, monkeypatch):
    fake_fastboot(None)
    monkeypatch.setattr(device, "usb_ids", lambda: set())
    with pytest.raises(FlipError, match="not in stock fastboot"):
        flash.flash(Target.RECOVERY, confirm=lambda q: True)


def test_flash_aborts_without_confirmation(fake_fastboot, outputs, isolated_config):
    log = fake_fastboot("WND")
    with pytest.raises(FlipError, match="aborted"):
        flash.flash(Target.ALL, confirm=lambda q: False)
    assert "flash" not in log.read_text()


def test_flash_recovery_only(fake_fastboot, outputs, isolated_config):
    log = fake_fastboot("WND")
    flash.flash(Target.RECOVERY, confirm=lambda q: True)
    calls = [l for l in log.read_text().splitlines() if l.startswith(("oem", "flash"))]
    assert calls == ["oem sudo", f"flash recovery {outputs.recovery}"]
    assert keys.flashed_pkmd() is None  # key not flashed, nothing recorded


def test_flash_all_records_key(fake_fastboot, outputs, isolated_config):
    log = fake_fastboot("WND")
    flash.flash(Target.ALL, confirm=lambda q: True)
    calls = [l for l in log.read_text().splitlines() if l.startswith(("oem", "flash"))]
    assert calls == [
        "oem sudo",
        f"flash avb_custom_key {outputs.pkmd}",
        f"flash vbmeta {outputs.vbmeta}",
        f"flash recovery {outputs.recovery}",
    ]
    assert keys.flashed_pkmd() is not None
    assert (isolated_config / "flashed-pkmd.sha256").stat().st_mode & 0o077 == 0


def test_flash_requires_outputs(fake_fastboot, outputs, isolated_config):
    fake_fastboot("WND")
    outputs.recovery.unlink()
    with pytest.raises(FlipError, match="flip build"):
        flash.flash(Target.RECOVERY, confirm=lambda q: True)

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flip import profile, sd
from flip.sd import Card, Ids
from flip.util import FlipError

GOOD = Card("/dev/mmcblk0", 15_931_539_456, "mmc", True, "", [], False)


def card(**change) -> Card:
    return Card(**{**GOOD.__dict__, **change})


def test_accepts_builtin_sd_slot_and_usb_reader():
    sd.check_card(GOOD, 2 << 30)
    sd.check_card(card(path="/dev/sdb", tran="usb", model="Card Reader"), 2 << 30)


@pytest.mark.parametrize("change,msg", [
    ({"model": profile.TOOLBOX_USB_MODEL, "tran": "usb", "size": 3_623_878_656},
     "PHONE'S OWN STORAGE"),
    ({"tran": "nvme", "hotplug": False}, "not an SD"),
    ({"tran": "sata"}, "not an SD"),
    ({"tran": "usb", "hotplug": False}, "not hot-pluggable"),
    ({"size": 1_000_000_000_000}, "between 4 and 128 GB"),
    ({"size": 2_000_000_000}, "between 4 and 128 GB"),
    ({"mounts": ["/"], "holds_system": True}, "this computer's system"),
])
def test_refuses_dangerous_targets(change, msg):
    with pytest.raises(FlipError, match=msg):
        sd.check_card(card(**change), 2 << 30)


def test_refuses_card_smaller_than_image():
    with pytest.raises(FlipError, match="smaller than the image"):
        sd.check_card(card(size=4_000_000_000), 5_000_000_000)


def test_inspect_card_reads_lsblk(monkeypatch):
    data = {"blockdevices": [{"name": "mmcblk0", "type": "disk", "size": 15931539456,
                              "tran": "mmc", "hotplug": True, "model": None,
                              "mountpoints": [None], "children": [
                                  {"name": "mmcblk0p1", "type": "part", "size": 1,
                                   "mountpoints": ["/run/media/u/E72E-B581"]}]}]}

    class R:
        stdout = json.dumps(data)

    monkeypatch.setattr(sd, "run", lambda *a, **k: R)
    c = sd.inspect_card("/dev/mmcblk0")
    assert (c.tran, c.hotplug, c.mounts, c.holds_system) == (
        "mmc", True, ["/run/media/u/E72E-B581"], False)
    sd.check_card(c, 1 << 30)  # mounted is fine: write_card unmounts it first


def test_inspect_card_rejects_partition(monkeypatch):
    class R:
        stdout = json.dumps({"blockdevices": [{"type": "part"}]})

    monkeypatch.setattr(sd, "run", lambda *a, **k: R)
    with pytest.raises(FlipError, match="not a whole disk"):
        sd.inspect_card("/dev/mmcblk0p1")


def test_ids_are_deterministic_and_distinct():
    a, b = Ids.derive("seed"), Ids.derive("seed")
    assert a == b
    assert len(set(a.__dict__.values())) == 5
    assert Ids.derive("other") != a


def test_boot_config_points_at_root_partition():
    ids = Ids.derive("x")
    conf = sd.extlinux_conf(ids, debug=False)
    assert f"root=PARTUUID={ids.root} " in conf and "loglevel=7" not in conf
    assert "linux /Image.gz" in conf and f"fdt /{profile.KERNEL_DTB_NAME}" in conf
    assert "loglevel=7" in sd.extlinux_conf(ids, debug=True)
    tab = sd.fstab(ids)
    assert f"PARTUUID={ids.root}  /      ext4" in tab and "x-systemd.growfs" in tab
    assert f"PARTUUID={ids.boot}  /boot  ext2" in tab


@pytest.mark.skipif(shutil.which("sfdisk") is None or shutil.which("mke2fs") is None,
                    reason="needs sfdisk and mke2fs")
def test_assemble_disk_image(tmp_path):
    ids = Ids.derive("test")
    (tmp_path / "b/extlinux").mkdir(parents=True)
    (tmp_path / "b/extlinux/extlinux.conf").write_text("default flip\n")
    (tmp_path / "r/etc").mkdir(parents=True)
    (tmp_path / "r/etc/hostname").write_text("flip\n")
    boot, root, img = tmp_path / "boot.ext2", tmp_path / "root.ext4", tmp_path / "sd.img"
    sd.make_ext("ext2", boot, 16 << 20, tmp_path / "b", "flipboot", ids.boot_fs, 0)
    sd.make_ext("ext4", root, 32 << 20, tmp_path / "r", "fliproot", ids.root_fs, 0)
    sd.assemble(img, boot, root, ids)

    table = json.loads(subprocess.run(["sfdisk", "--json", img], capture_output=True,
                                      text=True, check=True).stdout)["partitiontable"]
    assert table["label"] == "gpt" and table["id"].lower() == ids.disk
    p1, p2 = table["partitions"]
    assert (p1["name"], p1["uuid"].lower(), p1["start"]) == ("flipboot", ids.boot, 2048)
    assert (p2["name"], p2["uuid"].lower()) == ("fliproot", ids.root)
    assert p2["type"] == sd.TYPE_ROOT_ARM64  # what systemd-repart grows
    # the file systems are where the table says
    with open(img, "rb") as f:
        f.seek(p1["start"] * 512)
        assert f.read(16 << 20) == boot.read_bytes()
    # ext2 for lk2nd: no journal
    out = subprocess.run(["dumpe2fs", "-h", boot], capture_output=True, text=True).stdout
    assert "has_journal" not in out


def test_flipd_package_is_self_contained(tmp_path):
    """The copy installed on the phone must import with only stdlib + cryptography."""
    dest = tmp_path / "flip"
    shutil.copytree(Path(sd.__file__).parent / "link", dest / "link",
                    ignore=shutil.ignore_patterns("__pycache__", "client.py"))
    (dest / "__init__.py").write_text("")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import flip.link.server; "
            "assert not [m for m in sys.modules if m.startswith('flip.') and "
            "not m.startswith('flip.link')], sys.modules.keys()")
    subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path)], check=True)


def test_overlay_has_no_ssh_and_firewall_drops_input():
    overlay = Path(sd.SD_SRC) / "overlay"
    assert not any("ssh" in str(p) for p in overlay.rglob("*"))
    assert "openssh-server" not in profile.SD_PACKAGES and "sudo" not in profile.SD_PACKAGES
    nft = (overlay / "etc/nftables.conf").read_text()
    assert "hook input priority filter; policy drop;" in nft
    assert "policy drop" in nft.split("chain forward")[1].split("}")[0]

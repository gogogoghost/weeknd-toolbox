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
    assert table["label"] == "dos"  # lk2nd can't read GPT
    assert table["id"].lower() == f"0x{ids.disk}"
    p1, p2 = table["partitions"]
    assert (p1["type"], p1["start"]) == ("83", 2048)
    assert p2["type"] == "83" and p2["start"] == 2048 + (16 << 20) // 512
    # Linux names MBR partitions <disk id>-<number>; root=PARTUUID= relies on it
    blkid = subprocess.run(["blkid", "-p", "-o", "export", "-O", str(p2["start"] * 512), img],
                           capture_output=True, text=True).stdout
    assert "LABEL=fliproot" in blkid
    assert ids.root == f"{ids.disk}-02" and ids.boot == f"{ids.disk}-01"
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


def _owners(img: Path, paths: list[str]) -> dict[str, str]:
    out = {}
    for p in paths:
        stat = subprocess.run(["debugfs", "-R", f"stat {p}", str(img)],
                              capture_output=True, text=True).stdout
        line = next(ln for ln in stat.splitlines() if "User:" in ln).split()
        out[p] = f"{line[line.index('User:') + 1]}:{line[line.index('Group:') + 1]}"
    return out


def test_staged_files_are_packed_as_root_with_standard_modes(tmp_path):
    src = tmp_path / "stage"
    (src / "etc/flipd").mkdir(parents=True)
    (src / "etc/flipd").chmod(0o700)
    (src / "etc/flipd/identity.pem").write_text("k")
    (src / "etc/flipd/identity.pem").chmod(0o600)
    (src / "usr/lib").mkdir(parents=True)
    (src / "usr/lib/run.sh").write_text("#!/bin/sh")
    (src / "usr/lib/run.sh").chmod(0o700)
    tar = sd.root_owned_tar(src, tmp_path / "s.tar")
    import tarfile
    with tarfile.open(tar) as tf:
        members = {m.name: m for m in tf}
    assert "." not in members and "./" not in members  # never touches / itself
    assert all(m.uid == 0 and m.gid == 0 and m.uname == "root" for m in members.values())
    assert members["etc"].mode == 0o755 and members["etc/flipd"].mode == 0o755
    assert members["usr/lib/run.sh"].mode == 0o755
    assert members["etc/flipd/identity.pem"].mode == 0o644  # setup.sh tightens it


def test_file_systems_are_root_owned_whoever_builds_them(tmp_path):
    if shutil.which("debugfs") is None:
        pytest.skip("debugfs not installed")
    src = tmp_path / "boot"
    (src / "extlinux").mkdir(parents=True)
    (src / "Image.gz").write_bytes(b"k")
    (src / "extlinux/extlinux.conf").write_text("default flip\n")
    img = tmp_path / "boot.ext2"
    sd.make_ext("ext2", img, 16 << 20, sd.root_owned_tar(src, tmp_path / "b.tar"),
                "flipboot", Ids.derive("x").boot_fs, 0)
    owners = _owners(img, ["/", "/Image.gz", "/extlinux", "/extlinux/extlinux.conf"])
    assert set(owners.values()) == {"0:0"}, owners


def test_refuses_root_fs_with_user_owned_system_files(tmp_path):
    import tarfile

    def build(entries):
        path = tmp_path / "r.tar"
        with tarfile.open(path, "w") as tf:
            for name, uid in entries:
                info = tarfile.TarInfo(name)
                info.type, info.uid, info.gid = tarfile.DIRTYPE, uid, uid
                tf.addfile(info)
        return path

    ok = build([("./", 0), ("./etc", 0), ("./var/log/journal", 0), ("./home/phone", 1000),
                ("./home/phone/.cache", 1000)])
    sd.check_ownership(ok)
    for bad in ("./etc", "./usr/lib", "./", "./boot/Image.gz"):
        with pytest.raises(FlipError, match="owned by a regular user"):
            sd.check_ownership(build([(bad, 1000)]))


@pytest.mark.skipif(shutil.which("sfdisk") is None, reason="needs sfdisk")
def test_root_partition_grows_to_fill_card(tmp_path):
    ids = Ids.derive("grow")
    boot, root = tmp_path / "b.img", tmp_path / "r.img"
    boot.write_bytes(b"\0" * (4 << 20))
    root.write_bytes(b"\0" * (8 << 20))
    img = tmp_path / "sd.img"
    sd.assemble(img, boot, root, ids)
    with open(img, "r+b") as f:  # a bigger "card"
        f.truncate(64 << 20)
    sd.grow_root_partition(str(img), sudo=False)
    table = json.loads(subprocess.run(["sfdisk", "--json", img], capture_output=True,
                                      text=True, check=True).stdout)["partitiontable"]
    p1, p2 = table["partitions"]
    assert p1["size"] == (4 << 20) // 512  # boot untouched
    assert p2["start"] + p2["size"] == (64 << 20) // 512
    assert table["id"].lower() == f"0x{ids.disk}"  # same partition IDs after growing

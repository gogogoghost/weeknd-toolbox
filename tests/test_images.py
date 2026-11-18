import gzip
import shutil
import stat
import subprocess

import pytest

from flip import build, images, initramfs, profile
from flip.util import FlipError


def test_recovery_layout():
    head, boot = b"L" * 1000, b"B" * 5000
    img = images.assemble_recovery(head, boot)
    assert img[:1000] == head
    assert img[1000:profile.LK2ND_SLOT] == b"\0" * (profile.LK2ND_SLOT - 1000)
    assert img[profile.LK2ND_SLOT:] == boot


def test_recovery_size_checks():
    with pytest.raises(FlipError, match="slot"):
        images.assemble_recovery(b"L" * (profile.LK2ND_SLOT + 1), b"B")
    with pytest.raises(FlipError, match="recovery partition"):
        images.assemble_recovery(b"L", b"B" * profile.RECOVERY_SIZE)


def _sample_tree(tmp_path):
    root = tmp_path / "rootfs"
    (root / "lib/firmware").mkdir(parents=True)
    (root / "init").write_text("#!/sbin/sh\n")
    (root / "init").chmod(0o755)
    (root / "lib/firmware/fw.bin").write_bytes(b"\x01\x02")
    (root / "sbin").mkdir()  # generated dir, excluded
    (root / "sbin/junk").write_text("x")
    return root


def test_initramfs_reproducible(tmp_path):
    root = _sample_tree(tmp_path)
    extra = [initramfs.Entry("sbin", stat.S_IFDIR | 0o755),
             initramfs.Entry("sbin/sh", stat.S_IFLNK | 0o777, b"busybox")]
    a = initramfs.build(initramfs.tree_entries(root, exclude={"sbin"}) + extra)
    (root / "init").touch()  # mtime change must not matter
    b = initramfs.build(initramfs.tree_entries(root, exclude={"sbin"}) + extra)
    assert a == b


@pytest.mark.skipif(shutil.which("cpio") is None, reason="cpio not installed")
def test_initramfs_readable_by_cpio(tmp_path):
    root = _sample_tree(tmp_path)
    entries = initramfs.tree_entries(root, exclude={"sbin"}) + [
        initramfs.Entry("sbin", stat.S_IFDIR | 0o755),
        initramfs.Entry("sbin/busybox", stat.S_IFREG | 0o755, b"BB"),
        initramfs.Entry("sbin/sh", stat.S_IFLNK | 0o777, b"busybox"),
    ]
    raw = gzip.decompress(initramfs.build(entries))
    listing = subprocess.run(["cpio", "-itv", "--quiet"], input=raw, capture_output=True,
                             check=True).stdout.decode()
    names = [line.split()[-1] if "->" not in line else line.split()[-3]
             for line in listing.splitlines()]
    assert names == ["init", "lib", "lib/firmware", "lib/firmware/fw.bin",
                     "sbin", "sbin/busybox", "sbin/sh"]
    assert "sbin/sh -> busybox" in listing
    assert "junk" not in listing
    out = tmp_path / "x"
    out.mkdir()
    subprocess.run(["cpio", "-id", "--quiet"], input=raw, cwd=out, check=True)
    assert (out / "lib/firmware/fw.bin").read_bytes() == b"\x01\x02"
    assert (out / "init").stat().st_mode & 0o777 == 0o755


def test_initramfs_rejects_duplicates():
    e = initramfs.Entry("a", stat.S_IFREG | 0o644, b"")
    with pytest.raises(ValueError, match="duplicate"):
        initramfs.cpio_newc([e, e])


def test_toolbox_contains_init_and_applets(tmp_path):
    bb = tmp_path / "busybox"
    bb.write_bytes(b"BB")
    paths = {e.path for e in build.toolbox_entries(bb)}
    assert {"init", "sbin/busybox", "sbin/reboot", "sbin/sync"} <= paths

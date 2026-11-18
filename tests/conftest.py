import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def fake_fastboot(tmp_path, monkeypatch):
    """Install a fake `fastboot` that reports the given product and logs calls."""

    def make(product: str | None, serial: str = "TESTSERIAL") -> Path:
        log = tmp_path / "fastboot-calls.log"
        script = tmp_path / "fastboot"
        devices = f'echo "{serial}\tfastboot"' if product else "true"
        script.write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  devices) {devices} ;;
  getvar)
    case "$2" in
      product) echo "product: {product or ''}" ;;
      serialno) echo "serialno: {serial}" ;;
      all) echo "(bootloader) product:{product or ''}" ;;
    esac
    echo "Finished. Total time: 0.001s" ;;
  *) echo "OKAY" ;;
esac
""")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("FLIP_FASTBOOT", str(script))
        return log

    return make


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    d = tmp_path / "config"
    monkeypatch.setenv("FLIP_CONFIG_DIR", str(d))
    return d

"""Input fingerprints: rebuild an expensive step only when its inputs change."""

from __future__ import annotations

import hashlib
from pathlib import Path

from flip import config


def fingerprint(settings: str, files: list[Path]) -> str:
    h = hashlib.sha256()
    h.update(settings.encode() + b"\0")
    for f in sorted(files):
        h.update(str(f.relative_to(config.REPO)).encode() + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()


class Stamp:
    """Records the fingerprint an output was built from."""

    def __init__(self, name: str):
        self.path = config.STAMPS / name

    def matches(self, fp: str) -> bool:
        try:
            return self.path.read_text().strip() == fp
        except FileNotFoundError:
            return False

    def record(self, fp: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(fp + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

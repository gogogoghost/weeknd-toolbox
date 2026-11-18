"""Fetch pinned inputs, verifying every byte against the lockfile."""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from flip import config
from flip.lock import FilePin, GitPin
from flip.util import FlipError, run, say, sha256_file


def fetch_file(pin: FilePin, dest: Path | None = None) -> Path:
    """Download pin.url to dest (default: cache/<name>) unless present; always verify."""
    dest = dest or config.CACHE / pin.name
    if not dest.exists():
        say(f"fetching {pin.url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        try:
            with urllib.request.urlopen(pin.url, timeout=60) as resp, open(partial, "wb") as f:
                shutil.copyfileobj(resp, f)
        except OSError as e:
            partial.unlink(missing_ok=True)
            raise FlipError(f"download failed: {pin.url}: {e}") from e
        actual = sha256_file(partial)
        if actual != pin.sha256:
            partial.unlink()
            raise FlipError(
                f"SHA-256 mismatch downloading {pin.name}\n"
                f"   expected {pin.sha256}\n   actual   {actual}"
            )
        partial.rename(dest)
    actual = sha256_file(dest)
    if actual != pin.sha256:
        raise FlipError(
            f"SHA-256 mismatch for {dest}\n   expected {pin.sha256}\n   actual   {actual}\n"
            f"   Delete it to re-download, or update pins.toml if intended."
        )
    say(f"OK       {pin.name} (sha256 verified)")
    return dest


def fetch_git(pin: GitPin, dest: Path) -> Path:
    """Shallow-clone pin.url at exactly pin.commit, or verify an existing checkout."""
    if not dest.exists():
        say(f"fetching {pin.url} @ {pin.commit}")
        partial = dest.with_name(dest.name + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        run(["git", "init", "-q", partial])
        run(["git", "-C", partial, "remote", "add", "origin", pin.url])
        run(["git", "-C", partial, "fetch", "-q", "--depth", "1", "origin", pin.commit])
        run(["git", "-C", partial, "-c", "advice.detachedHead=false",
             "checkout", "-q", "FETCH_HEAD"])
        partial.rename(dest)
    head = run(["git", "-C", dest, "rev-parse", "HEAD"], capture=True, quiet=True).stdout.strip()
    if head != pin.commit:
        raise FlipError(
            f"{dest} is at {head}, expected pinned commit {pin.commit}.\n"
            f"   Remove it to re-fetch, or update pins.toml if intended."
        )
    say(f"OK       {pin.name} @ {pin.commit}")
    return dest

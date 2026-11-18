"""Small shared helpers: errors, logging, running commands, hashing."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from pathlib import Path


class FlipError(Exception):
    """An expected failure, reported to the user without a traceback."""


def say(msg: str = "") -> None:
    print(f">> {msg}" if msg else "", flush=True)


def warn(msg: str) -> None:
    print(f">> WARNING: {msg}", file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    cmd: list[str | os.PathLike],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    quiet: bool = False,
    input_: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, echoing it first. Raises FlipError on failure."""
    args = [str(c) for c in cmd]
    if not quiet:
        print("   $ " + shlex.join(args), flush=True)
    full_env = None
    if env:
        full_env = {**os.environ, **env}
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=full_env,
            text=True,
            capture_output=capture,
            input=input_,
        )
    except FileNotFoundError as e:
        raise FlipError(f"command not found: {args[0]}") from e
    if check and result.returncode != 0:
        detail = ""
        if capture:
            detail = "\n" + (result.stdout or "") + (result.stderr or "")
        raise FlipError(f"command failed ({result.returncode}): {shlex.join(args)}{detail}")
    return result


def write_private(path: Path, data: str | bytes) -> None:
    """Write a file readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, mode) as f:
        f.write(data)
    os.chmod(path, 0o600)

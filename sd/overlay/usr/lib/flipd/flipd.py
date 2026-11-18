#!/usr/bin/python3 -I
"""Start flipd (the flip-link server) from its install directory."""
import sys

sys.path.insert(0, "/usr/lib/flipd")
from flip.link.server import main  # noqa: E402

main()

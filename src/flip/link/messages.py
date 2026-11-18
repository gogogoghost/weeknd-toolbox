"""Message types inside an encrypted session.

A session carries exactly one request: the computer sends REQUEST, then the
two sides exchange the messages that operation needs, and the phone ends it
with EXIT (commands) or RESULT/ERROR (everything else).
"""

import json

READY = 0x00     # phone -> computer, once, right after the handshake
REQUEST = 0x01   # computer -> phone: JSON {"op": ..., ...}
STDIN = 0x02     # computer -> phone: bytes for the command; empty = end of input
STDOUT = 0x03    # phone -> computer
STDERR = 0x04    # phone -> computer
EXIT = 0x05      # phone -> computer: JSON {"code": n}
DATA = 0x06      # file contents, either direction
END = 0x07       # end of file: JSON {"sha256": ...}
ERROR = 0x08     # JSON {"message": ...}
RESIZE = 0x09    # computer -> phone: JSON {"rows": r, "cols": c}
RESULT = 0x0A    # JSON

CHUNK = 64 * 1024


def dump(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def load(body: bytes):
    return json.loads(body.decode())

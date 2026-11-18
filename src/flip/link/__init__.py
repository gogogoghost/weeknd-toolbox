"""flip-link: the authenticated, encrypted wired connection to the phone.

This package runs on both ends: `flip` on the computer, and `flipd` on the
phone (copied into the SD card image). It must only import the standard
library and `cryptography`, never the rest of `flip`.

Layers:
  frame.py    byte framing over the USB serial line, resyncs after noise
  secure.py   handshake (mutual Ed25519 authentication, X25519 key exchange)
              and the encrypted message channel
  keys.py     key files and the trusted-hosts list
  server.py   flipd, the phone side
  client.py   the computer side
"""

PROTOCOL_VERSION = 1

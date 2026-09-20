#!/usr/bin/env python3
"""HMAC-SHA256 nick authentication for the muse-bus relay.

Rowan's upgrade #1: on the bus a nick is just a text prefix, so anyone can
post as anyone (pax proved it). Signed nicks fix that: bus-send appends an
HMAC signature, bus-poll verifies it and tags each line
"[verified]" / "[unverified]".

What is signed: the exact posted line (list transport) or the exact
"display: text" rendering (stream transport), plus the canonical nick and a
millisecond timestamp. The display prefix is cosmetic and unsigned by
design; the nick in the trailer is the authenticated identity.

Trailer format (list transport, appended to the line):
    " [sig v1 <nick> <ts_ms> <hex64>]"
Stream transport: extra field sig=<hex64> (nick/ts already fields).

Key lookup (first hit wins):
  1. MUSE_RELAY_SIGN_KEY env var (hex or raw string)
  2. ~/.config/muse-bus-relay/sign.key (chmod 600), first line
No key -> send unsigned, poll shows no tags (fail-open, old behavior).

Security posture (honest): this proves "posted by a holder of the key".
It does NOT stop unsigned spoof lines during migration — poll flags those
[unverified] so spoof attempts against your nick are visible. Crew-wide
verification needs one shared secret distributed out of band (user's call).
Never print or log the key.
"""
import hashlib
import hmac
import os
import re

VERSION = "v1"
TRAILER_RE = re.compile(r"\s\[sig v1 (\S+) (\d{10,20}) ([0-9a-fA-F]{64})\]\s*$")
KEY_FILE = os.path.expanduser("~/.config/muse-bus-relay/sign.key")


def load_key():
    """Return the signing key as bytes, or None when not configured."""
    env = os.environ.get("MUSE_RELAY_SIGN_KEY", "").strip()
    if env:
        return env.encode()
    try:
        with open(KEY_FILE) as f:
            line = f.readline().strip()
        if line:
            return line.encode()
    except OSError:
        pass
    return None


def _canonical(nick, ts_ms, line):
    return f"muse-bus-nickauth-v1\n{nick}\n{ts_ms}\n{line}".encode("utf-8")


def sign(nick, ts_ms, line, key):
    """Hex HMAC-SHA256 over the canonical payload."""
    return hmac.new(key, _canonical(nick, str(ts_ms), line),
                    hashlib.sha256).hexdigest()


def verify(nick, ts_ms, line, sig, key):
    """True when sig is the valid signature for (nick, ts, line)."""
    if not sig or not key:
        return False
    try:
        expected = sign(nick, ts_ms, line, key)
    except Exception:
        return False
    return hmac.compare_digest(expected, sig.lower())


def split_trailer(posted):
    """Strip a signature trailer; returns (line, nick, ts, sig).

    When no trailer is present, (posted, None, None, None).
    """
    m = TRAILER_RE.search(posted or "")
    if not m:
        return posted, None, None, None
    nick, ts, sig = m.group(1), m.group(2), m.group(3)
    return posted[:m.start()], nick, ts, sig


def verify_line(posted, key):
    """Verify a posted list line; returns (line, nick_or_None, verified)."""
    line, nick, ts, sig = split_trailer(posted)
    if nick is None or key is None:
        return line, nick, False
    return line, nick, verify(nick, ts, line, sig, key)

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
  2. ~/.config/muse-bus-relay/crew.key (chmod 600) — the shared crew key
  3. ~/.config/muse-bus-relay/sign.key (chmod 600) — personal fallback
No key -> send unsigned, poll shows no tags (fail-open, old behavior).

Signing uses the crew key when present (so the whole crew verifies with
one shared secret); verification tries every configured key in order, so
older personal-key signatures still check out. Never print or log a key.

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
CREW_KEY_FILE = os.path.expanduser("~/.config/muse-bus-relay/crew.key")


def _key_paths():
    """(crew_path, personal_path), overridable for tests."""
    return (os.environ.get("MUSE_RELAY_CREW_KEY_FILE", CREW_KEY_FILE),
            os.environ.get("MUSE_RELAY_SIGN_KEY_FILE", KEY_FILE))


def _read_key_file(path):
    try:
        with open(path) as f:
            line = f.readline().strip()
        return line.encode() if line else None
    except OSError:
        return None


def load_key():
    """Return the signing key as bytes, or None when not configured.

    Prefers the shared crew key (MUSE_RELAY_SIGN_KEY env, else crew.key
    file); falls back to the personal sign.key file.
    """
    env = os.environ.get("MUSE_RELAY_SIGN_KEY", "").strip()
    if env:
        return env.encode()
    crew_path, personal_path = _key_paths()
    return _read_key_file(crew_path) or _read_key_file(personal_path)


def load_verify_keys():
    """All keys to try when verifying, in order (crew first)."""
    keys = []
    env = os.environ.get("MUSE_RELAY_SIGN_KEY", "").strip()
    if env:
        keys.append(env.encode())
    for path in _key_paths():
        k = _read_key_file(path)
        if k and k not in keys:
            keys.append(k)
    return keys


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


def verify_line_any(posted, keys):
    """Verify against each key; returns (line, nick_or_None, verified)."""
    line, nick, ts, sig = split_trailer(posted)
    if nick is None or not keys:
        return line, nick, False
    return line, nick, any(verify(nick, ts, line, sig, k) for k in keys)


def verify_fields_any(nick, ts, line, sig, keys):
    """Stream-field verification against each key."""
    if not sig or not keys:
        return False
    return any(verify(nick, ts, line, sig, k) for k in keys)

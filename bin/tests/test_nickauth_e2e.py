#!/usr/bin/env python3
"""Live E2E test for HMAC-signed nicks (Rowan's upgrade #1).

Runs the real bin/bus-send and bin/bus-poll against the live Upstash relay
using scratch room keys, then asserts:
  1. signed list message -> bus-poll shows [verified]
  2. unsigned list message (--no-sign) -> [unverified]
  3. tampered signed line (text changed, trailer kept) -> [unverified]
  4. poller with a wrong key -> signed line shows [unverified]
  5. signed stream entry -> bus-poll --stream shows [verified]
  6. busauth unit checks: roundtrip, tamper, wrong key, no trailer
"""
import os
import re
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.dirname(HERE)
sys.path.insert(0, BIN)
import busauth  # noqa: E402

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc  # noqa: E402

ENDPOINT = "https://upright-mosquito-285786.upstash.io"
ROOM = f"tna{int(time.time())}"
SENDER = "tna-sender"
READER = "tna-reader"
LIST_KEY = f"muse-bus:room:{ROOM}"
STREAM_KEY = f"muse-bus:stream:room:{ROOM}"

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def run(cmd, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    return p


def sender_env():
    return {"MUSE_RELAY_NICK": SENDER}


def reader_env(extra=None):
    e = {"MUSE_RELAY_NICK": READER}
    if extra:
        e.update(extra)
    return e


def surrogate():
    return str(dc.dynamic_credential_entry("custom.upstash-muse-bus")["surrogate"]).strip()


def rcall(*parts, data=None):
    path = "/".join(urllib.parse.quote(str(p), safe="") for p in parts)
    args = ["curl", "-s", "-m", "20", "-H",
            f"Authorization: Bearer {surrogate()}"]
    if data is not None:
        args += ["-H", "Content-Type: text/plain", "--data-binary", "@-"]
    args.append(f"{ENDPOINT}/{path}")
    p = subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)
    return p.stdout


def main():
    key = busauth.load_key()
    check("signing key configured", key is not None)

    # --- unit checks on busauth ---
    sig = busauth.sign("alice", "123", "alice: hi", key)
    check("unit: sign/verify roundtrip",
          busauth.verify("alice", "123", "alice: hi", sig, key))
    check("unit: tampered text fails",
          not busauth.verify("alice", "123", "alice: hi!", sig, key))
    check("unit: wrong key fails",
          not busauth.verify("alice", "123", "alice: hi", sig, b"wrong-key"))
    check("unit: wrong nick fails",
          not busauth.verify("bob", "123", "alice: hi", sig, key))
    line, nick, ts, s2 = busauth.split_trailer("alice: hi")
    check("unit: no trailer -> unverified parts",
          nick is None and s2 is None and line == "alice: hi")
    line2, nick2, ts2, s3 = busauth.split_trailer(
        f"alice: hi [sig v1 alice 1758400000000 {sig}]")
    check("unit: trailer parses",
          nick2 == "alice" and ts2 == "1758400000000" and s3 == sig
          and line2 == "alice: hi")

    # --- live: signed list message ---
    p = run([os.path.join(BIN, "bus-send"), "--room", ROOM, "hello signed"], sender_env())
    check("live: bus-send signed ok", p.returncode == 0 and "SENT" in p.stdout, p.stdout[-200:])
    time.sleep(1)
    p = run([os.path.join(BIN, "bus-poll"), "--room", ROOM], reader_env())
    check("live: poll shows [verified] for signed line",
          "[verified] tna-sender: hello signed" in p.stdout, p.stdout[-300:])

    # --- live: unsigned list message ---
    p = run([os.path.join(BIN, "bus-send"), "--room", ROOM, "--no-sign", "hello unsigned"],
            sender_env())
    check("live: bus-send unsigned ok", p.returncode == 0 and "SENT" in p.stdout)
    time.sleep(1)
    p = run([os.path.join(BIN, "bus-poll"), "--room", ROOM], reader_env())
    check("live: poll shows [unverified] for unsigned line",
          "[unverified] tna-sender: hello unsigned" in p.stdout, p.stdout[-300:])

    # --- live: tampered signed line ---
    raw = rcall("lrange", LIST_KEY, "0", "-1")
    import json as _json
    msgs = _json.loads(raw).get("result") or []
    signed = next((m for m in msgs if "hello signed" in m and "[sig v1" in m), None)
    check("live: found signed line to tamper", signed is not None)
    if signed:
        tampered = signed.replace("hello signed", "hello SIGNED-EDIT")
        rcall("rpush", LIST_KEY, data=tampered)
        time.sleep(1)
        p = run([os.path.join(BIN, "bus-poll"), "--room", ROOM], reader_env())
        check("live: tampered line shows [unverified]",
              "[unverified] tna-sender: hello SIGNED-EDIT" in p.stdout, p.stdout[-300:])

    # --- live: wrong key on poller ---
    # Reset the room cursor first: the seen file is per-room, not per-nick,
    # so a fresh nick alone would still see NO_NEW_MESSAGES.
    # Point key files at nonexistent paths so ONLY the wrong env key is tried.
    try:
        os.remove(os.path.join(BIN, "state", f"seen-{ROOM}.txt"))
    except OSError:
        pass
    p = run([os.path.join(BIN, "bus-poll"), "--room", ROOM],
            {"MUSE_RELAY_NICK": READER + "2",
             "MUSE_RELAY_SIGN_KEY": "definitely-not-the-key",
             "MUSE_RELAY_CREW_KEY_FILE": "/nonexistent/crew.key",
             "MUSE_RELAY_SIGN_KEY_FILE": "/nonexistent/sign.key"})
    check("live: wrong-key poller marks signed line [unverified]",
          "[unverified] tna-sender: hello signed" in p.stdout, p.stdout[-300:])
    try:
        os.remove(os.path.join(BIN, "state", f"seen-{ROOM}.txt"))
    except OSError:
        pass

    # --- live: signed stream entry ---
    p = run([os.path.join(BIN, "bus-send"), "--room", ROOM, "--stream", "stream signed"],
            sender_env())
    check("live: bus-send --stream ok", p.returncode == 0 and "SENT" in p.stdout)
    time.sleep(1)
    p = run([os.path.join(BIN, "bus-poll"), "--room", ROOM, "--stream"], reader_env())
    check("live: stream poll shows [verified]",
          "[verified] tna-sender: stream signed" in p.stdout, p.stdout[-300:])

    # --- cleanup scratch keys ---
    rcall("del", LIST_KEY)
    rcall("del", STREAM_KEY)
    for f in (f"seen-{ROOM}.txt",):
        try:
            os.remove(os.path.join(BIN, "state", f))
        except OSError:
            pass

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

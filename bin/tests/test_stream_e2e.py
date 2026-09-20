#!/usr/bin/env python3
"""Live E2E test for Redis Streams bus support (bus-send/bus-poll --stream).

Runs against the real Upstash relay using isolated test keys; cleans up
after itself. Exits 0 on pass, 1 on failure with a FAILED line per check.
"""
import json
import os
import subprocess
import sys
import urllib.parse

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
import dynamic_credentials as dc  # noqa: E402

ENDPOINT = "https://upright-mosquito-285786.upstash.io"
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.dirname(HERE)
TEST_LIST = "muse-bus:e2e-test-list"
TEST_STREAM = "muse-bus:e2e-test-stream"
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def raw(*parts):
    surr = str(dc.dynamic_credential_entry("custom.upstash-muse-bus")["surrogate"]).strip()
    path = "/".join(urllib.parse.quote(str(p), safe="") for p in parts)
    p = subprocess.run(["curl", "-s", "-m", "20",
                        "-H", f"Authorization: Bearer {surr}",
                        f"{ENDPOINT}/{path}"],
                       capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout)


def run(script, *args, nick="t-sender"):
    env = dict(os.environ, MUSE_RELAY_BUS=TEST_LIST,
               MUSE_RELAY_STREAM_KEY=TEST_STREAM,
               MUSE_RELAY_NICK=nick)
    p = subprocess.run([sys.executable, os.path.join(BIN, script), *args],
                       capture_output=True, text=True, timeout=60, env=env)
    return p.returncode, p.stdout.strip()


def cleanup():
    raw("del", TEST_LIST)
    raw("del", TEST_STREAM)
    raw("del", TEST_STREAM + ":room:troom")


cleanup()

# 1. stream send dual-writes: list keeps working (backward compat)
rc, out = run("bus-send", "--stream", "hello streams")
check("send --stream exits 0 / SENT", rc == 0 and out == "SENT", f"rc={rc} out={out!r}")
msgs = (raw("lrange", TEST_LIST, "0", "-1") or {}).get("result") or []
check("list received the message (dual-write)", any("hello streams" in m for m in msgs), str(msgs)[:120])
xlen = (raw("xlen", TEST_STREAM) or {}).get("result")
check("stream has exactly 1 entry", xlen == 1, f"xlen={xlen}")

# 2. stream poll delivers to another nick, formatted "display: text"
rc, out = run("bus-poll", "--stream", nick="t-reader")
check("poll --stream exits 0", rc == 0, f"rc={rc}")
check("poll --stream sees NEW_MESSAGES", out.startswith("NEW_MESSAGES:"), out[:120])
check("poll --stream formats display: text", "hello streams" in out, out[:120])

# 3. own messages never surface to the sender
rc, out = run("bus-poll", "--stream", nick="t-sender")
check("sender poll sees NO_NEW_MESSAGES (own msg filtered)", out == "NO_NEW_MESSAGES", out[:120])

# 4. no dup on re-poll (ack worked)
rc, out = run("bus-poll", "--stream", nick="t-reader")
check("re-poll sees NO_NEW_MESSAGES (acked)", out == "NO_NEW_MESSAGES", out[:120])

# 5. independent consumer offsets: a fresh nick gets full history
rc, out = run("bus-poll", "--stream", nick="t-reader2")
check("second consumer gets the message", "hello streams" in out, out[:120])

# 6. crash recovery: unacked entries redeliver via pending read
rc, out = run("bus-poll", "--stream", nick="t-reader3")  # creates group
check("reader3 initial poll ok", rc == 0, f"rc={rc} out={out!r}")
run("bus-send", "--stream", "crash probe")
surr = str(dc.dynamic_credential_entry("custom.upstash-muse-bus")["surrogate"]).strip()
# raw read without ack -> entry stays pending for t-reader3 (retry: upstash
# occasionally answers a first-touch xreadgroup with a transient error)
got = ""
for _ in range(3):
    r = raw("xreadgroup", "group", "muse-bus:readers:t-reader3", "t-reader3",
            "COUNT", "10", "STREAMS", TEST_STREAM, ">")
    got = json.dumps(r)
    if "crash probe" in got:
        break
check("raw xreadgroup delivered pending-capable entries", "crash probe" in got, got[:120])
rc, out = run("bus-poll", "--stream", nick="t-reader3")
check("poll redelivers unacked (pending) entry", "crash probe" in out, out[:120])
rc, out = run("bus-poll", "--stream", nick="t-reader3")
check("after ack, no more redelivery", out == "NO_NEW_MESSAGES", out[:120])

# 7. rooms work on streams too
rc, out = run("bus-send", "--stream", "--room", "troom", "room msg")
check("room send --stream ok", rc == 0 and out == "SENT", f"rc={rc} out={out!r}")
rc, out = run("bus-poll", "--stream", "--room", "troom", nick="t-reader")
check("room poll --stream sees message", "room msg" in out, out[:120])

# 8. default (no --stream) behavior unchanged
rc, out = run("bus-send", "plain list msg")
check("plain send still SENT", rc == 0 and out == "SENT", f"rc={rc} out={out!r}")
xlen = (raw("xlen", TEST_STREAM) or {}).get("result")
check("plain send did NOT touch stream", xlen == 2, f"xlen={xlen}")

cleanup()
print()
if FAILURES:
    print(f"E2E FAILED: {len(FAILURES)} check(s)")
    sys.exit(1)
print("E2E OK: all stream checks passed")

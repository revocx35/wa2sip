"""Stand-in for agent/index.js speaking the same JSON-lines protocol (no browser).

Started by AgentRuntime as `<node_bin> index.js` with node_bin = python: this file is copied
to <tmp>/index.js by the test.
"""
import json
import os
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


assert os.environ.get("PULSE_SINK", "").endswith("_spk"), "agent must get its PulseAudio sink"
send({"event": "state", "state": "loading", "detail": "starting browser"})
send({"event": "qr", "qr": "data:image/png;base64,AAAA"})
if os.environ.get("FAKE_AGENT_CRASH") and not os.path.exists(os.environ["FAKE_AGENT_CRASH"]):
    open(os.environ["FAKE_AGENT_CRASH"], "w").close()
    sys.exit(5)
send({"event": "me", "me": {"id": "490000@c.us", "number": "490000"}})
send({"event": "state", "state": "ready"})
send({"event": "log", "level": "info", "msg": "fake agent ready"})
for line in sys.stdin:
    msg = json.loads(line)
    cmd, args, rid = msg["cmd"], msg.get("args", {}), msg["id"]
    if cmd == "dial":
        if args["to"].endswith("9999"):
            send({"id": rid, "ok": False, "code": "not_on_whatsapp", "error": "this number is not on WhatsApp"})
            continue
        snap = {"id": "C1", "state": 1, "outgoing": True, "peer": {"jid": args["to"] + "@c.us", "number": args["to"]}}
        send({"event": "call", "call": snap})
        send({"id": rid, "ok": True, "result": snap})
        time.sleep(0.05)
        send({"event": "call", "call": {**snap, "state": 6, "everConnected": True}})
    elif cmd == "hangup":
        send({"event": "call", "call": {"id": "C1", "state": 0, "outgoing": True, "everConnected": True,
                                        "peer": {"number": "4911"}}})
        send({"id": rid, "ok": True, "result": True})
    elif cmd == "contacts":
        send({"id": rid, "ok": True, "result": [{"id": "4911@c.us", "number": "4911", "name": "A"}]})
    elif cmd == "slow":
        pass                                  # never answers
    elif cmd == "shutdown":
        send({"id": rid, "ok": True, "result": True})
        sys.exit(0)
    else:
        send({"id": rid, "ok": False, "code": "unknown_command", "error": "unknown command " + cmd})

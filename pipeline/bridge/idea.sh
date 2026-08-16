#!/usr/bin/env bash
# Give the planning loop an idea. Writes an inquiry it will read next iteration.
#
#   idea.sh "the approval console should show why something was rejected"
#   idea.sh "gate feels slow" "no idea if it's the tests or the scratch copy"
#
# Second argument is what you do NOT know — the loop treats that as the research
# question. Omit it and it records that you didn't say, which is honest and
# still useful; it just makes the scope wider.
#
# This is the same channel the loops use to raise questions to each other, so an
# idea from you and an observation from a loop arrive identically and compete on
# merit rather than on who sent them.
set -uo pipefail

IDEA="${1:?usage: idea.sh \"<what you noticed>\" [\"what you do not know\"]}"
UNKNOWN="${2:-raised by the operator; they did not state what they are unsure of — treat the scope as unestablished}"
ROOT="${LOOPQUEUE:-$HOME/OmniAgentOS/var/loopqueue}"
BRIDGE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"

[ -d "$ROOT/inquiries" ] || { echo "no queue at $ROOT"; exit 1; }

python3 - "$IDEA" "$UNKNOWN" "$ROOT" "$BRIDGE_DIR" <<'PY'
import hashlib, json, os, sys, time
from pathlib import Path

idea, unknown, root, bridge_dir = sys.argv[1:5]
sys.path.insert(0, bridge_dir)
from ledger_write import append_event

def canon(o):
    if isinstance(o, dict):
        return "{" + ",".join(f"{canon(str(k))}:{canon(v)}" for k, v in sorted(o.items())) + "}"
    if isinstance(o, list):
        return "[" + ",".join(canon(v) for v in o) + "]"
    if isinstance(o, str):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    if o is None: return "null"
    if o is True: return "true"
    if o is False: return "false"
    return str(int(o)) if float(o).is_integer() else repr(o)

# NO timestamp in payload: the id is its hash, so a clock would make the same
# idea a new artifact every time and the loop would research it repeatedly.
payload = {"area": idea[:80], "observation": idea,
           "why_not_a_fix": unknown, "urgency": "normal"}
ident = "sha256:" + hashlib.sha256(canon(payload).encode()).hexdigest()
stem = ident.replace(":", "_")

for d in ("inquiries", "rejected", "parked"):
    if os.path.exists(f"{root}/{d}/{stem}.json"):
        print(f"  already raised (and currently in {d}/) — not duplicating")
        raise SystemExit(0)

now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
art = {"contract": "v1.1", "id": ident, "kind": "inquiry", "title": idea[:200],
       "created_at": now, "producer": {"role": "external", "actor": "operator"},
       "payload": payload}

tmp = f"{root}/inquiries/{stem}.json.tmp"
with open(tmp, "w") as fh:
    json.dump(art, fh, indent=2); fh.flush(); os.fsync(fh.fileno())
os.rename(tmp, f"{root}/inquiries/{stem}.json")

append_event(Path(root), {"ts": now, "role": "external", "event": "inquired",
                          "id": ident, "actor": "operator"})

print(f"  queued {ident[7:19]}  — the loop reads inquiries/ FIRST, so this is next up")
PY

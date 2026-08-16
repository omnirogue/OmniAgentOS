#!/usr/bin/env bash
# Tell the Implementer what to do next. Operator -> Implementer, direct.
#
#   directive.sh "land integration/train-0808b, it is verified"
#   directive.sh --priority "close system.py:912 before anything else"
#
# This is NOT idea.sh. idea.sh writes an inquiry the PLANNER reads and may
# research for hours. A directive is read by the IMPLEMENTER at the top of its
# next iteration and is meant to change what it does now.
#
# It is an instruction, not a command: the Implementer still admits, gates and
# refuses on merit. A directive cannot buy a candidate a skipped gate — that is
# deliberate, and it is why this channel is safe to hand an operator.
set -uo pipefail
PRIORITY=0
[ "${1:-}" = "--priority" ] && { PRIORITY=1; shift; }
TEXT="${1:?usage: directive.sh [--priority] \"<what to do>\"}"
ROOT="${LOOPQUEUE:-$HOME/OmniAgentOS/var/loopqueue}"
BRIDGE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
[ -d "$ROOT/directives" ] || mkdir -p "$ROOT/directives"

python3 - "$TEXT" "$ROOT" "$PRIORITY" "$BRIDGE_DIR" <<'PY'
import hashlib, json, os, sys, time
from pathlib import Path

text, root, prio, bridge_dir = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
sys.path.insert(0, bridge_dir)
from ledger_write import append_event
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
# id is a content hash, so re-issuing the same directive does not duplicate it;
# the timestamp lives OUTSIDE the hashed payload for exactly that reason.
payload = {"directive": text, "priority": "now" if prio else "normal"}
ident = "sha256:" + hashlib.sha256(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
stem = ident.replace(":", "_")
path = os.path.join(root, "directives", stem + ".json")
if os.path.exists(path):
    print("  already issued and not yet consumed — not duplicating"); raise SystemExit(0)
art = {"contract": "v1.1", "id": ident, "kind": "directive", "title": text[:200],
       "created_at": now, "producer": {"role": "external", "actor": "operator"},
       "payload": payload}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(art, fh, indent=2); fh.flush(); os.fsync(fh.fileno())
os.rename(tmp, path)
append_event(Path(root), {"ts": now, "role": "external", "event": "directed",
                          "id": ident, "actor": "operator"})
print(f"  directive {ident[7:19]} queued{' (PRIORITY)' if prio else ''}")
print("  the Implementer reads directives/ at the top of its next iteration")
PY

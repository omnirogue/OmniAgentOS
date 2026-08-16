# work-drive-sync — ~/Work ⇄ Google Drive mirror automation

Canonical home of the Work⇄Drive sync pair (adopted from the 2026-08-01 mac-hygiene
session + org upgrade; adversarially reviewed pre-ship). Live copies run from
`~/bin/`; this directory is the versioned source of truth.

## What it does

- **Push** (`work-drive-sync.sh`, launchd `com.owner.work-drive-sync`, daily 08:30):
  one-way **additive** rsync of `~/` → `My Drive/Work/`
  (`owner@acmeuni.example`). Never deletes on the Drive side, so
  Drive:/Work is a safe superset (Google-native docs, collaborator uploads).
  Excludes repo/build churn and, by policy, Finance/secrets/Personal — see the
  script header and `~/README.md` for the exact lists.
- **Pull** (`work-drive-pull.sh`, launchd `com.owner.work-drive-pull`, daily 07:00):
  promotes items dropped in any `Drive:/Work/**/_FromDrive/` folder into the
  matching local path — copy, byte-verify, then move the Drive copy to its final
  mirrored place. Never overwrites local data; 3-strike backoff; containment-checked
  under `~/Work`. The push defers if the pull is still running.

Operator doc: `~/README.md` · agent-facing mapping: `vault/sources/work-drive-mapping.md`.

## Files

| file | role |
|---|---|
| `work-drive-sync.sh` | push script (deployed to `~/bin/`) |
| `work-drive-pull.sh` | pull script (deployed to `~/bin/`) |
| `com.owner.work-drive-sync.plist.template` | launchd template (`__HOME__` placeholder) |
| `com.owner.work-drive-pull.plist.template` | launchd template (`__HOME__` placeholder) |
| `install.sh` | copies scripts to `~/bin`, renders + (re)loads both plists; idempotent |

## Install / update

```
zsh scripts/work-drive-sync/install.sh
```

## Verify

```
launchctl list | grep work-drive              # both jobs, last exit 0
~/bin/work-drive-sync.sh --dry-run            # push preview
DRY_RUN=1 ~/bin/work-drive-pull.sh            # pull preview
tail ~/Library/Logs/work-drive-sync.log ~/Library/Logs/work-drive-pull.log
```

Pull undo-trail: `~/Library/Logs/work-drive-pull-manifest.tsv` (TSV:
ts/status/drive_side/local_side/note; statuses PULLED/NATIVE/DUP/REPLACED/
SKIP/REFUSED/BACKOFF/FAILED).

## Editing rules

Edit here, then run `install.sh` to deploy — do not hand-edit `~/bin` copies.
These files are intentionally untracked until committed through the merge gate
(same status as `scripts/health-sentinel/`).

#!/usr/bin/env bash
# Refresh the local CopywritingBrainVault mirror from the iCloud master.
#
# Master (the operator edits in Obsidian): ~/Library/Mobile Documents/com~apple~CloudDocs/Media Buying/CopywritingBrainVault
# Mirror (what agents read; registered as the `copywriting-vault` mount): ~/KnowledgeVaults/CopywritingBrainVault
#
# The mirror exists because iCloud can evict ("dataless") files at any time,
# which would stall or silently blind an agent mid-read. rsync only sends
# deltas, so re-running is cheap. --delete keeps removals in sync; .DS_Store
# and .obsidian workspace churn are excluded (plugin/theme config stays).
#
# usage: scripts/copyvault/sync-copyvault.sh [--dry-run]

set -euo pipefail

SRC="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Media Buying/CopywritingBrainVault/"
DST="$HOME/KnowledgeVaults/CopywritingBrainVault/"

if [ ! -d "$SRC" ]; then
  echo "ERROR: iCloud master not found at $SRC" >&2
  exit 1
fi

mkdir -p "$DST"

FLAGS=(-a --delete --exclude '.DS_Store' --exclude '.obsidian/workspace*')
[ "${1:-}" = "--dry-run" ] && FLAGS+=(-n -v)

rsync "${FLAGS[@]}" "$SRC" "$DST"

COUNT=$(find "$DST" -name '*.md' | wc -l | tr -d ' ')
echo "copyvault mirror refreshed: $COUNT markdown notes at $DST"

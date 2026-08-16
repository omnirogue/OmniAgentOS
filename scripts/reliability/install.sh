#!/bin/sh
# Render OmniAgentOS reliability launchd jobs without touching live launchd.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
# Rendering is safe by default: never imply installation into live launchd.
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
mkdir -p "$TARGET_DIR"

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12)" >&2
  exit 1
fi

render() {
  label=$1
  case "$label" in
    com.omniagentos.reliability-watch|\
    com.omniagentos.reliability-audit|\
    com.omniagentos.reliability-daily|\
    com.omniagentos.reliability-weekly) ;;
    *) echo "error: refusing unknown reliability label: $label" >&2; exit 1 ;;
  esac
  template="$SCRIPT_DIR/$label.plist.template"
  target="$TARGET_DIR/$label.plist"
  sed -e "s|{{PYBIN}}|$PYBIN|g" -e "s|{{ROOT}}|$ROOT_DIR|g" \
    "$template" > "$target"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$target"
  fi
  echo "Rendered $target"
}

render com.omniagentos.reliability-watch
render com.omniagentos.reliability-audit
render com.omniagentos.reliability-daily
render com.omniagentos.reliability-weekly

echo "NOT loaded. These jobs may recover or mutate local state."
echo "Review each exact product-scoped plist and load only the intended label."

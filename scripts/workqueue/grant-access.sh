#!/usr/bin/env bash
# grant-access.sh — implements FOR-OPERATOR/MACHINE-ACCESS.md on a spare/worker Mac
# or Linux box: a dedicated `omniworker` account, per-person SSH keys, and
# (deliberately opt-in) sshd hardening.
#
# Never shares a private key. Never touches ~/.config/omni/connections.env.
# Every action here is additive/idempotent except --harden, which changes
# live sshd policy and REQUIRES --yes.
#
# Usage:
#   grant-access.sh --create-worker-account [--user omniworker]
#   grant-access.sh --add-key '<pubkey line>' [--user omniworker]
#   grant-access.sh --harden --yes
#
# Flags combine: `grant-access.sh --create-worker-account --add-key '...' --user omniworker`
set -euo pipefail

WORKER_USER="omniworker"
DO_CREATE=0
DO_ADD_KEY=0
PUBKEY=""
DO_HARDEN=0
YES=0

usage() {
  cat >&2 <<'EOF'
usage: grant-access.sh [--create-worker-account] [--add-key '<pubkey line>']
                        [--user NAME] [--harden] [--yes]

  --create-worker-account   Create a Standard (non-admin) account (darwin:
                             sysadminctl; linux: useradd -m) for the worker
                             to run as. Idempotent — no-ops if it exists.
  --add-key '<pubkey line>' Append a public key line to that account's
                             ~/.ssh/authorized_keys (mkdir/chmod discipline
                             applied). One person's key per invocation.
  --user NAME                Account name. Default: omniworker.
  --harden                   Set PasswordAuthentication no and
                             PermitRootLogin no in sshd_config and reload
                             sshd. NEVER the default — must be combined with
                             --yes, and REFUSES when the current session is
                             root (see below).
  --yes                      Required alongside --harden; without it,
                             --harden only prints what it WOULD change.

--harden safety: both Vultr Linux boxes in this fleet are root-access today
(MACHINE-FLEET-PLAN.md). Running --harden from a root session would turn off
PermitRootLogin and could lock out the very session running it — "sawing off
the branch we sit on." grant-access.sh refuses to apply --harden when it
detects the invoking session is root. Create and verify a non-root login
first (--create-worker-account, --add-key, confirm you can `ssh` in as that
user), THEN run --harden from a non-root session.
EOF
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --create-worker-account) DO_CREATE=1; shift ;;
    --add-key) DO_ADD_KEY=1; PUBKEY="$2"; shift 2 ;;
    --user) WORKER_USER="$2"; shift 2 ;;
    --harden) DO_HARDEN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage ;;
    *) echo "grant-access.sh: unknown argument: $1" >&2; usage ;;
  esac
done

if [ "$DO_CREATE" -eq 0 ] && [ "$DO_ADD_KEY" -eq 0 ] && [ "$DO_HARDEN" -eq 0 ]; then
  usage
fi

die() { echo "grant-access.sh: $*" >&2; exit 1; }

OS_NAME="$(uname -s)"

# ---------------------------------------------------------------------------
# --create-worker-account
# ---------------------------------------------------------------------------
if [ "$DO_CREATE" -eq 1 ]; then
  if id "$WORKER_USER" >/dev/null 2>&1; then
    echo "grant-access.sh: account '${WORKER_USER}' already exists — skipping create." >&2
  elif [ "$OS_NAME" = "Darwin" ]; then
    [ "$(id -u)" -eq 0 ] || die "creating an account on darwin requires sudo. Re-run: sudo $0 --create-worker-account --user ${WORKER_USER}"
    echo "grant-access.sh: creating Standard account '${WORKER_USER}' (sysadminctl)" >&2
    NEXT_UID="$(dscl . -list /Users UniqueID | awk '{print $2}' | sort -n | tail -1 | awk '{print $1+1}')"
    sysadminctl -addUser "$WORKER_USER" \
      -fullName "Workqueue worker" \
      -UID "$NEXT_UID" \
      -home "/Users/${WORKER_USER}" \
      -shell /bin/bash \
      -password - \
      2>&1 | grep -v '^$' || true
    # sysadminctl -addUser creates an admin-capable account by default in some
    # macOS versions when no -admin/-nonadmin flag is passed; be explicit.
    dscl . -delete "/Groups/admin" GroupMembership "$WORKER_USER" >/dev/null 2>&1 || true
    echo "grant-access.sh: created '${WORKER_USER}' as a Standard (non-admin) account." >&2
  elif [ "$OS_NAME" = "Linux" ]; then
    [ "$(id -u)" -eq 0 ] || die "creating an account on linux requires root. Re-run with sudo."
    useradd -m -s /bin/bash "$WORKER_USER" \
      || die "useradd failed for ${WORKER_USER}"
    echo "grant-access.sh: created '${WORKER_USER}' (useradd -m)." >&2
  else
    die "unsupported OS: $OS_NAME"
  fi
fi

# ---------------------------------------------------------------------------
# --add-key
# ---------------------------------------------------------------------------
if [ "$DO_ADD_KEY" -eq 1 ]; then
  [ -n "$PUBKEY" ] || die "--add-key requires a public key line argument"
  case "$PUBKEY" in
    ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*\ *)
      ;;
    *)
      die "--add-key argument does not look like an OpenSSH public key line (expected 'ssh-ed25519 AAAA... comment'). Never pass a private key here."
      ;;
  esac
  id "$WORKER_USER" >/dev/null 2>&1 || die "account '${WORKER_USER}' does not exist — run --create-worker-account first"

  if [ "$OS_NAME" = "Darwin" ]; then
    HOME_DIR="$(dscl . -read "/Users/${WORKER_USER}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  else
    HOME_DIR="$(getent passwd "$WORKER_USER" | cut -d: -f6)"
  fi
  [ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || die "could not resolve home directory for ${WORKER_USER}"

  SSH_DIR="${HOME_DIR}/.ssh"
  AUTH_KEYS="${SSH_DIR}/authorized_keys"

  RUN_AS=""
  if [ "$(id -u)" -eq 0 ]; then
    RUN_AS="sudo -u ${WORKER_USER}"
  elif [ "$(id -un)" != "$WORKER_USER" ]; then
    die "not root and not logged in as ${WORKER_USER} — re-run as root (sudo) or as ${WORKER_USER}"
  fi

  ${RUN_AS} mkdir -p "$SSH_DIR"
  ${RUN_AS} chmod 700 "$SSH_DIR"
  ${RUN_AS} touch "$AUTH_KEYS"
  ${RUN_AS} chmod 600 "$AUTH_KEYS"

  if grep -qF "$PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
    echo "grant-access.sh: key already present in ${AUTH_KEYS} — no-op." >&2
  else
    echo "$PUBKEY" | ${RUN_AS} tee -a "$AUTH_KEYS" >/dev/null
    echo "grant-access.sh: appended key to ${AUTH_KEYS}." >&2
  fi

  if [ "$(id -u)" -eq 0 ] && [ "$OS_NAME" = "Darwin" ]; then
    chown -R "${WORKER_USER}:staff" "$SSH_DIR"
  elif [ "$(id -u)" -eq 0 ]; then
    chown -R "${WORKER_USER}:${WORKER_USER}" "$SSH_DIR"
  fi
fi

# ---------------------------------------------------------------------------
# --harden — never default, refuses under root, requires --yes
# ---------------------------------------------------------------------------
if [ "$DO_HARDEN" -eq 1 ]; then
  if [ "$(id -u)" -eq 0 ]; then
    die "refusing --harden: this session is root. Both Vultr Linux boxes are root-access today (MACHINE-FLEET-PLAN.md) — turning off PermitRootLogin from a root session risks locking out the session running it. Create/verify a non-root login first (--create-worker-account, --add-key, then SSH in as that user from a SEPARATE terminal), and run --harden from that non-root session."
  fi

  if [ "$OS_NAME" = "Darwin" ]; then
    SSHD_CONFIG="/etc/ssh/sshd_config"
  else
    SSHD_CONFIG="/etc/ssh/sshd_config"
  fi

  echo "grant-access.sh: --harden will change ${SSHD_CONFIG}:" >&2
  echo "  PasswordAuthentication no" >&2
  echo "  PermitRootLogin no" >&2
  echo "and reload sshd. This makes key-only login mandatory — confirm you (and everyone" >&2
  echo "who needs access) already has a working key-based login before proceeding." >&2

  if [ "$YES" -ne 1 ]; then
    echo "grant-access.sh: dry-run only (pass --yes to apply)." >&2
    exit 0
  fi

  [ -w "$SSHD_CONFIG" ] || command -v sudo >/dev/null 2>&1 || die "need write access to ${SSHD_CONFIG} (sudo not found)"

  SUDO=""
  [ -w "$SSHD_CONFIG" ] || SUDO="sudo"

  $SUDO cp "$SSHD_CONFIG" "${SSHD_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"

  if [ "$OS_NAME" = "Darwin" ]; then
    $SUDO sed -i '' 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
    $SUDO sed -i '' 's/^#*PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
    grep -q '^PasswordAuthentication no' "$SSHD_CONFIG" || echo "PasswordAuthentication no" | $SUDO tee -a "$SSHD_CONFIG" >/dev/null
    grep -q '^PermitRootLogin no' "$SSHD_CONFIG" || echo "PermitRootLogin no" | $SUDO tee -a "$SSHD_CONFIG" >/dev/null
    $SUDO launchctl kickstart -k system/com.openssh.sshd
  else
    $SUDO sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
    $SUDO sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "$SSHD_CONFIG"
    grep -q '^PasswordAuthentication no' "$SSHD_CONFIG" || echo "PasswordAuthentication no" | $SUDO tee -a "$SSHD_CONFIG" >/dev/null
    grep -q '^PermitRootLogin no' "$SSHD_CONFIG" || echo "PermitRootLogin no" | $SUDO tee -a "$SSHD_CONFIG" >/dev/null
    ($SUDO systemctl reload sshd 2>/dev/null || $SUDO systemctl reload ssh 2>/dev/null) \
      || die "sshd config updated but reload failed — reload manually before disconnecting this session"
  fi

  echo "grant-access.sh: hardening applied. sshd_config backed up alongside the original." >&2
fi

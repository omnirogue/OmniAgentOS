#!/bin/bash
# pg-auth-setup.sh — hardening script for PostgreSQL knowledge subsystem authentication.
# Implements R2-3/R2-4/R2-6: password generation, role setup, pg_hba flip to scram-sha-256,
# self-testing that passwordless superuser access is blocked.
#
# Usage:
#   scripts/knowledge/pg-auth-setup.sh [--dry-run]
#
# The script is idempotent — safe to re-run. It will only make changes if not already applied.

set -eu

DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done

log() { echo "[pg-auth-setup] $@" >&2; }
die() { log "ERROR: $@"; exit 1; }

# Check that we're in a reasonable directory
if [ ! -f pyproject.toml ]; then
  die "pyproject.toml not found; run from repo root"
fi

# Determine the PostgreSQL version and config location
PG_VERSION=$(psql --version | awk '{print $3}' | cut -d. -f1) || die "Could not determine PostgreSQL version"
log "PostgreSQL version: $PG_VERSION"

# Find pg_hba.conf
PG_CONFIGDIR=$(psql -t -c "SHOW config_directory") || die "Could not find PostgreSQL config directory"
HBA_PATH="$PG_CONFIGDIR/pg_hba.conf"
[ -f "$HBA_PATH" ] || die "pg_hba.conf not found at $HBA_PATH"
log "pg_hba.conf location: $HBA_PATH"

# Check if already flipped (look for scram-sha-256 in local/127.0.0.1 lines)
if grep -q "^local.*scram-sha-256" "$HBA_PATH" && grep -q "^host.*127.0.0.1.*scram-sha-256" "$HBA_PATH"; then
  log "pg_hba.conf already configured with scram-sha-256 — exiting (already applied)"
  exit 0
fi

log "Generating random passwords..."

# Generate strong passwords
GEN_PASSWD() { openssl rand -base64 32 | tr '\n' '\0' | od -An -tx1 | tr ' ' '\n' | head -16 | tr -d '\n'; echo; }

AGENT_PASSWD=$(GEN_PASSWD)
ADMIN_PASSWD=$(GEN_PASSWD)
SUPERUSER_PASSWD=$(GEN_PASSWD)

log "Passwords generated (will be stored in var/secrets/knowledge-pg.env)"

# Determine the superuser (usually the running OS user)
SUPERUSER=$(whoami)
log "Superuser: $SUPERUSER"

# Build the DSNs
AGENT_DSN="postgresql://knowledge_agent@localhost/omniagentos_knowledge"
ADMIN_DSN="postgresql://knowledge_admin@localhost/omniagentos_knowledge"
MIGRATE_DSN="postgresql://$SUPERUSER@localhost/omniagentos_knowledge"
TEST_DSN="postgresql://localhost/omniagentos_knowledge_test"
TEST_ADMIN_DSN="postgresql://knowledge_admin@localhost/omniagentos_knowledge_test"

if [ $DRY_RUN -eq 0 ]; then
  # Step 1: Create var/secrets directory
  mkdir -p var/secrets
  chmod 700 var/secrets

  # Step 2: Write the DSNs with passwords to var/secrets/knowledge-pg.env
  log "Writing DSNs to var/secrets/knowledge-pg.env (0600)..."
  cat > var/secrets/knowledge-pg.env.tmp <<EOF
OMNIAGENTOS_KNOWLEDGE_PG_DSN=postgresql://knowledge_agent:$(echo -n "$AGENT_PASSWD" | sed 's/@/%40/g')@localhost/omniagentos_knowledge
OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN=postgresql://knowledge_admin:$(echo -n "$ADMIN_PASSWD" | sed 's/@/%40/g')@localhost/omniagentos_knowledge
OMNIAGENTOS_KNOWLEDGE_MIGRATE_DSN=postgresql://$SUPERUSER:$(echo -n "$SUPERUSER_PASSWD" | sed 's/@/%40/g')@localhost/omniagentos_knowledge
OMNIAGENTOS_KNOWLEDGE_TEST_ADMIN_DSN=postgresql://knowledge_admin:$(echo -n "$ADMIN_PASSWD" | sed 's/@/%40/g')@localhost/omniagentos_knowledge_test
EOF
  chmod 600 var/secrets/knowledge-pg.env.tmp
  mv var/secrets/knowledge-pg.env.tmp var/secrets/knowledge-pg.env

  # Step 3: Set passwords on roles (ORDER: must set BEFORE flipping hba)
  log "Setting passwords on database roles..."
  psql -U "$SUPERUSER" -d postgres <<EOSQL
DO \$\$ BEGIN
  ALTER ROLE knowledge_agent WITH PASSWORD '$AGENT_PASSWD';
  ALTER ROLE knowledge_admin WITH PASSWORD '$ADMIN_PASSWD';
  ALTER ROLE $SUPERUSER WITH PASSWORD '$SUPERUSER_PASSWD';
END \$\$;
EOSQL

  # Step 4: Backup pg_hba.conf
  HBA_BACKUP="$HBA_PATH.backup-$(date +%s)"
  log "Backing up pg_hba.conf to $HBA_BACKUP..."
  cp "$HBA_PATH" "$HBA_BACKUP"

  # Step 5: Flip pg_hba.conf from trust to scram-sha-256 for local and 127.0.0.1
  log "Flipping pg_hba.conf from trust to scram-sha-256..."
  sed -i.bak_temp \
    -e '/^local.*trust$/s/trust/scram-sha-256/' \
    -e '/^host.*127\.0\.0\.1.*trust$/s/trust/scram-sha-256/' \
    -e '/^host.*::1.*trust$/s/trust/scram-sha-256/' \
    "$HBA_PATH"
  rm -f "$HBA_PATH.bak_temp"

  # Step 6: Set log_statement='mod' for the knowledge database
  log "Enabling mutation logging on omniagentos_knowledge..."
  psql -U "$SUPERUSER" -d postgres <<EOSQL
ALTER DATABASE omniagentos_knowledge SET log_statement = 'mod';
ALTER DATABASE omniagentos_knowledge SET log_line_prefix = '%u %d %c ';
EOSQL

  # Step 7: Reload pg_hba changes (reload is sufficient, no restart needed)
  log "Reloading PostgreSQL configuration..."
  pg_ctl reload -D "$PG_CONFIGDIR" 2>/dev/null || { log "Warning: pg_ctl reload failed; try 'systemctl reload postgresql'"; }

  # Brief pause for reload to take effect
  sleep 1

  # Step 8: Self-test — verify that passwordless superuser connect now FAILS
  log "Running self-test: verifying passwordless superuser access is blocked..."
  if PGPASSFILE=/dev/null psql -U "$SUPERUSER" -d omniagentos_knowledge -c 'SELECT 1' >/dev/null 2>&1; then
    die "SELF-TEST FAILED: passwordless superuser connect should have been blocked but succeeded"
  fi
  log "Self-test PASSED: passwordless superuser access is blocked (as intended)"

  # Step 9: Write agent-only credentials to ~/.pgpass
  log "Writing knowledge_agent credentials to ~/.pgpass (0600)..."
  # Remove existing knowledge_agent entry if present
  if [ -f ~/.pgpass ]; then
    grep -v '^localhost.*knowledge_agent' ~/.pgpass > ~/.pgpass.tmp || true
    mv ~/.pgpass.tmp ~/.pgpass
  fi
  # Append only agent credentials (never admin/superuser per R2-3)
  echo "localhost:5432:omniagentos_knowledge:knowledge_agent:$AGENT_PASSWD" >> ~/.pgpass
  chmod 600 ~/.pgpass

  log "SUCCESS: PostgreSQL authentication hardened"
  log ""
  log "=== CHANGES MADE ==="
  log "1. Generated and stored passwords in var/secrets/knowledge-pg.env (0600, gitignored)"
  log "2. Set passwords on roles: knowledge_agent, knowledge_admin, $SUPERUSER"
  log "3. Backed up pg_hba.conf to $HBA_BACKUP"
  log "4. Flipped pg_hba.conf local/127.0.0.1 lines from 'trust' to 'scram-sha-256'"
  log "5. Enabled mutation logging (log_statement='mod') on omniagentos_knowledge"
  log "6. Reloaded PostgreSQL configuration"
  log "7. Wrote knowledge_agent credentials to ~/.pgpass (admin/superuser NOT in pgpass per R2-3)"
  log ""
  log "=== REVERT PROCEDURE ==="
  log "To restore trust-auth (dev/test only, NOT for production):"
  log "  1. Restore pg_hba.conf:  cp $HBA_BACKUP $HBA_PATH"
  log "  2. Reload:                 pg_ctl reload -D $PG_CONFIGDIR"
  log "  3. Remove ~/.pgpass:       rm -f ~/.pgpass"
  log "  4. Remove secrets:         rm -f var/secrets/knowledge-pg.env"
  log "  5. Reset roles to passwordless (optional): psql -c 'ALTER ROLE knowledge_agent PASSWORD NULL;' etc"
  log ""
else
  log "DRY RUN: would apply the above changes (use without --dry-run to apply)"
fi

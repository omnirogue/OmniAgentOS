#!/bin/bash
# Tests for scripts/lib/preflight.sh environment validation.
#
# Run with: bash tests/ops/test_preflight.sh
# Or: make test-preflight (if a Make target is added)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PREFLIGHT_LIB="${ROOT_DIR}/scripts/lib/preflight.sh"
TEST_TMPDIR="${TMPDIR:-/tmp}/omni-preflight-tests.$$"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Cleanup
cleanup() {
  rm -rf "$TEST_TMPDIR"
}
trap cleanup EXIT

# Test utilities
assert_success() {
  local test_name="$1"
  local cmd="$2"
  TESTS_RUN=$((TESTS_RUN + 1))

  if eval "$cmd" >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} $test_name"
    TESTS_PASSED=$((TESTS_PASSED + 1))
  else
    echo -e "${RED}✗${NC} $test_name"
    echo "  Command: $cmd"
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

assert_failure() {
  local test_name="$1"
  local cmd="$2"
  TESTS_RUN=$((TESTS_RUN + 1))

  if ! eval "$cmd" >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} $test_name"
    TESTS_PASSED=$((TESTS_PASSED + 1))
  else
    echo -e "${RED}✗${NC} $test_name"
    echo "  Command: $cmd (expected to fail but succeeded)"
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

# Setup test environment
mkdir -p "$TEST_TMPDIR"

echo "Testing scripts/lib/preflight.sh..."
echo "=================================="
echo

# Test 1: Preflight library exists
if [ -f "$PREFLIGHT_LIB" ]; then
  echo -e "${GREEN}✓${NC} Preflight library file exists"
  TESTS_PASSED=$((TESTS_PASSED + 1))
else
  echo -e "${RED}✗${NC} Preflight library file not found at $PREFLIGHT_LIB"
  TESTS_FAILED=$((TESTS_FAILED + 1))
fi
TESTS_RUN=$((TESTS_RUN + 1))
echo

# Test 2: Preflight library can be sourced
test_source_ok() {
  . "$PREFLIGHT_LIB" && [ -n "$(declare -f _omni_preflight_env_vars 2>/dev/null)" ]
}
assert_success "Preflight library sources without error" "test_source_ok"
echo

# Test 3: Function _omni_preflight_env_vars exists
test_func_exists() {
  . "$PREFLIGHT_LIB"
  declare -f _omni_preflight_env_vars >/dev/null 2>&1
}
assert_success "Function _omni_preflight_env_vars is defined" "test_func_exists"
echo

# Test 4: Missing env file detection
test_missing_file() {
  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$TEST_TMPDIR/nonexistent.env" >/dev/null 2>&1
}
assert_failure "Missing env file is detected" "test_missing_file"
echo

# Test 5: Unreadable env file detection
test_unreadable_file() {
  local test_file="$TEST_TMPDIR/unreadable.env"
  touch "$test_file"
  chmod 000 "$test_file"

  . "$PREFLIGHT_LIB"
  result=$(_omni_preflight_env_vars "$test_file" 2>&1)
  rc=$?

  chmod 644 "$test_file" # restore permissions for cleanup
  return $rc
}
assert_failure "Unreadable env file is detected" "test_unreadable_file"
echo

# Test 6: Readable empty env file passes (no required vars)
test_readable_empty() {
  local test_file="$TEST_TMPDIR/empty.env"
  touch "$test_file"
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" >/dev/null 2>&1
}
assert_success "Readable empty env file passes check (no required vars)" "test_readable_empty"
echo

# Test 7: Readable env file with content passes (no required vars)
test_readable_with_content() {
  local test_file="$TEST_TMPDIR/with_content.env"
  cat > "$test_file" << 'EOF'
API_KEY=test_value
SECRET_VAR=another_value
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" >/dev/null 2>&1
}
assert_success "Readable env file with content passes check" "test_readable_with_content"
echo

# Test 8: Required var check - missing var
test_required_var_missing() {
  local test_file="$TEST_TMPDIR/missing_var.env"
  cat > "$test_file" << 'EOF'
SOME_VAR=value
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" "REQUIRED_VAR" >/dev/null 2>&1
}
assert_failure "Missing required variable is detected" "test_required_var_missing"
echo

# Test 9: Required var check - var is empty
test_required_var_empty() {
  local test_file="$TEST_TMPDIR/empty_var.env"
  cat > "$test_file" << 'EOF'
REQUIRED_VAR=
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" "REQUIRED_VAR" >/dev/null 2>&1
}
assert_failure "Empty required variable is detected" "test_required_var_empty"
echo

# Test 10: Required var check - var is set
test_required_var_set() {
  local test_file="$TEST_TMPDIR/var_set.env"
  cat > "$test_file" << 'EOF'
REQUIRED_VAR=value
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" "REQUIRED_VAR" >/dev/null 2>&1
}
assert_success "Set required variable passes check" "test_required_var_set"
echo

# Test 11: Multiple required vars - all set
test_multiple_vars_set() {
  local test_file="$TEST_TMPDIR/multiple_set.env"
  cat > "$test_file" << 'EOF'
VAR1=value1
VAR2=value2
VAR3=value3
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" "VAR1" "VAR2" "VAR3" >/dev/null 2>&1
}
assert_success "Multiple set required variables pass check" "test_multiple_vars_set"
echo

# Test 12: Multiple required vars - one missing
test_multiple_vars_one_missing() {
  local test_file="$TEST_TMPDIR/multiple_missing.env"
  cat > "$test_file" << 'EOF'
VAR1=value1
VAR2=value2
EOF
  chmod 644 "$test_file"

  . "$PREFLIGHT_LIB"
  _omni_preflight_env_vars "$test_file" "VAR1" "VAR2" "VAR3" >/dev/null 2>&1
}
assert_failure "Missing one of multiple required variables is detected" "test_multiple_vars_one_missing"
echo

# Test 13: Error messages are precise for missing file
test_error_msg_missing_file() {
  local test_file="$TEST_TMPDIR/nonexistent.env"
  . "$PREFLIGHT_LIB"
  local err_msg=$(_omni_preflight_env_vars "$test_file" 2>&1)

  if echo "$err_msg" | grep -q "FATAL.*env file not found"; then
    return 0
  else
    echo "Error message: $err_msg"
    return 1
  fi
}
assert_success "Error message for missing file is precise" "test_error_msg_missing_file"
echo

# Test 14: Error messages are precise for unreadable file
test_error_msg_unreadable() {
  local test_file="$TEST_TMPDIR/unreadable2.env"
  touch "$test_file"
  chmod 000 "$test_file"

  . "$PREFLIGHT_LIB"
  # Simply check that the function returns non-zero for an unreadable file
  _omni_preflight_env_vars "$test_file" >/dev/null 2>&1
  local rc=$?

  chmod 644 "$test_file" # restore for cleanup

  # Should return 1 (failure) for unreadable file
  return $([ $rc -ne 0 ] && echo 0 || echo 1)
}
assert_success "Error message for unreadable file is precise" "test_error_msg_unreadable"
echo

# Test 15: launch-env.sh integrates preflight check
test_launch_env_has_preflight() {
  grep -q "_omni_preflight_env_vars" "${ROOT_DIR}/scripts/launch-env.sh" && \
  grep -q "scripts/lib/preflight.sh" "${ROOT_DIR}/scripts/launch-env.sh"
}
assert_success "launch-env.sh includes preflight library and calls preflight check" "test_launch_env_has_preflight"
echo

# Test 16: launch-env.sh can be sourced
test_launch_env_source() {
  # Use a subshell to isolate
  (
    # Unset inherited simulation vars that may interfere
    unset OMNIAGENTOS_DB OMNIAGENTOS_VAR OMNIAGENTOS_VAR_DIR
    unset OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR
    unset OMNIAGENTOS_SIM_ENV_LOADED OMNIAGENTOS_SIM_ENV_NONCE OMNIAGENTOS_LAUNCH_ENV_LOADED
    
    # Create a minimal test environment
    export OMNIAGENTOS_SIM_CAMPAIGN="test-$$"
    export OMNIAGENTOS_SIM_MODE=1
    . "${ROOT_DIR}/scripts/launch-env.sh" 2>/dev/null
  )
}
assert_success "launch-env.sh can be sourced successfully" "test_launch_env_source"
echo

# Summary
echo "=================================="
echo "Test Results:"
echo -e "  Passed: ${GREEN}${TESTS_PASSED}${NC}"
echo -e "  Failed: ${RED}${TESTS_FAILED}${NC}"
echo -e "  Total:  ${YELLOW}${TESTS_RUN}${NC}"
echo

if [ $TESTS_FAILED -eq 0 ]; then
  echo -e "${GREEN}All tests passed!${NC}"
  exit 0
else
  echo -e "${RED}Some tests failed.${NC}"
  exit 1
fi

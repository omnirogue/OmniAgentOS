# shellcheck shell=bash
# Preflight environment validation library for OmniAgentOS.
#
# Provides _omni_preflight_env_vars() to verify that a required environment
# file exists, is readable, and defines a list of required variables.
# Exits non-zero with a precise error message if validation fails.
#
# Usage:
#   _omni_preflight_env_vars "$env_file" "VAR1" "VAR2" "VAR3"
#
# Returns:
#   0 if env file exists, is readable, and all required vars are non-empty
#   1 if env file missing, unreadable, or required vars are empty/unset

_omni_preflight_env_vars() {
  local env_file="$1"
  shift
  local required_vars=("$@")

  # Verify env file path is provided
  if [ -z "$env_file" ]; then
    echo "preflight: env file path required" >&2
    return 1
  fi

  # Check if env file exists
  if [ ! -f "$env_file" ]; then
    echo "FATAL(preflight): env file not found: $env_file" >&2
    echo "  (This file is required for proper operation but is missing)" >&2
    return 1
  fi

  # Check if env file is readable
  if [ ! -r "$env_file" ]; then
    echo "FATAL(preflight): env file is not readable: $env_file" >&2
    echo "  (File exists at $env_file but cannot be read; check permissions)" >&2
    return 1
  fi

  # If no required vars were specified, just verify the file exists (already done)
  if [ ${#required_vars[@]} -eq 0 ]; then
    return 0
  fi

  # Source the env file and verify all required vars are non-empty
  # Use a subshell to avoid polluting the caller's environment
  (
    # Source the env file; if it fails, report the error
    if ! . "$env_file" 2>/dev/null; then
      echo "FATAL(preflight): env file could not be sourced: $env_file" >&2
      echo "  (File is readable but contains a syntax error or sourcing failed)" >&2
      exit 1
    fi

    # Check each required variable
    local empty_vars=()
    for var in "${required_vars[@]}"; do
      # Use eval to get the value of the variable dynamically
      eval "local val=\$$var"
      if [ -z "$val" ]; then
        empty_vars+=("$var")
      fi
    done

    # If any vars are empty, report them
    if [ ${#empty_vars[@]} -gt 0 ]; then
      echo "FATAL(preflight): required environment variables not set or empty: ${empty_vars[*]}" >&2
      echo "  (File $env_file exists and is readable, but these vars are missing/empty: ${empty_vars[*]})" >&2
      exit 1
    fi

    exit 0
  )

  return $?
}

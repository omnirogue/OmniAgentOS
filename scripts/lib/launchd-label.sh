# shellcheck shell=sh
# One answer to "is this launchd label safe to put in a path".
#
# Every installer builds its plist path as ${TARGET_DIR}/${LABEL}.plist, so a
# label carrying "/" or "../" writes -- or, in uninstall.sh, DELETES -- outside
# the render directory. Before this file the repo had three answers: an exact
# literal comparison in ten installers, nothing at all in nine, and a per-file
# opinion drifting between them.
#
# The check is on the SHAPE, not on a list of known-bad values. Rejecting ".."
# and "/" by name is what let "..." through in the swarm ownership check
# (issue #31) -- an enumerated denylist is always one variant behind. A label
# with no "/" and no leading "." cannot leave its directory no matter what
# else it contains.
#
# The optional second argument additionally pins the label to one exact value,
# for installers that genuinely only ever have one. That is a policy choice per
# installer; the shape check is not optional for anyone.
#
# POSIX sh only: uninstall.sh and several installers are #!/bin/sh.

require_safe_launchd_label() {
    _rsl_label=${1:-}
    _rsl_expected=${2:-}

    if [ -z "$_rsl_label" ]; then
        echo "error: launchd label is empty" >&2
        return 1
    fi
    case "$_rsl_label" in
        *'
'*)
            # Command substitution strips trailing newlines, so tr alone
            # cannot detect a label that ends in one.
            echo "error: launchd label has characters outside [A-Za-z0-9._-]: $_rsl_label" >&2
            return 1
            ;;
        */*)
            echo "error: launchd label may not contain '/': $_rsl_label" >&2
            return 1
            ;;
        .*)
            echo "error: launchd label may not start with '.': $_rsl_label" >&2
            return 1
            ;;
    esac
    # Anything outside [A-Za-z0-9._-] -- control characters, spaces, quotes,
    # backslashes, NUL-adjacent bytes -- is refused rather than escaped. An
    # installer has no need for them and every use of one is a surprise.
    if [ -n "$(printf '%s' "$_rsl_label" | tr -d 'A-Za-z0-9._-')" ]; then
        echo "error: launchd label has characters outside [A-Za-z0-9._-]: $_rsl_label" >&2
        return 1
    fi
    if [ -n "$_rsl_expected" ] && [ "$_rsl_label" != "$_rsl_expected" ]; then
        echo "error: refusing unexpected launchd label: $_rsl_label (expected $_rsl_expected)" >&2
        return 1
    fi
    return 0
}

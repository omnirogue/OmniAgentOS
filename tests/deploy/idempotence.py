"""A generic bash idempotence checker used by the deploy plan tests.

It reads a rendered plan script and reports every line that MUTATES remote
state without either (a) a probe guard (``command -v caddy || ...``) or
(b) being naturally repeat-safe (``mkdir -p``, a whole-file write, a restart).

Deliberately written against generic bash shapes rather than against the exact
strings the planner emits, so it stays a real assertion instead of a mirror of
the implementation. ``test_idempotence_checker.py`` pins that it actually
catches unguarded commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Commands that change remote state.
_MUTATING = (
    r"apt-get\s+install",
    r"apt\s+install",
    r"\buseradd\b",
    r"\bgroupadd\b",
    r"\bgpg\s+--dearmor\b",
    r"\bgit\s+clone\b",
    r"\brsync\b",
    r"\bmkdir\b",
    r"\bchown\b",
    r"\bchmod\b",
    r"\binstall\s+-m\b",
    r"\bln\s+-s",
    r"\bsystemctl\s+(enable|start|restart|reload|daemon-reload)\b",
    r"\bufw\s+(allow|deny|--force\s+enable|enable)\b",
    r"\bapt-get\s+update\b",
    r">\s*/(etc|usr|srv|opt|var)/",
)

# Probes that make whatever follows `||` (or a then/else branch) conditional.
_GUARD_HEAD = (
    r"command\s+-v\s",
    r"id\s+-u\s",
    r"test\s+-[a-z]\s",
    r"\[\s+-[a-z]\s",
    r"dpkg\s+-s\s",
    r"grep\s+-q",
    r"systemctl\s+is-(enabled|active)",
    r"ufw\s+status",
)

# Naturally repeat-safe: running them twice converges to the same state.
_REPEAT_SAFE = (
    r"^mkdir\s+-p\s",
    r"^install\s+-m\s",  # whole-file write with a fixed mode
    r"^apt-get\s+update\b",
    r"^ufw\s+allow\s",
    r"^chown\s",
    r"^chmod\s",
    r"^systemctl\s+daemon-reload\b",
    r"^systemctl\s+(reload|restart|reload-or-restart|enable)\s",
    r"^rsync\s+-a\s+--delete\s",
)

_RUNUSER = re.compile(r"^runuser\s+-u\s+\S+\s+--\s+\S+\s+-lc\s+'(?P<payload>.*)'$", re.DOTALL)
_HEREDOC_OPEN = re.compile(r"<<'(?P<delim>[A-Z_]+)'")


@dataclass(frozen=True)
class Violation:
    line_no: int
    line: str
    reason: str


def _payloads(line: str) -> list[str]:
    """The line itself plus, if it wraps one, the inner `bash -lc '...'` payload."""
    match = _RUNUSER.match(line)
    if match:
        return [match.group("payload")]
    return [line]


def _is_mutating(text: str) -> bool:
    return any(re.search(pattern, text) for pattern in _MUTATING)


def _is_guarded(text: str) -> bool:
    stripped = text.strip()
    # `if <probe>; then ... fi` — the mutation is inside a conditional branch.
    if stripped.startswith("if "):
        head = stripped[3:]
        return any(re.match(pattern, head) for pattern in _GUARD_HEAD)
    # `<probe> || <mutation>` — the mutation only runs when the probe fails.
    if "||" not in stripped:
        return False
    head = stripped.split("||", 1)[0].strip()
    return any(re.match(pattern, head) for pattern in _GUARD_HEAD)


def _is_repeat_safe(text: str) -> bool:
    stripped = text.strip()
    # A chained `a && b && c` is repeat-safe only if EVERY link is.
    parts = [part.strip() for part in re.split(r"&&", stripped) if part.strip()]
    return all(
        any(re.match(pattern, part) for pattern in _REPEAT_SAFE) for part in parts
    )


def _significant_lines(script: str) -> list[tuple[int, str]]:
    """Script lines with comments, blanks, and heredoc BODIES removed.

    Heredoc bodies are literal file content (a systemd unit, a Caddy block), not
    commands, so scanning them would produce nonsense findings.
    """
    out: list[tuple[int, str]] = []
    delim: str | None = None
    for idx, raw in enumerate(script.splitlines(), start=1):
        if delim is not None:
            if raw.strip() == delim:
                delim = None
            continue
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("set "):
            continue
        opened = _HEREDOC_OPEN.search(line)
        if opened:
            delim = opened.group("delim")
        out.append((idx, line))
    return out


def find_unguarded(script: str) -> list[Violation]:
    """Every mutating line in ``script`` that is neither guarded nor repeat-safe."""
    violations: list[Violation] = []
    for line_no, line in _significant_lines(script):
        for text in _payloads(line):
            if not _is_mutating(text):
                continue
            if _is_guarded(text) or _is_repeat_safe(text):
                continue
            violations.append(
                Violation(line_no=line_no, line=line, reason="mutating, unguarded")
            )
    return violations


def mutating_lines(script: str) -> list[str]:
    """Every line the checker classifies as mutating (used to prove it sees work)."""
    found: list[str] = []
    for _, line in _significant_lines(script):
        if any(_is_mutating(text) for text in _payloads(line)):
            found.append(line)
    return found

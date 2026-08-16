"""Negative control for the idempotence checker.

Without this, a green idempotence test could mean "the checker sees nothing"
rather than "the script is guarded".
"""

from __future__ import annotations

import pytest

from tests.deploy.idempotence import find_unguarded, mutating_lines

UNGUARDED = [
    "apt-get install -y caddy",
    "useradd deploy",
    "curl -fsSL https://x/key | gpg --dearmor -o /usr/share/keyrings/x.gpg",
    "git clone https://example.com/a.git /srv/apps/a",
    "echo hi > /etc/motd.d/hi",
]

GUARDED = [
    "command -v caddy >/dev/null 2>&1 || apt-get install -y caddy",
    "id -u deploy >/dev/null 2>&1 || useradd deploy",
    "test -f /usr/share/keyrings/x.gpg || (curl -fsSL https://x/key | gpg --dearmor -o /usr/share/keyrings/x.gpg)",
    "mkdir -p /srv/apps && chown deploy:deploy /srv/apps",
    "if test -d /srv/apps/a/.git; then git -C /srv/apps/a pull --ff-only; else git clone https://e/a.git /srv/apps/a; fi",
    "runuser -u deploy -- /bin/bash -lc 'rsync -a --delete /opt/staging/a/ /srv/apps/a/'",
]


@pytest.mark.parametrize("line", UNGUARDED)
def test_unguarded_mutation_is_caught(line: str) -> None:
    assert find_unguarded(line), f"checker missed an unguarded mutation: {line}"


@pytest.mark.parametrize("line", GUARDED)
def test_guarded_mutation_passes(line: str) -> None:
    assert mutating_lines(line) == [line], "checker no longer sees this as mutating"
    assert find_unguarded(line) == []


def test_heredoc_bodies_are_not_scanned_as_commands() -> None:
    script = (
        "install -m 644 /dev/stdin /etc/systemd/system/x.service <<'OMNI_UNIT'\n"
        "ExecStart=/bin/bash -lc 'apt-get install everything'\n"
        "OMNI_UNIT\n"
    )
    assert find_unguarded(script) == []


def test_comments_and_blank_lines_are_ignored() -> None:
    assert find_unguarded("# apt-get install foo\n\n") == []

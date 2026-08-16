from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.orchestrator.approvals import classify_hard_stop
from omniagentos.orchestrator.contracts import ApprovalRequest
from omniagentos.policy.shell import _parse_ssh_command, classify_shell
from omniagentos.runner import sandbox
from omniagentos.sessions import ssh_keys


@pytest.mark.parametrize(
    "command",
    [
        "ssh host 'ls -la'",
        "ssh host 'cat /etc/hosts'",
        "ssh host 'git status'",
        "ssh host 'ps aux'",
        "ssh host 'docker ps'",
        "ssh db 'psql -c \"SELECT * FROM users\"'",
        "ssh host hostname",
        "ssh -o BatchMode=yes -o ConnectTimeout=8 host hostname",
    ],
)
def test_read_only_ssh_commands_auto_approve(command: str) -> None:
    assert classify_shell(command, "/workspace") == ActionClass.READ_ONLY


@pytest.mark.parametrize(
    "command",
    [
        "ssh host 'systemctl restart nginx'",
        "ssh host 'docker compose up -d'",
        "ssh host 'systemctl restart'",
        "ssh host 'docker restart container'",
        "ssh host 'systemctl start postgresql'",
    ],
)
def test_safe_operations_auto_approve(command: str) -> None:
    # Per the operator's policy: service management, container management auto-approve
    # (not delete, not money, so should return CONSEQUENTIAL not IRREVERSIBLE)
    result = classify_shell(command, "/workspace")
    assert result != ActionClass.IRREVERSIBLE, f"Command should auto-approve: {command}"


@pytest.mark.parametrize(
    "command",
    [
        "ssh host 'rm -rf /srv/app'",
        "ssh db 'psql -c \"DROP TABLE users\"'",
        "ssh db 'mysql -c \"DELETE FROM users\"'",
        "ssh host 'git reset --hard'",
        "ssh host 'apt remove postgresql'",
        "ssh host 'stripe refunds create --charge ch_123'",
        "ssh host 'x && rm -rf /y'",
    ],
)
def test_destructive_and_money_remote_commands_escalate(command: str) -> None:
    assert classify_shell(command, "/workspace") == ActionClass.IRREVERSIBLE


@pytest.mark.parametrize(
    "command",
    [
        # Multi-statement: a leading SELECT must not launder a later mutation into
        # READ_ONLY. The old check used query.startswith("select"), so anything
        # that *began* with SELECT was reported read-only even when a write followed.
        "ssh db 'psql -c \"select 1; update users set x=1\"'",
        "ssh db 'psql -c \"select 1; insert into users values (1)\"'",
        "ssh db 'psql -c \"select 1; alter table users add column x int\"'",
        "ssh db 'psql -c \"select 1; create table pwn(i int)\"'",
        "ssh db 'mysql -e \"select 1; update t set x=1\"'",
        # Single statement that starts with SELECT but is a write/export/lock.
        "ssh db 'psql -c \"select * into junk from users\"'",
        "ssh db 'psql -c \"select id from users for update\"'",
        # Multiple -c/-e payloads: psql/mysql execute every one. A first pure
        # SELECT must not launder a later write flag (return-True-inside-loop bug).
        "ssh db 'psql -c \"select 1\" -c \"update users set x=1\"'",
        "ssh db 'psql -c \"select 1\" -c \"insert into users values (1)\"'",
        "ssh db 'psql -c \"select 1\" -c \"alter table users add column x int\"'",
        "ssh db 'psql -c \"select 1\" -c \"create table pwn(i int)\"'",
        "ssh db 'mysql -e \"select 1\" -e \"update t set x=1\"'",
        # EXPLAIN ANALYZE executes the given statement; DML must not claim READ_ONLY.
        "ssh db 'psql -c \"explain analyze update users set x = 1\"'",
        "ssh db 'psql -c \"explain analyze insert into users values (1)\"'",
        "ssh db 'psql -c \"explain (analyze) update users set x = 1\"'",
        "ssh db 'psql -c \"explain (analyze, buffers) create table pwn(i int)\"'",
    ],
)
def test_remote_sql_mutation_is_not_reported_read_only(command: str) -> None:
    """Non-result-as-favourable: unproven SQL must not claim READ_ONLY.

    Counterfeits this catches:
      * startswith('select') only — multi-statement select;update still 'passes'
      * ban only the word 'update' — insert/alter/create still launder
      * ban ';' entirely but leave SELECT INTO / FOR UPDATE as READ_ONLY
      * return True after the first -c payload — second -c write still 'passes'
      * admit EXPLAIN heads without peeling ANALYZE — EXPLAIN ANALYZE DML 'passes'
      * always return False from the helper but still classify pure SELECT as
        READ_ONLY via a parallel path — pure SELECT stays covered by
        test_read_only_ssh_commands_auto_approve
    """
    result = classify_shell(command, "/workspace")
    assert result is not ActionClass.READ_ONLY, (
        f"mutating / multi-statement SQL must not claim read-only: {command!r} -> {result}"
    )


def test_remote_sql_trailing_semicolon_still_read_only() -> None:
    """A sole SELECT with a trailing ';' is still a single read-only statement."""
    assert (
        classify_shell("ssh db 'psql -c \"select 1;\"'", "/workspace")
        is ActionClass.READ_ONLY
    )


def test_remote_sql_multi_select_still_read_only() -> None:
    """Multiple pure SELECT statements remain read-only (no write laundering)."""
    assert (
        classify_shell("ssh db 'psql -c \"select 1; select 2\"'", "/workspace")
        is ActionClass.READ_ONLY
    )


def test_remote_sql_multi_c_all_select_still_read_only() -> None:
    """Every -c payload pure SELECT remains read-only (no false hard-stop)."""
    assert (
        classify_shell(
            "ssh db 'psql -c \"select 1\" -c \"select 2\"'",
            "/workspace",
        )
        is ActionClass.READ_ONLY
    )


def test_remote_sql_explain_analyze_select_still_read_only() -> None:
    """EXPLAIN ANALYZE of a pure SELECT still executes only a read."""
    assert (
        classify_shell(
            "ssh db 'psql -c \"explain analyze select 1\"'",
            "/workspace",
        )
        is ActionClass.READ_ONLY
    )


@pytest.mark.parametrize(
    "command",
    [
        "ssh host 'cmd1 | cmd2'",
        "ssh host 'cmd1 && cmd2'",
        "ssh host 'rm -rf $(find /tmp -name \"*.tmp\")'",
        "ssh host < /path/to/script",
        "ssh host \"echo 'data' > /file\"",
        "ssh host",
    ],
)
def test_ambiguous_remote_commands_fail_closed(command: str) -> None:
    assert classify_shell(command, "/workspace") == ActionClass.IRREVERSIBLE


def test_ssh_options_and_transfer_inference() -> None:
    assert _parse_ssh_command('ssh -p 22 user@host "ls -la"') == (
        "user@host",
        "ls -la",
    )
    assert _parse_ssh_command("scp host:/srv/file /tmp/file") == (
        "host",
        "cat /srv/file",
    )
    assert _parse_ssh_command("rsync host:/srv/files /tmp/files") == (
        "host",
        "rsync on host",
    )
    assert classify_shell("scp local.txt host:/srv/file", "/workspace") == (
        ActionClass.IRREVERSIBLE
    )
    assert classify_shell("rsync local/ host:/srv/files", "/workspace") == (
        ActionClass.IRREVERSIBLE
    )
    assert (
        classify_shell("scp host:/srv/file /workspace/file", "/workspace")
        == ActionClass.INTERNAL_REVERSIBLE
    )
    assert classify_shell("scp host:/srv/file /tmp/file", "/workspace") == ActionClass.IRREVERSIBLE
    assert classify_shell("ssh -o ProxyCommand=evil host 'ls'", "/workspace") == (
        ActionClass.IRREVERSIBLE
    )
    assert classify_shell("rsync -e evil host:/srv/files /workspace", "/workspace") == (
        ActionClass.IRREVERSIBLE
    )
    assert (
        classify_shell("rsync 'host:/srv/files; rm -rf /' /workspace", "/workspace")
        == ActionClass.IRREVERSIBLE
    )


def test_session_grant_rejects_unlisted_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    ssh_keys.issue_ssh_key_grant("ses_test", ["hostA", "deploy@hostB"])

    assert classify_shell("ssh hostA 'ls'", "/workspace", "ses_test") == ActionClass.READ_ONLY
    assert (
        classify_shell("ssh deploy@hostB 'ls'", "/workspace", "ses_test") == ActionClass.READ_ONLY
    )
    assert (
        classify_shell("ssh root@hostB 'ls'", "/workspace", "ses_test") == ActionClass.IRREVERSIBLE
    )
    assert (
        classify_shell("ssh unauthorized 'ls'", "/workspace", "ses_test")
        == ActionClass.IRREVERSIBLE
    )


def test_ssh_grant_file_is_scoped_and_mode_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    issued = ssh_keys.issue_ssh_key_grant("ses_ssh", ["hostA", "user@hostB"])
    path = Path(issued)
    assert path == ssh_keys.ssh_key_grant_path("ses_ssh")
    assert ssh_keys.read_ssh_key_grant("ses_ssh") == ["hostA", "user@hostB"]
    assert path.stat().st_mode & 0o777 == 0o600
    ssh_keys.revoke_ssh_key_grant("ses_ssh")
    assert not path.exists()


def test_inventory_reader_accepts_only_summary_aliases(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        """# Server Inventory

Prose mentioning attacker.example must not become a grant.

## Summary Table

| Alias | IP | Status |
|---|---|---|
| prod-a | 192.0.2.10 | Reachable |
| deploy@prod-b | 192.0.2.11 | Reachable |

## Notes

| unrelated | value |
|---|---|
| not-a-host/path | ignored |
""",
        encoding="utf-8",
    )
    assert ssh_keys.read_server_inventory_hosts(inventory) == ["prod-a", "deploy@prod-b"]
    assert ssh_keys.read_server_inventory_hosts(tmp_path / "missing.md") == []


def test_sandbox_reopens_only_granted_host_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    config = ssh_dir / "config"
    known_hosts = ssh_dir / "known_hosts"
    id_a = ssh_dir / "id_host_a"
    id_b = ssh_dir / "id_host_b"
    id_dsa = ssh_dir / "id_dsa"
    control_token = Path(__file__).resolve().parents[2] / "var" / "secrets" / "sessions-token"
    config.write_text(
        "\n".join(
            [
                "Host hostA",
                "  IdentityFile ~/.ssh/id_host_a",
                "Host hostB",
                "  IdentityFile ~/.ssh/id_host_b",
                f"  IdentityFile {control_token}",
                "Host other",
                "  IdentityFile ~/.ssh/id_dsa",
            ]
        ),
        encoding="utf-8",
    )
    known_hosts.write_text("hostA key\n", encoding="utf-8")
    for key in (id_a, id_b, id_dsa):
        key.write_text("test key", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    ssh_keys.issue_ssh_key_grant("ses_owner", ["hostA", "hostB"])

    allowed = sandbox.session_ssh_credentials_read_allow("ses_owner")
    assert os.path.realpath(config) in allowed
    assert os.path.realpath(known_hosts) in allowed
    assert os.path.realpath(id_a) in allowed
    assert os.path.realpath(id_b) in allowed
    assert os.path.realpath(id_dsa) not in allowed
    assert os.path.realpath(control_token) not in allowed
    assert os.path.realpath(ssh_keys.ssh_key_grant_path("ses_owner")) in allowed

    profile = sandbox.build_profile(
        str(tmp_path / "workspace"), ssh_key_grant_session_id="ses_owner"
    )
    assert f'(deny file-read* (subpath "{sandbox.ssh_key_read_deny_root()}"))' in profile
    assert f'(deny file-write* (subpath "{sandbox.ssh_key_read_deny_root()}"))' in profile
    for path in allowed:
        assert f'(allow file-read* (literal "{path}"))' in profile
    assert f'(allow file-read* (literal "{os.path.realpath(id_dsa)}"))' not in profile
    assert f'(allow file-read* (literal "{os.path.realpath(control_token)}"))' not in profile
    assert any(
        f'(deny file-read* (subpath "{root}"))' in profile
        for root in sandbox.secret_read_deny_roots()
        if root.endswith("var/secrets")
    )
    assert f'(allow file-read* (subpath "{os.path.realpath(ssh_dir)}"))' not in profile


def test_orchestrator_policy_escalates_only_delete_and_money() -> None:
    """Per the operator's policy: only delete and money operations escalate."""
    delete_op = ApprovalRequest(
        "Bash",
        ActionClass.IRREVERSIBLE,
        "Bash",
        {"command": "ssh host 'rm -rf /'"},
    )
    money_op = ApprovalRequest(
        "Bash",
        ActionClass.IRREVERSIBLE,
        "Bash",
        {"command": "ssh host 'stripe refunds create'"},
    )
    safe_op = ApprovalRequest(
        "Bash",
        ActionClass.CONSEQUENTIAL,
        "Bash",
        {"command": "ssh host 'systemctl restart nginx'"},
    )
    chained_delete = ApprovalRequest(
        "Bash",
        ActionClass.IRREVERSIBLE,
        "Bash",
        {"command": "ssh host 'foo && rm -rf /y'"},
    )

    assert classify_hard_stop(delete_op) == "delete"
    assert classify_hard_stop(money_op) == "money"
    assert classify_hard_stop(safe_op) is None  # auto-approve
    assert classify_hard_stop(chained_delete) == "delete"

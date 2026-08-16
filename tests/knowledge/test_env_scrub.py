"""Env-scrub is an ALLOWLIST (AC-policy money boundary), with two exceptions
that are themselves load-bearing.

_scrubbed_env() passes ONLY process-hygiene/locale vars, the provider CONFIG-DIR
pointers, and the macOS per-session handles. EVERYTHING else -- payment/bank/infra
credentials, model-provider API keys, admin DSNs, OPERATOR_TOKEN / self-approve
tokens, and any unknown secret -- is stripped, so an agent/runner subprocess has no
credential to move money with; the broker (in the parent process) is the sole money
path.

The config-dir passthrough is the fix for a LIVE failure (2026-07-26): stripping
CLAUDE_CONFIG_DIR did not make the spawn safer, it silently re-routed the child CLI
to ~/.claude -- a different account than the parent is authenticated as -- which was
over its monthly spend limit and returned `terminal_reason: api_error` with zero
tokens. Same argv, same scrubbed env, CLAUDE_CONFIG_DIR restored => clean success.
"""

from __future__ import annotations

from omniagentos.adapters.common import _scrubbed_env


class TestCredentialsAreStripped:
    """The money boundary: nothing credential-shaped reaches a subprocess."""

    def test_payment_bank_infra_credentials_are_absent(self) -> None:
        base = {
            "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY": "sk_live_x",
            "ACMEUNI_STRIPE_SECONDARY_SECRET_KEY": "sk_live_y",
            "SLASH_API_KEY": "slash_x",
            "PAYPAL_WRITE_CLIENT_SECRET": "pp_x",
            "FORTPOINT_NMI_SECURITY_KEY": "nmi_x",
            "DATABASE_URL": "postgres://admin:pw@host/db",
            "AWS_SECRET_ACCESS_KEY": "aws_x",
            "CLOUDFLARE_API_TOKEN": "cf_x",
            "STRIPE_SECRET_KEY": "sk_live_z",
            "PATH": "/usr/bin",
        }
        result = _scrubbed_env(base)
        for leaked in (
            "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY",
            "ACMEUNI_STRIPE_SECONDARY_SECRET_KEY",
            "SLASH_API_KEY",
            "PAYPAL_WRITE_CLIENT_SECRET",
            "FORTPOINT_NMI_SECURITY_KEY",
            "DATABASE_URL",
            "AWS_SECRET_ACCESS_KEY",
            "CLOUDFLARE_API_TOKEN",
            "STRIPE_SECRET_KEY",
        ):
            assert leaked not in result, f"{leaked} leaked into the subprocess env"
        assert result["PATH"] == "/usr/bin"

    def test_model_provider_api_keys_are_never_passed(self) -> None:
        """Cost-policy invariant: a subscription CLI handed an API key silently
        switches to metered per-token billing. It also widens the blast radius of
        a compromised agent. The broker is the explicit path for anything that
        genuinely needs one."""
        base = {
            "ANTHROPIC_API_KEY": "sk-ant-x",
            "OPENAI_API_KEY": "sk-x",
            "OPENROUTER_API_KEY": "sk-or-x",
            "XAI_API_KEY": "xai-x",
            "GEMINI_API_KEY": "g-x",
            "PATH": "/bin",
        }
        result = _scrubbed_env(base)
        for leaked in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "XAI_API_KEY",
            "GEMINI_API_KEY",
        ):
            assert leaked not in result, f"{leaked} reached a subscription CLI"
        assert result["PATH"] == "/bin"

    def test_operator_and_admin_tokens_are_absent(self) -> None:
        base = {
            "OPERATOR_TOKEN": "secret-token",
            "OMNIAGENTOS_OPERATOR_TOKEN": "the-real-operator-var",
            "OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN": "postgresql://admin:secret@localhost/kb",
            "OMNIAGENTOS_KNOWLEDGE_MIGRATE_DSN": "postgresql://root:secret@localhost/kb",
            "OMNIAGENTOS_KNOWLEDGE_PG_DSN": "postgresql://agent@localhost/kb",
            "SOME_PROTECTED_STORE": "/path",
            "HOME": "/home/user",
        }
        result = _scrubbed_env(base)
        # Under an allowlist EVERYTHING non-allowlisted is stripped -- including the
        # agent-role DSN, which an agent CLI subprocess never needs.
        for leaked in (
            "OPERATOR_TOKEN",
            "OMNIAGENTOS_OPERATOR_TOKEN",
            "OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN",
            "OMNIAGENTOS_KNOWLEDGE_MIGRATE_DSN",
            "OMNIAGENTOS_KNOWLEDGE_PG_DSN",
            "SOME_PROTECTED_STORE",
        ):
            assert leaked not in result
        assert result["HOME"] == "/home/user"

    def test_unknown_var_is_stripped_by_default(self) -> None:
        base = {"WEIRD_UNDECLARED_SECRET": "x", "PATH": "/bin"}
        result = _scrubbed_env(base)
        assert "WEIRD_UNDECLARED_SECRET" not in result  # deny-by-default
        assert "PATH" in result

    def test_credential_shaped_names_are_force_denied(self) -> None:
        """Force-deny runs BEFORE the allowlist, so a future allowlist edit that
        named one of these could still not pass it."""
        base = {
            "SOMETHING_API_KEY": "x",
            "VENDOR_TOKEN": "x",
            "APP_PASSWORD": "x",
            "SVC_CREDENTIALS": "x",
            "DEPLOY_PRIVATE_KEY": "x",
            "ANALYTICS_DSN": "x",
            "PATH": "/bin",
        }
        result = _scrubbed_env(base)
        assert set(result) == {"PATH"}

    def test_prefix_admission_is_closed_over_credential_shapes(self) -> None:
        """Prefix allowlists retain path/session pointers, never credentials."""
        paths = {
            "XDG_CONFIG_HOME": "/home/user/.config",
            "XDG_CACHE_HOME": "/home/user/.cache",
            "XDG_CONFIG_DIRS": "/etc/xdg",
            "XDG_DATA_HOME": "/home/user/.local/share",
            "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
            "XDG_RUNTIME_DIR": "/run/user/501",
        }
        credentials = {
            "xdg_auth": "dummy-xdg-auth",
            "XDG_AUTH": "dummy-xdg-auth",
            "Xdg-Auth": "dummy-xdg-auth",
            "XDG_A-U-T-H": "dummy-xdg-auth",
            "omniagentos_bridge_session_auth": "dummy-bridge-auth",
            "OMNIAGENTOS-BRIDGE-SESSION-AUTH": "dummy-bridge-auth",
            "OMNIAGENTOS_BRIDGE_SESSION_AUTH": "dummy-bridge-auth",
            "MY_AUTH_TOKEN": "dummy-auth-token",
            "API_SECRET": "dummy-api-secret",
        }
        session = {"OMNIAGENTOS_BRIDGE_SESSION_ID": "abc-def-123"}

        result = _scrubbed_env(paths | credentials | session)

        assert all(result[name] == value for name, value in paths.items())
        assert result == paths | session
        assert not (result.keys() & credentials.keys())

    def test_ssh_agent_socket_is_not_passed(self) -> None:
        """SSH_AUTH_SOCK is a live credential channel, not a hygiene var."""
        result = _scrubbed_env({"SSH_AUTH_SOCK": "/tmp/agent.sock", "PATH": "/bin"})
        assert "SSH_AUTH_SOCK" not in result

    def test_default_uses_os_environ(self) -> None:
        import os

        original = os.environ.get("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY")
        try:
            os.environ["ACMEUNI_STRIPE_PRIMARY_SECRET_KEY"] = "sk_live_leak"
            result = _scrubbed_env()
            assert "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY" not in result
        finally:
            if original is None:
                os.environ.pop("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", None)
            else:
                os.environ["ACMEUNI_STRIPE_PRIMARY_SECRET_KEY"] = original


class TestAccountRoutingSurvivesTheScrub:
    """The live regression: a scrub that drops the config-dir pointer does not
    harden the spawn, it silently runs the CLI as the WRONG account."""

    def test_provider_config_dir_pointers_are_kept(self) -> None:
        base = {
            "CLAUDE_CONFIG_DIR": "/Users/x/.claude-account-3",
            "CODEX_HOME": "/Users/x/.codex",
            "GROK_HOME": "/Users/x/.grok",
            "GEMINI_CLI_HOME": "/Users/x/.gemini",
            "KIMI_CODE_HOME": "/Users/x/.kimi-code",
            "QWEN_HOME": "/Users/x/.qwen",
            "PATH": "/bin",
        }
        assert _scrubbed_env(base) == base

    def test_config_dir_is_a_path_not_a_credential(self) -> None:
        """Sanity: the value is passed through verbatim and nothing reads inside it."""
        result = _scrubbed_env({"CLAUDE_CONFIG_DIR": "/Users/x/.claude-account-3"})
        assert result["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-account-3"

    def test_explicit_override_wins_over_the_inherited_value(self) -> None:
        """_invoke() merges env_overrides AFTER the scrub, so a pooled account's
        selection always beats whatever the parent exported (adapters/claude.py)."""
        scrubbed = _scrubbed_env({"CLAUDE_CONFIG_DIR": "/inherited", "PATH": "/bin"})
        scrubbed.update({"CLAUDE_CONFIG_DIR": "/pooled-account"})
        assert scrubbed["CLAUDE_CONFIG_DIR"] == "/pooled-account"


class TestKeychainSessionHandles:
    """macOS securityd/Keychain lookups resolve against the login session; these
    handles are opaque identifiers, not secrets."""

    def test_macos_session_vars_are_kept_when_the_parent_has_them(self) -> None:
        base = {
            "SECURITYSESSIONID": "186b9",
            "__CF_USER_TEXT_ENCODING": "0x1F5:0x0:0x0",
            "__CFBundleIdentifier": "com.apple.Terminal",
            "LaunchInstanceID": "ABC-123",
            "TERM_SESSION_ID": "w0t0p0",
            "XPC_FLAGS": "0x0",
            "XPC_SERVICE_NAME": "0",
            "PATH": "/bin",
        }
        assert _scrubbed_env(base) == base

    def test_safe_hygiene_and_locale_vars_are_kept(self) -> None:
        base = {
            "HOME": "/home/user",
            "PATH": "/usr/local/bin:/usr/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": "/tmp",
            "XDG_CONFIG_HOME": "/home/user/.config",
        }
        result = _scrubbed_env(base)
        assert result == base  # every one of these is on the safe allowlist


class TestSpawnSitesScrub:
    def test_scrubbed_env_covers_both_adapter_spawn_sites(self) -> None:
        """Both CLI spawn sites (Popen in _invoke, subprocess.run in health) scrub."""
        import inspect

        from omniagentos.adapters.common import CliAdapter

        assert "_scrubbed_env()" in inspect.getsource(CliAdapter._invoke)
        assert "_scrubbed_env()" in inspect.getsource(CliAdapter.health)

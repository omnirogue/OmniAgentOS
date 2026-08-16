from omniagentos.fleetcap.attribution import attribute


def test_human_dispatch_uses_device_owner() -> None:
    assert attribute({"n_user": 1, "device_owner": "emp_owner"})[:2] == ("human", "emp_owner")


def test_owned_user_session_without_hook_states_weak_basis() -> None:
    result = attribute({"n_user": 1, "device_owner": "emp_owner"})
    assert "n_user>0, no hook evidence" in result[2]


def test_daemon_dispatch_uses_swarm_cwd() -> None:
    kind, dispatcher, evidence = attribute({"cwd": "/repo/var/swarm/run-42/task"})
    assert kind == "daemon"
    assert dispatcher == "daemon:swarm-run-42"
    assert "hook/cwd" in evidence


def test_headless_hook_with_user_turns_is_daemon() -> None:
    kind, dispatcher, _ = attribute(
        {"n_user": 3, "device_owner": "emp_owner", "hook": {"source": "startup", "tty": ""}}
    )
    assert (kind, dispatcher) == ("daemon", "daemon:startup")


def test_real_headless_tty_payloads_are_daemon() -> None:
    for payload in (
        {"tty": "", "tty_raw": "??", "interactive": False, "source": "startup"},
        {"tty": "", "tty_raw": "?", "interactive": False, "source": "startup"},
    ):
        assert attribute({"n_user": 2, "device_owner": "emp_owner", "hook": payload})[0] == "daemon"


def test_interactive_hook_beats_worktree_cwd() -> None:
    facts = {
        "cwd": "/private/tmp/bld-human",
        "n_user": 2,
        "device_owner": "emp_owner",
        "hook": {"tty": "ttys004", "interactive": True, "cwd": "/private/tmp/bld-human"},
    }
    assert attribute(facts)[:2] == ("human", "emp_owner")


def test_subagent_dispatch_derives_parent() -> None:
    kind, dispatcher, _ = attribute(
        {"path": "/tmp/parent-session/subagents/agent-a1.jsonl", "n_sidechain": 2}
    )
    assert kind == "subagent"
    assert dispatcher == "subagent:parent-session"


def test_unknown_without_decisive_signal() -> None:
    assert attribute({"n_user": 0})[:2] == ("unknown", None)

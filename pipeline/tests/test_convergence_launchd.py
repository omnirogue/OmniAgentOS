from __future__ import annotations

import plistlib
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent
LAUNCHD = PIPELINE / "launchd"
SERVING_REPO = Path("/Users/youruser/OmniAgentOS")


def test_converged_plists_execute_only_from_grok_pipeline() -> None:
    for path in sorted(LAUNCHD.glob("*.plist")):
        body = path.read_text(encoding="utf-8")
        assert "/Users/youruser/Work/Ops/ThreeLoops" not in body
        data = plistlib.loads(path.read_bytes())
        assert data["WorkingDirectory"] == str(SERVING_REPO / "pipeline")
        argv = data["ProgramArguments"]
        assert any(str(SERVING_REPO / "pipeline" / "bridge") in arg for arg in argv)


def test_gate_daemon_is_bound_to_the_one_repo_and_queue() -> None:
    data = plistlib.loads((LAUNCHD / "com.threeloops.gate-loop.plist").read_bytes())
    argv = data["ProgramArguments"]
    assert argv[0] == str(SERVING_REPO / ".venv" / "bin" / "python")
    assert argv[argv.index("--repo") + 1] == str(SERVING_REPO)
    assert argv[argv.index("--repo-key") + 1] == "omniagentos"
    assert argv[argv.index("--loops-root") + 1] == str(SERVING_REPO / "var" / "loopqueue")


def test_installer_replaces_old_jobs_and_verifies_the_serving_path() -> None:
    body = (LAUNCHD / "install.sh").read_text(encoding="utf-8")
    assert 'launchctl bootout "$DOMAIN/$label"' in body
    assert 'launchctl bootstrap "$DOMAIN" "$DEST/$plist"' in body
    assert 'grep -q "$REPO/pipeline/bridge"' in body
    assert 'local_head" = "$remote_head' in body


def test_loop_restart_is_explicit_and_rebinds_all_three_prompts() -> None:
    body = (LAUNCHD / "restart-loops.sh").read_text(encoding="utf-8")
    assert "--apply is required" in body
    for role in ("planning", "reviewer", "implementer"):
        assert f'"$RUN_LOOP" {role} ' in body
    assert '"loop-$role"' in body

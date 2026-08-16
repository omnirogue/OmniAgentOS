"""FROZEN acceptance check for fx_021_tooldense_command_metadata.

This acceptance check is copied in after the agent finishes to prevent the agent
from editing or weakening it.
"""

from __future__ import annotations

import importlib

import index
import router


def test_acceptance_command_metadata() -> None:
    # 1. Expected table data
    table = [
        ("build", "Compile the project artifacts.", False, ("b", "make")),
        ("clean", "Remove build output and caches.", True, ("cl",)),
        ("deploy", "Ship the current build to an environment.", True, ("dep", "ship")),
        ("doctor", "Diagnose the local environment.", False, ("doc", "dr")),
        ("fetch", "Download remote dependencies.", False, ("f", "pull")),
        ("init", "Create a new project skeleton.", False, ("new",)),
        ("lint", "Check the source for style problems.", False, ("l",)),
        ("migrate", "Apply pending database migrations.", True, ("mig", "mg")),
        ("publish", "Upload a release to the registry.", True, ("pub",)),
        ("rollback", "Revert the last deployment.", True, ("rb", "undo")),
        ("status", "Show the current project state.", False, ("st", "info")),
        ("verify", "Run the acceptance checks.", False, ("v", "check")),
    ]

    # Sort table by command name to match COMMANDS expectation
    sorted_table = sorted(table, key=lambda x: x[0])

    # 2. Check each module's META
    for name, summary, danger, aliases in table:
        mod = importlib.import_module(f"commands.{name}")
        assert hasattr(mod, "META"), f"Module '{name}' is missing 'META'"
        meta = mod.META
        assert isinstance(meta, dict), f"META in '{name}' must be a dict"
        assert meta.get("name") == name, f"META['name'] in '{name}' must be '{name}'"
        assert meta.get("summary") == summary, f"META['summary'] in '{name}' must be '{summary}'"
        assert meta.get("danger") is danger, f"META['danger'] in '{name}' must be {danger}"
        assert meta.get("aliases") == aliases, f"META['aliases'] in '{name}' must be {aliases}"

    # 3. Check index.COMMANDS
    assert len(index.COMMANDS) == 12, f"Expected 12 commands in index, got {len(index.COMMANDS)}"
    for idx, (name, summary, danger, aliases) in enumerate(sorted_table):
        cmd = index.COMMANDS[idx]
        assert cmd.get("name") == name, (
            f"At index {idx}, expected '{name}', got '{cmd.get('name')}'"
        )
        assert cmd.get("summary") == summary
        assert cmd.get("danger") is danger
        assert cmd.get("aliases") == aliases

    # 4. Check index.find resolves every name and alias
    # Build maps to check unique mappings
    name_to_cmd = {}
    for name, summary, danger, aliases in table:
        expected_meta = {
            "name": name,
            "summary": summary,
            "danger": danger,
            "aliases": aliases,
        }
        name_to_cmd[name] = expected_meta
        for alias in aliases:
            name_to_cmd[alias] = expected_meta

    # Run find for all names and aliases
    for key, expected_meta in name_to_cmd.items():
        resolved = index.find(key)
        assert resolved == expected_meta, f"find('{key}') returned incorrect metadata"

    # assert find raises UnknownCommand for "nope"
    try:
        index.find("nope")
        raise AssertionError("Expected index.find('nope') to raise UnknownCommand")
    except index.UnknownCommand:
        pass
    except KeyError:
        raise AssertionError(
            "index.find('nope') raised bare KeyError instead of UnknownCommand subclass"
        ) from None

    # 5. Check alias uniqueness across all command names and aliases
    all_tokens = []
    for name, _, _, aliases in table:
        all_tokens.append(name)
        all_tokens.extend(aliases)
    assert len(all_tokens) == len(set(all_tokens)), (
        "Duplicate command name or alias detected in prompt spec!"
    )

    # 6. Check index.help_text() is byte-exact
    # Longest name is "rollback" (len 8).
    expected_help_lines = [
        "   build  Compile the project artifacts.",
        "   clean  Remove build output and caches. [danger]",
        "  deploy  Ship the current build to an environment. [danger]",
        "  doctor  Diagnose the local environment.",
        "   fetch  Download remote dependencies.",
        "    init  Create a new project skeleton.",
        "    lint  Check the source for style problems.",
        " migrate  Apply pending database migrations. [danger]",
        " publish  Upload a release to the registry. [danger]",
        "rollback  Revert the last deployment. [danger]",
        "  status  Show the current project state.",
        "  verify  Run the acceptance checks.",
    ]
    expected_help = "\n".join(expected_help_lines)
    actual_help = index.help_text()
    assert actual_help == expected_help, (
        f"Help text mismatch.\nExpected:\n{repr(expected_help)}\nActual:\n{repr(actual_help)}"
    )

    # 7. Check router.dispatch
    # Test dispatch returns 2 for empty argv
    assert router.dispatch([]) == 2, "dispatch([]) must return 2"

    # Test dispatch returns 2 for unknown command
    assert router.dispatch(["nope"]) == 2, "dispatch(['nope']) must return 2"
    assert router.dispatch(["nope", "arg1"]) == 2, "dispatch(['nope', 'arg1']) must return 2"

    # Test dispatch to build with no args
    assert router.dispatch(["build"]) == 0, "dispatch(['build']) must return 0"
    # Test dispatch to build with args
    assert router.dispatch(["build", "a", "b"]) == 2, "dispatch(['build', 'a', 'b']) must return 2"

    # Test dispatch to clean (danger=True) via name and alias
    assert router.dispatch(["clean"]) == 1, "dispatch(['clean']) must return 1"
    assert router.dispatch(["cl", "a"]) == 2, "dispatch(['cl', 'a']) must return 2"

    # Test dispatch to deploy (danger=True) via alias
    assert router.dispatch(["dep", "x", "y"]) == 4, "dispatch(['dep', 'x', 'y']) must return 4"
    assert router.dispatch(["ship", "x", "y"]) == 4, "dispatch(['ship', 'x', 'y']) must return 4"

    # Test dispatch to rollback (danger=True) via alias
    assert router.dispatch(["undo", "x", "y", "z"]) == 12, "dispatch(['undo']) must return 12"

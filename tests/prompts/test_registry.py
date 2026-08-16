"""Tests for the system-prompt registry loader.

Two halves:

1. Tests against the REAL ``system-prompts/ROLE-REGISTRY.yaml`` — they prove the
   shipped inventory still describes reality, so a moved or emptied prompt file
   fails here rather than at 04:15 inside a daemon.
2. Tests against synthetic registries written to ``tmp_path`` — they prove every
   failure mode raises its own error instead of degrading to an empty string,
   which is the whole point of this loader existing beside the fail-soft
   ``promptshape.rolepack``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.prompts import (
    KINDS,
    LOCATIONS,
    REGISTRY_RELATIVE_PATH,
    EmptyPromptError,
    PromptFileMissingError,
    PromptRegistryError,
    RegistryFileError,
    UnknownRoleError,
    UnresolvablePromptError,
    get_prompt,
    get_role,
    list_roles,
    load_registry,
    repo_root,
)
from omniagentos.prompts.registry import _clear_cache

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_stale_cache() -> Any:
    """The loader caches by (path, mtime, size); tests rewrite files fast."""
    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture
def real_registry() -> Any:
    return load_registry()


def _write_registry(root: Path, roles: list[dict[str, Any]], *, schema_version: Any = 1) -> Path:
    """Write a synthetic registry at ``root/system-prompts/ROLE-REGISTRY.yaml``."""
    path = root / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"schema_version": schema_version, "roles": roles}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "example.hello",
        "description": "A synthetic role for tests.",
        "owner": "tests",
        "version": 1,
        "live": False,
        "kind": "agent",
        "location": "registry",
        "prompt_file": "system-prompts/example.hello/v1.md",
    }
    base.update(overrides)
    return base


def _seed_prompt(root: Path, relative: str, text: str = "You are a test prompt.\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The shipped registry describes reality                                      #
# --------------------------------------------------------------------------- #


def test_real_registry_loads() -> None:
    registry = load_registry()
    assert registry.schema_version == 1
    assert registry.path == repo_root() / REGISTRY_RELATIVE_PATH
    assert len(registry) > 0


def test_every_resolvable_entry_points_at_real_nonempty_text(real_registry: Any) -> None:
    """The load-bearing test: a moved or emptied prompt file fails HERE."""
    real_registry.validate_files()


def test_every_entry_is_internally_consistent(real_registry: Any) -> None:
    for entry in real_registry:
        assert entry.location in LOCATIONS
        assert entry.kind in KINDS
        assert entry.version >= 1
        assert entry.description
        assert entry.owner
        if entry.resolvable:
            assert entry.prompt_file is not None
            assert entry.source_ref is None
        else:
            assert entry.source_ref is not None
            assert entry.prompt_file is None


def test_registry_owned_prompts_live_under_system_prompts(real_registry: Any) -> None:
    for entry in real_registry:
        if entry.location == "registry":
            assert entry.prompt_file == f"system-prompts/{entry.id}/v{entry.version}.md"
            assert real_registry.prompt_path(entry.id).is_file()


def test_the_fourteen_job_roles_are_all_registered(real_registry: Any) -> None:
    """The JobRole enum is the estate's job vocabulary; the registry must cover it.

    Coupled to ``omniagentos.roles.JobRole`` on purpose: adding a fifteenth job
    role without registering its prompt should fail a test, not be discovered
    later by an agent running with no contract.
    """
    from omniagentos.roles import JobRole

    registered = {entry.id for entry in real_registry}
    missing = [role.value for role in JobRole if f"job.{role.value}" not in registered]
    assert not missing, f"JobRole values with no registry entry: {missing}"


def test_job_role_entries_point_at_the_files_rolepack_actually_reads(real_registry: Any) -> None:
    """Registry and ``promptshape.rolepack`` must name the same file, not a copy."""
    from omniagentos.roles import JobRole

    for role in JobRole:
        entry = real_registry.get(f"job.{role.value}")
        assert entry.prompt_file == f"vault/prompts/roles/{role.value}.md"
    assert real_registry.get("job.universal-base").prompt_file == "vault/prompts/universal-base.md"


def test_worked_example_resolves_end_to_end() -> None:
    """The documented 'copy this' path must actually work, and not be live."""
    entry = get_role("example.hello")
    assert entry.location == "registry"
    assert entry.live is False
    text = get_prompt("example.hello")
    assert text.strip()
    assert "worked example" in text.lower()


def test_external_entries_are_registered_by_reference_only(real_registry: Any) -> None:
    external = [e for e in real_registry if e.location == "external"]
    assert external, "the ~/.claude/agents and ThreeLoops prompts should be recorded"
    for entry in external:
        assert entry.source_ref is not None
        with pytest.raises(UnresolvablePromptError) as excinfo:
            real_registry.prompt_text(entry.id)
        # The refusal must name where the text really is, or it is not actionable.
        assert entry.source_ref in str(excinfo.value)


def test_embedded_entries_name_their_module_and_constant(real_registry: Any) -> None:
    embedded = [e for e in real_registry if e.location == "embedded"]
    assert embedded
    for entry in embedded:
        assert entry.source_ref is not None
        assert "::" in entry.source_ref, "expected 'path/to/module.py::CONSTANT'"
        module_path = repo_root() / entry.source_ref.split("::", 1)[0]
        assert module_path.is_file(), f"{entry.id} names a module that does not exist"
        constant = entry.source_ref.split("::", 1)[1]
        assert constant in module_path.read_text(encoding="utf-8"), (
            f"{entry.id}: {constant} not found in {module_path}"
        )
        with pytest.raises(UnresolvablePromptError):
            real_registry.prompt_text(entry.id)


def test_repo_entries_name_paths_inside_the_checkout(real_registry: Any) -> None:
    for entry in real_registry:
        if entry.location == "repo":
            assert entry.prompt_file is not None
            assert not entry.prompt_file.startswith("system-prompts/")
            assert (repo_root() / entry.prompt_file).is_file()


def test_list_roles_filters(real_registry: Any) -> None:
    everything = list_roles(registry=real_registry)
    live = list_roles(registry=real_registry, live_only=True)
    assert 0 < len(live) < len(everything), "example.hello is deliberately not live"
    assert all(e.live for e in live)
    daemons = list_roles(registry=real_registry, kind="daemon")
    assert daemons and all(e.kind == "daemon" for e in daemons)
    with pytest.raises(ValueError):
        list_roles(registry=real_registry, kind="not-a-kind")


def test_readme_exists_and_names_the_add_a_prompt_path() -> None:
    """The operator-facing document must keep naming the commands that exist."""
    readme = repo_root() / "system-prompts" / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "ROLE-REGISTRY.yaml" in body
    assert "tests/prompts" in body, "the README must tell the operator how to check their work"
    for command in (
        "python -m omniagentos.prompts list",
        "python -m omniagentos.prompts show",
        "python -m omniagentos.prompts check",
    ):
        assert command in body, f"README no longer documents `{command}`"


# --------------------------------------------------------------------------- #
# Failure modes: every one raises, none degrades to ""                        #
# --------------------------------------------------------------------------- #


def test_unknown_role_raises_and_suggests(tmp_path: Path) -> None:
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md")
    path = _write_registry(tmp_path, [_entry()])
    registry = load_registry(path)
    with pytest.raises(UnknownRoleError) as excinfo:
        registry.prompt_text("example.helo")
    message = str(excinfo.value)
    assert "example.helo" in message
    assert "example.hello" in message, "a near-miss id must be suggested"


def test_unknown_role_never_returns_empty_string(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [])
    registry = load_registry(path)
    assert registry.role_ids() == ()
    with pytest.raises(UnknownRoleError):
        registry.prompt_text("anything")


def test_missing_prompt_file_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry()])  # no file seeded
    registry = load_registry(path)
    with pytest.raises(PromptFileMissingError) as excinfo:
        registry.prompt_text("example.hello")
    assert "system-prompts/example.hello/v1.md" in str(excinfo.value)


def test_blank_prompt_file_raises(tmp_path: Path) -> None:
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md", text="   \n\t\n")
    path = _write_registry(tmp_path, [_entry()])
    registry = load_registry(path)
    with pytest.raises(EmptyPromptError):
        registry.prompt_text("example.hello")


def test_validate_files_surfaces_a_broken_entry(tmp_path: Path) -> None:
    _seed_prompt(tmp_path, "system-prompts/good.one/v1.md")
    path = _write_registry(
        tmp_path,
        [
            _entry(id="good.one", prompt_file="system-prompts/good.one/v1.md"),
            _entry(id="bad.one", prompt_file="system-prompts/bad.one/v1.md"),
        ],
    )
    registry = load_registry(path)
    with pytest.raises(PromptFileMissingError):
        registry.validate_files()


def test_prompt_text_is_verbatim(tmp_path: Path) -> None:
    body = "# Role\n\nLine one.\n\n  indented\ttab\n"
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md", text=body)
    path = _write_registry(tmp_path, [_entry()])
    assert load_registry(path).prompt_text("example.hello") == body


def test_all_errors_share_one_base_class() -> None:
    for exc in (
        RegistryFileError,
        UnknownRoleError,
        PromptFileMissingError,
        EmptyPromptError,
        UnresolvablePromptError,
    ):
        assert issubclass(exc, PromptRegistryError)


# --------------------------------------------------------------------------- #
# Registry-file validation                                                    #
# --------------------------------------------------------------------------- #


def test_absent_registry_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(tmp_path / "system-prompts" / "ROLE-REGISTRY.yaml")
    assert REGISTRY_RELATIVE_PATH in str(excinfo.value)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 1\nroles: [ unclosed\n", encoding="utf-8")
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_top_level_must_be_a_mapping(tmp_path: Path) -> None:
    path = tmp_path / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_omitted_roles_key_raises_rather_than_reading_as_empty(tmp_path: Path) -> None:
    """A typo'd `role:` must not look like a registry with zero roles."""
    path = tmp_path / REGISTRY_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 1\nrole: []\n", encoding="utf-8")
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "roles" in str(excinfo.value)


def test_non_integer_schema_version_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [], schema_version="1")
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_duplicate_ids_raise(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(), _entry(description="A second one.")])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "twice" in str(excinfo.value)


@pytest.mark.parametrize(
    "missing",
    ["id", "description", "owner", "version", "live", "kind", "location"],
)
def test_missing_required_key_raises(tmp_path: Path, missing: str) -> None:
    entry = _entry()
    del entry[missing]
    path = _write_registry(tmp_path, [entry])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert missing in str(excinfo.value)


def test_unrecognised_key_raises(tmp_path: Path) -> None:
    """A misspelled key must not read as an absent value."""
    path = _write_registry(tmp_path, [_entry(prompt_fil="system-prompts/x/v1.md")])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "prompt_fil" in str(excinfo.value)


def test_truthy_string_is_not_accepted_for_live(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(live="yes")])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "live" in str(excinfo.value)


@pytest.mark.parametrize("bad_version", [0, -1, "1", 1.0, True])
def test_bad_version_raises(tmp_path: Path, bad_version: Any) -> None:
    path = _write_registry(tmp_path, [_entry(version=bad_version)])
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_unknown_location_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(location="somewhere")])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "location" in str(excinfo.value)


def test_unknown_kind_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(kind="wizard")])
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_resolvable_location_requires_prompt_file(tmp_path: Path) -> None:
    entry = _entry()
    del entry["prompt_file"]
    path = _write_registry(tmp_path, [entry])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "prompt_file" in str(excinfo.value)


def test_resolvable_location_rejects_source_ref(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(source_ref="~/elsewhere.md")])
    with pytest.raises(RegistryFileError):
        load_registry(path)


@pytest.mark.parametrize("location", ["external", "embedded"])
def test_reference_location_requires_source_ref(tmp_path: Path, location: str) -> None:
    entry = _entry(location=location)
    del entry["prompt_file"]
    path = _write_registry(tmp_path, [entry])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "source_ref" in str(excinfo.value)


@pytest.mark.parametrize("location", ["external", "embedded"])
def test_reference_location_rejects_prompt_file(tmp_path: Path, location: str) -> None:
    path = _write_registry(tmp_path, [_entry(location=location, source_ref="~/x.md")])
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_registry_version_and_filename_must_agree(tmp_path: Path) -> None:
    """Bumping `version` without renaming the file is the drift this forbids."""
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md")
    path = _write_registry(tmp_path, [_entry(version=2)])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "v2.md" in str(excinfo.value), "the error must name the file to create"


def test_registry_location_must_use_the_id_as_its_directory(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(prompt_file="system-prompts/other/v1.md")])
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_repo_location_under_system_prompts_is_refused(tmp_path: Path) -> None:
    """Mislabelling a registry-owned prompt as `repo` would dodge the version check."""
    path = _write_registry(
        tmp_path,
        [_entry(location="repo", prompt_file="system-prompts/example.hello/v1.md")],
    )
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "registry" in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "../outside/prompt.md",
        "vault/prompts/../../../outside.md",
    ],
)
def test_prompt_file_cannot_escape_the_checkout(tmp_path: Path, bad_path: str) -> None:
    path = _write_registry(tmp_path, [_entry(location="repo", prompt_file=bad_path)])
    with pytest.raises(RegistryFileError):
        load_registry(path)


@pytest.mark.parametrize("bad_id", ["", "   ", " padded", "with/slash", "..", "."])
def test_bad_id_raises(tmp_path: Path, bad_id: str) -> None:
    path = _write_registry(tmp_path, [_entry(id=bad_id)])
    with pytest.raises(RegistryFileError):
        load_registry(path)


def test_consumers_must_be_strings(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(consumers=["ok", 7])])
    with pytest.raises(RegistryFileError) as excinfo:
        load_registry(path)
    assert "consumers" in str(excinfo.value)


def test_blank_description_raises(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, [_entry(description="   ")])
    with pytest.raises(RegistryFileError):
        load_registry(path)


# --------------------------------------------------------------------------- #
# Caching                                                                     #
# --------------------------------------------------------------------------- #


def test_cache_returns_the_same_object_for_an_unchanged_file(tmp_path: Path) -> None:
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md")
    path = _write_registry(tmp_path, [_entry()])
    assert load_registry(path) is load_registry(path)


def test_cache_is_invalidated_when_the_registry_changes(tmp_path: Path) -> None:
    _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md")
    path = _write_registry(tmp_path, [_entry()])
    assert load_registry(path).role_ids() == ("example.hello",)

    _seed_prompt(tmp_path, "system-prompts/second.role/v1.md")
    _write_registry(
        tmp_path,
        [_entry(), _entry(id="second.role", prompt_file="system-prompts/second.role/v1.md")],
    )
    # Same path, different content. No manual cache clear here on purpose: the
    # (mtime_ns, size) signature must invalidate on its own, or a prompt edit
    # would be invisible for the life of the process.
    assert load_registry(path).role_ids() == ("example.hello", "second.role")


def test_prompt_text_is_not_cached_stale(tmp_path: Path) -> None:
    """The registry is cached; prompt BODIES are read fresh every call."""
    prompt = _seed_prompt(tmp_path, "system-prompts/example.hello/v1.md", text="first\n")
    path = _write_registry(tmp_path, [_entry()])
    registry = load_registry(path)
    assert registry.prompt_text("example.hello") == "first\n"
    prompt.write_text("second\n", encoding="utf-8")
    assert registry.prompt_text("example.hello") == "second\n"

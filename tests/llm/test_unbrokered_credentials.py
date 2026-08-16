"""U-R9: GEMINI_API_KEY is the SOLE sanctioned unbrokered credential read.

``omniagentos/connectors/broker.py`` is the only module that may resolve a
credential. The one deliberate exception is MODEL_COMPLETE, whose spend is
governed by the llm/ BudgetGuard rather than by a connector grant: it reads
``GEMINI_API_KEY`` directly in ``omniagentos/llm/client.py``, and
``scheduler/loop_effects.py`` declares the exception with ``broker_capability=""``.

WHAT THE FIRST VERSION OF THIS SCANNER COULD NOT SEE (K3)
---------------------------------------------------------
It matched ``os.environ.get("NAME")`` and a bare ``getenv("NAME")`` and nothing
else, so all of these were invisible to it:

* ``os.getenv("NAME")`` — the single most common spelling in this codebase, and
  the form of two LIVE unbrokered reads in ``goals/collect.py`` that the suite
  was green over;
* ``os.environ["NAME"]`` / ``os.environ.pop(...)`` / ``os.environ.setdefault(...)``;
* a name held in a module constant (``os.getenv(ENV_ACCESS_TOKEN)``), which is
  how a read hides in plain sight from a literal-only matcher.

It was also FILE-granular: ``llm/client.py`` and ``scheduler/loop_effects.py``
were allow-listed wholesale, so any new credential read added beside the
sanctioned one was absorbed silently — and ``loop_effects.py``, which contains
zero scanner-visible reads, was pure absorption capacity.

WHAT IT DOES NOW
----------------
* recognises every ``os.environ`` / ``os.getenv`` spelling above;
* folds module-level string constants so a name held in a constant is judged by
  its VALUE, and falls back to the name EXPRESSION's own text when it cannot be
  resolved (``os.getenv(brand.stripe_key_env)`` is credential-shaped on its
  face);
* allow-lists per SITE, not per file: an anchor declares the exact env names it
  may read, so a second read inside an anchor file fails;
* and refuses to carry a stale inventory: a file listed as a known violation
  that no longer reads a credential must be removed from the list.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest


def _find_omniagentos_root() -> Path:
    """Locate the repository root from this test file's location.

    The marker is `pyproject.toml` AND an `omniagentos/` directory, which is the
    same conjunction `omniagentos/harnesses/release_gate.py:default_repo_root`
    uses, and for the reason discovered here: an `omniagentos/` directory alone
    is not a unique marker. A test package named `tests/omniagentos/` satisfied
    it from `tests/`, so this scan walked ONE test file instead of the 750-file
    package and reported every real anchor as vanished and every inventoried
    read as stale — a fail-OPEN on the credential-read inventory, arrived at by
    a directory name. Fail closed instead: no marker, no scan.
    """
    current = Path(__file__).resolve()
    while current.parent != current:
        if (current / "pyproject.toml").is_file() and (current / "omniagentos").is_dir():
            return current
        current = current.parent
    raise RuntimeError("Could not find the repository root (pyproject.toml + omniagentos/)")


OMNIAGENTOS_ROOT = _find_omniagentos_root()
OMNIAGENTOS_PATH = OMNIAGENTOS_ROOT / "omniagentos"

#: The sanctioned exception, allow-listed per SITE rather than per file: each
#: anchor names the exact set of credential-shaped env names it may read.
#:
#: - ``llm/client.py`` performs the one unbrokered read (GEMINI_API_KEY).
#: - ``scheduler/loop_effects.py`` DECLARES the exception (broker_capability="")
#:   and reads nothing, so its permitted set is empty. Listing it with an empty
#:   set is the point: it keeps the declaration anchored without giving the file
#:   room to absorb a future read.
ALLOWED_ANCHORS: dict[str, frozenset[str]] = {
    "llm/client.py": frozenset({"GEMINI_API_KEY"}),
    "scheduler/loop_effects.py": frozenset(),
}

#: Pre-existing unbrokered reads, tracked for the T4.8 migration. This is an
#: INVENTORY, not a permission: it exists so that a NEW read anywhere fails
#: immediately, and it is asserted to be exact in both directions (nothing
#: missing, nothing stale). Every entry names why it is here.
#: NAME-granular like ALLOWED_ANCHORS (review 2026-08-14, finding F1): a flat
#: file set let a listed file absorb ANY future read silently — and it did:
#: sessions/notify.py had grown two reads beyond its comment, and the
#: steward/alerts/monitor.py comment named a variable the file does not read.
#: Each file now declares the EXACT env-name set the scanner may observe.
KNOWN_VIOLATIONS: dict[str, frozenset[str]] = {
    # --- real credentials read outside the broker (T4.8 migration targets) ---
    "api/routes/knowledge.py": frozenset({"OMNIAGENTOS_OPERATOR_TOKEN"}),  # operator auth
    "archdocs/narrative.py": frozenset({"GEMINI_API_KEY"}),  # second, unsanctioned site
    "comms/pollers/slack.py": frozenset({"SLACK_BOT_TOKEN"}),
    "comms/pollers/telegram.py": frozenset({"TELEGRAM_BOT_TOKEN"}),
    "lab/eval/_worker.py": frozenset({"EVAL_KEY_ENV"}),  # protected-eval encryption key
    "lab/surfaces/__init__.py": frozenset({"OMNIAGENTOS_OPERATOR_TOKEN"}),  # operator auth
    # notify: the ntfy topic URL is a secret; the dashboard URL and ops webhook
    # are credential-SHAPED names this file also reads (surfaced by the
    # name-granular conversion — previously absorbed silently).
    "sessions/notify.py": frozenset(
        {"OMNI_NTFY_URL", "OMNI_DASHBOARD_URL", "OPS_ALERT_SLACK_WEBHOOK_URL"}
    ),
    "steward/alerts/monitor.py": frozenset({"MONEY_ALERT_SLACK_WEBHOOK_URL"}),
    # --- landed 08-11..08-14 through the continue-on-error CI window; U-R9 ---
    # Inventoried 2026-08-14 (get-main-green). Same shape as the already-listed
    # comms/pollers/slack.py read; the honest fix is the T4.8 broker routing,
    # tracked as its own lane. Name-granular: a NEW name in ANY file fails.
    "edc/main.py": frozenset({"SLACK_BOT_TOKEN"}),  # EDC decision notifier
    "team/balance_alerts.py": frozenset({"SLACK_BOT_TOKEN"}),
    "team/decisions.py": frozenset({"SLACK_BOT_TOKEN"}),
    "team/notify.py": frozenset({"SLACK_BOT_TOKEN"}),
    "team/overnight.py": frozenset({"SLACK_BOT_TOKEN"}),
    "team/report.py": frozenset({"SLACK_BOT_TOKEN"}),
    "team/session_tracker.py": frozenset({"SLACK_BOT_TOKEN", "WQ_TOKEN"}),
    "workqueue/client.py": frozenset({"WQ_TOKEN"}),  # cross-machine workqueue auth
    # Inventoried 2026-08-15 (get-main-green): the estate-compute readers
    # (fusion(compute-backend), landed 08-14) poll the workqueue pool with the
    # same bearer token as workqueue/client.py above; the env-then-
    # CONNECTIONS_ENV fallback shape mirrors scripts/ops/wq_offload.py's
    # load_token() (readers.py's own docstring cites it). Honest fix stays the
    # T4.8 broker routing.
    "compute/readers.py": frozenset({"WQ_TOKEN"}),
    # 0776a565 brokered steward PiedPiper email, removing its former direct token read.
    # --- name matches the credential SHAPE but denotes a budget or a flag ---
    # Kept in the inventory rather than pattern-excluded on purpose: a
    # "these look like credentials but are not" exception in the DETECTOR is a
    # bypass waiting to be widened, whereas a list entry is reviewable.
    "api/main.py": frozenset({"OMNIAGENTOS_MINT_TOKEN_ON_STARTUP"}),  # a boolean flag
    "api/routes/chats.py": frozenset({"CHAT_HISTORY_MAX_TOKENS"}),  # a token BUDGET
    "knowledge/config.py": frozenset({"ENV_BUDGET_TOKENS"}),  # a token BUDGET
    "memory/config.py": frozenset({"OMNIAGENTOS_MEMORY_BUDGET_TOKENS"}),  # a token BUDGET
}

#: Files exempt from the scan entirely, with the reason.
_EXEMPT: dict[str, str] = {
    "connectors/broker.py": "the broker IS the credential boundary",
    "connectors/secrets_env.py": "declares credential name PATTERNS, reads no values",
}

_CREDENTIAL_NAME_RE = re.compile(
    r"(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIAL|_URL)",
    re.IGNORECASE,
)

#: ``os.environ`` methods that hand back a value.
_ENVIRON_READERS = frozenset({"get", "pop", "setdefault"})


def _is_environ(node: ast.AST) -> bool:
    """``os.environ`` (attribute form) or a ``from os import environ`` name."""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "LITERAL"`` bindings, for folding indirect reads."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def _env_name(arg: ast.AST, constants: dict[str, str]) -> str:
    """The env var name this expression names, or its source text as a fallback.

    Falling back to the expression TEXT is what makes a non-literal read
    visible: ``os.getenv(brand.stripe_key_env)`` cannot be resolved statically,
    but it says what it is. A read whose name expression carries no credential
    shape at all (``os.environ.get(name)``) is genuinely undecidable here and is
    left to the runtime boundary rather than guessed at.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in constants:
        return constants[arg.id]
    return ast.unparse(arg)


def _find_credential_reads(file_path: Path) -> list[tuple[int, str]]:
    """Every credential-shaped environment read in one file: (line, env name)."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    constants = _module_string_constants(tree)
    found: list[tuple[int, str]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            reader = False
            if isinstance(func, ast.Attribute):
                if func.attr in _ENVIRON_READERS and _is_environ(func.value):
                    reader = True  # os.environ.get / .pop / .setdefault
                elif (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    reader = True  # os.getenv
            elif isinstance(func, ast.Name) and func.id == "getenv":
                reader = True  # from os import getenv
            if reader and node.args:
                name = _env_name(node.args[0], constants)
                if _CREDENTIAL_NAME_RE.search(name):
                    found.append((node.lineno, name))
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if _is_environ(node.value):
                name = _env_name(node.slice, constants)
                if _CREDENTIAL_NAME_RE.search(name):
                    found.append((node.lineno, name))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sorted(found)


def _scan_package() -> dict[str, list[tuple[int, str]]]:
    """Every non-exempt file in ``omniagentos/`` that reads a credential name."""
    reads: dict[str, list[tuple[int, str]]] = {}
    for py_file in OMNIAGENTOS_PATH.rglob("*.py"):
        relative = str(py_file.relative_to(OMNIAGENTOS_PATH)).replace("\\", "/")
        if relative in _EXEMPT:
            continue
        found = _find_credential_reads(py_file)
        if found:
            reads[relative] = found
    return reads


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_no_unbrokered_credential_read_outside_the_anchor_or_the_inventory() -> None:
    """A NEW unbrokered credential read anywhere in omniagentos/ fails here.

    Name-granular in BOTH lists: an unlisted file fails on any read, and a
    listed file fails on any read of a name outside its declared set — an
    inventoried file is not a place where new reads land unreviewed.
    """
    reads = _scan_package()
    violations: dict[str, list[tuple[int, str]]] = {}
    for path, sites in reads.items():
        if path in ALLOWED_ANCHORS:
            continue  # the anchor test asserts its exact set
        permitted = KNOWN_VIOLATIONS.get(path)
        if permitted is None:
            violations[path] = sites
        else:
            extra = [(line, name) for line, name in sites if name not in permitted]
            if extra:
                violations[path] = extra

    if violations:
        lines = [
            "NEW unbrokered credential reads found (U-R9).",
            "Route them through omniagentos/connectors/broker.py, or add the exact",
            "env name to the file's KNOWN_VIOLATIONS set with a written",
            "justification if it is a false positive.",
            "",
        ]
        for path in sorted(violations):
            lines.append(f"  {path}:")
            lines.extend(f"    line {line}: {name}" for line, name in violations[path])
        raise AssertionError("\n".join(lines))


def test_an_anchor_may_read_only_the_names_it_declares() -> None:
    """Site-granular, so an anchor file cannot absorb a second read.

    File-granular allow-listing made ``llm/client.py`` a place where any future
    credential read would be accepted without review, and gave
    ``loop_effects.py`` — which reads nothing — an unused slot of the same kind.
    """
    for anchor, permitted in ALLOWED_ANCHORS.items():
        path = OMNIAGENTOS_PATH / anchor
        assert path.exists(), f"anchor {anchor} no longer exists"
        actual = {name for _line, name in _find_credential_reads(path)}
        assert actual == permitted, (
            f"{anchor} reads {sorted(actual)} but is sanctioned for "
            f"{sorted(permitted)}. Adding an unbrokered read to an anchor file "
            "is a U-R9 change and needs its own review."
        )


def test_the_inventory_is_exact_in_both_directions() -> None:
    """No missing entries, and no stale ones.

    An inventory that is allowed to keep names of files that no longer read a
    credential slowly becomes a permission list nobody re-derives.
    """
    reads = _scan_package()
    stale = sorted(set(KNOWN_VIOLATIONS) - set(reads))
    assert stale == [], (
        f"{stale} are listed as known unbrokered reads but the scanner finds none. "
        "Remove them: a stale inventory entry is standing permission for a read "
        "that would otherwise have to be reviewed."
    )
    for path, permitted in KNOWN_VIOLATIONS.items():
        observed = {name for _line, name in reads.get(path, [])}
        stale_names = sorted(permitted - observed)
        assert stale_names == [], (
            f"{path} declares {stale_names} but the scanner no longer observes "
            "them. Remove the stale names: an unused declared name is standing "
            "permission for a read that would otherwise have to be reviewed."
        )
    overlap = sorted(set(KNOWN_VIOLATIONS) & set(ALLOWED_ANCHORS))
    assert overlap == [], f"{overlap} is both an anchor and a known violation"


def test_the_sanctioned_exception_is_still_wired_where_it_claims_to_be() -> None:
    """If the exception moves or disappears, the allow-list must not linger."""
    client = (OMNIAGENTOS_PATH / "llm/client.py").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" in client

    loop = (OMNIAGENTOS_PATH / "scheduler/loop_effects.py").read_text(encoding="utf-8")
    assert 'broker_capability=""' in loop
    assert "MODEL_COMPLETE" in loop


# ---------------------------------------------------------------------------
# The detector itself (the part that was previously asserted as a constant)
# ---------------------------------------------------------------------------


_SYNTHETIC_FORMS = [
    ("os_getenv", 'os.getenv("SYNTHETIC_API_KEY")', "SYNTHETIC_API_KEY"),
    ("os_environ_get", 'os.environ.get("SYNTHETIC_API_KEY")', "SYNTHETIC_API_KEY"),
    ("os_environ_subscript", 'os.environ["SYNTHETIC_API_KEY"]', "SYNTHETIC_API_KEY"),
    ("os_environ_pop", 'os.environ.pop("SYNTHETIC_API_KEY", None)', "SYNTHETIC_API_KEY"),
    ("bare_getenv", 'getenv("SYNTHETIC_API_KEY")', "SYNTHETIC_API_KEY"),
    ("bare_environ_get", 'environ.get("SYNTHETIC_API_KEY")', "SYNTHETIC_API_KEY"),
    ("bare_environ_subscript", 'environ["SYNTHETIC_API_KEY"]', "SYNTHETIC_API_KEY"),
    ("named_token", 'os.getenv("SYNTHETIC_ACCESS_TOKEN")', "SYNTHETIC_ACCESS_TOKEN"),
    ("named_secret", 'os.getenv("SYNTHETIC_CLIENT_SECRET")', "SYNTHETIC_CLIENT_SECRET"),
]


@pytest.mark.parametrize(("label", "expression", "expected"), _SYNTHETIC_FORMS)
def test_the_detector_sees_every_environment_read_spelling(
    tmp_path: Path, label: str, expression: str, expected: str
) -> None:
    """Inject a synthetic module per spelling and require the scanner to see it.

    This replaces a "self-test" that asserted ``len(ALLOWED_ANCHORS) == 2`` — a
    statement about a constant in this file, true no matter what the scanner
    could or could not detect, and green while two live ``os.getenv`` reads sat
    undetected in ``goals/collect.py``.
    """
    module = tmp_path / f"synthetic_{label}.py"
    module.write_text(
        textwrap.dedent(
            f"""
            import os
            from os import environ, getenv


            def read() -> str | None:
                return {expression}
            """
        ),
        encoding="utf-8",
    )

    found = _find_credential_reads(module)
    assert [name for _line, name in found] == [expected], (
        f"the {label} spelling is invisible to the scanner; a credential read "
        "written this way would never be reported"
    )


def test_the_detector_resolves_a_name_held_in_a_module_constant(tmp_path: Path) -> None:
    """The indirection that hides a read from a literal-only matcher."""
    module = tmp_path / "synthetic_constant.py"
    module.write_text(
        textwrap.dedent(
            """
            import os

            ENV_NAME = "SYNTHETIC_SERVICE_TOKEN"
            HARMLESS = "SYNTHETIC_MODE"


            def read() -> str | None:
                os.getenv(HARMLESS)
                return os.getenv(ENV_NAME)
            """
        ),
        encoding="utf-8",
    )

    assert [name for _line, name in _find_credential_reads(module)] == ["SYNTHETIC_SERVICE_TOKEN"]


def test_the_detector_flags_an_unresolvable_but_credential_shaped_expression(
    tmp_path: Path,
) -> None:
    """``os.getenv(brand.stripe_key_env)`` says what it is even unresolved."""
    module = tmp_path / "synthetic_dynamic.py"
    module.write_text(
        textwrap.dedent(
            """
            import os


            def read(brand, name):
                os.getenv(name)  # undecidable, and shaped like nothing
                return os.getenv(brand.stripe_key_env)
            """
        ),
        encoding="utf-8",
    )

    found = [name for _line, name in _find_credential_reads(module)]
    assert found == ["brand.stripe_key_env"]


def test_the_detector_does_not_fire_on_a_non_credential_read(tmp_path: Path) -> None:
    """The control: a scanner that flags everything decides nothing either."""
    module = tmp_path / "synthetic_clean.py"
    module.write_text(
        textwrap.dedent(
            """
            import os

            VAR_DIR = os.getenv("OMNIAGENTOS_VAR_DIR", "var")
            MODE = os.environ.get("OMNIAGENTOS_MODE", "auto")
            DEPTH = os.environ["OMNIAGENTOS_MAX_DEPTH"]
            """
        ),
        encoding="utf-8",
    )

    assert _find_credential_reads(module) == []


def test_a_second_read_added_to_an_anchor_file_is_rejected(tmp_path: Path) -> None:
    """The absorption case, exercised rather than described.

    Copy the real anchor, add one more credential read to it, and require the
    per-site rule to refuse the result. Under the previous file-granular
    allow-list this addition was accepted silently.
    """
    anchor = OMNIAGENTOS_PATH / "llm/client.py"
    tampered = tmp_path / "client.py"
    tampered.write_text(
        anchor.read_text(encoding="utf-8")
        + '\n\n_SMUGGLED = os.environ.get("SMUGGLED_API_KEY", "")\n',
        encoding="utf-8",
    )

    names = {name for _line, name in _find_credential_reads(tampered)}
    assert names == {"GEMINI_API_KEY", "SMUGGLED_API_KEY"}
    assert names != ALLOWED_ANCHORS["llm/client.py"], (
        "the anchor's declared name set must not admit a second read"
    )

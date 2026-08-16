"""The other half of the reachability fix: routes whose caller IS the framework.

WHY THIS FILE EXISTS AND WHY IT LANDS WITH THE STRICTNESS CHANGE
---------------------------------------------------------------
Tightening caller detection to AST-verified call sites (see
``test_reachability_gate_false_green.py``) closes a false-GREEN — and, on its own, makes a
known false-RED worse. A FastAPI route handler has no named call site anywhere: the caller
is the framework, reached through ``@router.post(...)`` plus ``app.include_router(router)``.
Under word-match the handler often passed by accident; under strict matching it can only be
refused, so every new route would need a hand-written exemption line forever. Two entries in
``devtasks/REACHABILITY-EXEMPT.txt`` existed for exactly that reason and no other, which is
the receipt that this is real rather than anticipated.

So the two changes are a pair. These tests pin the pairing:

  - a routed handler on a router that IS wired into an app is reachable with zero named
    call sites;
  - a routed handler on a router that is NEVER ``include_router``'d is STILL REFUSED. That
    is the counterfeit case — "I decorated it, therefore it is wired" — and recognising
    registration without checking the registration would just be a new false-GREEN;
  - registration is proven by AST, not by the word ``include_router`` appearing in a file,
    and a wiring line that exists only in tests does not wire production;
  - mountedness is a CLOSURE, computed from the application outward. A router is mounted
    because an app mounts it, or because a mounted router mounts it — never merely because
    somebody, somewhere, passed it to ``include_router``. An aggregator nobody serves is a
    counterfeit with an extra hop in it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ROUTES_MODULE = (
    "from fastapi import APIRouter\n\n"
    'router = APIRouter(prefix="/api/notifications", tags=["notifications"])\n\n\n'
    '@router.post("/read-all")\n'
    "def mark_all_notifications_read() -> dict[str, int]:\n"
    '    """No named caller anywhere: the framework dispatches this."""\n'
    "    return {'marked': 0}\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def reachability_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Reachability Gate Test")
    _git(repo, "config", "user.email", "reachability-gate@example.com")
    gate = repo / "scripts" / "reachability-gate.py"
    gate.parent.mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "reachability-gate.py", gate)
    gate.chmod(0o755)
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "candidate")
    return repo


def _commit_candidate(repo: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/reachability-gate.py", "candidate", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_wired_route_handler_needs_no_named_call_site(reachability_repo: Path) -> None:
    """The real false-RED: a handler on an included router, called by nobody by name."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        "A route handler on a router wired via include_router() is reachable — the caller "
        "is the framework.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_route_handler_on_never_included_router_still_refuses(
    reachability_repo: Path,
) -> None:
    """COUNTERFEIT CASE: decorated, but the router is wired into nothing.

    This must refuse both before and after framework recognition lands. A decorator is a
    claim of registration; only ``include_router`` makes it true.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.other import router as other_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(other_router)\n"
            ),
            "omniagentos/api/routes/other.py": (
                "from fastapi import APIRouter\n\nrouter = APIRouter()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        "A handler on a router that is never include_router'd is unreachable.\n"
        f"stdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_commented_out_include_router_does_not_wire_anything(
    reachability_repo: Path,
) -> None:
    """Registration is read by AST, never by the word ``include_router`` in a file."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n"
                "# TODO: app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A commented-out include_router() wires nothing.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_include_router_only_in_tests_does_not_wire_production(
    reachability_repo: Path,
) -> None:
    """A test harness that mounts the router is not a production registration."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/tests/test_notifications_route.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "def test_marks() -> None:\n"
                "    app = FastAPI()\n"
                "    app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"Only a test mounts this router; production never does.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_handler_decorated_on_the_app_itself_is_reachable(
    reachability_repo: Path,
) -> None:
    """``@app.get(...)`` on a FastAPI() instance needs no include_router."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/health.py": (
                "from fastapi import FastAPI\n\n"
                "asgi_app = FastAPI()\n\n\n"
                '@asgi_app.get("/healthz")\n'
                "def health_probe() -> dict[str, str]:\n"
                "    return {'status': 'ok'}\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A handler decorated on the application object is registered at import.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_include_router_as_a_keyword_argument_is_recognised(
    reachability_repo: Path,
) -> None:
    """``include_router(router=x)`` mounts exactly as ``include_router(x)`` does."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n"
                'app.include_router(router=notifications_router, prefix="/v2")\n'
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, f"Keyword form is the same mounting.\nstdout:\n{result.stdout}"


def test_router_passed_in_as_a_parameter_wires_nothing(reachability_repo: Path) -> None:
    """``def mount(app, router): app.include_router(router)`` names no particular router.

    The parameter shadows nothing knowable; treating it as this module's ``router`` would
    let any module with a helper like this launder its own handlers into 'mounted'.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": (
                ROUTES_MODULE
                + "\n\ndef _mount(app, router) -> None:\n    app.include_router(router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A parameter named 'router' mounts nothing in particular.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_module_qualified_include_router_is_recognised(reachability_repo: Path) -> None:
    """``from pkg import module`` + ``app.include_router(module.router)`` also wires."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes import notifications\n\n"
                "app = FastAPI()\n"
                "app.include_router(notifications.router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"Module-qualified registration is registration.\nstdout:\n{result.stdout}"
    )


def test_relative_import_include_router_is_recognised(reachability_repo: Path) -> None:
    """Relative imports resolve to the same module identity as absolute ones."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from .routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A relative import wires the same router.\nstdout:\n{result.stdout}"
    )


AGGREGATOR = (
    "from fastapi import APIRouter\n\n"
    "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
    "router = APIRouter()\n"
    "router.include_router(notifications_router)\n"
)


def test_router_included_into_an_included_parent_router_is_reachable(
    reachability_repo: Path,
) -> None:
    """Nesting, mounted end to end: app -> aggregate -> notifications.

    Read this together with the test below it. On its own it proves nothing about the
    closure, because "anything passed to include_router is mounted" also passes it — which
    is exactly how a decorative test earns its keep in a review and nowhere else.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/routes/aggregate.py": AGGREGATOR,
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.aggregate import router as aggregate_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(aggregate_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A child router mounted on a mounted parent is reachable.\nstdout:\n{result.stdout}"
    )


def test_handler_mounted_only_into_an_unmounted_aggregator_refuses(
    reachability_repo: Path,
) -> None:
    """THE CLOSURE TEST: identical to the test above, minus the one line that serves it.

    ``aggregate.py`` mounts the notifications router, and nothing mounts ``aggregate``. No
    request can reach the handler. A gate that treats any include_router target as mounted
    calls this GREEN — which is the original defect with one extra hop bolted on, and the
    hop is free to add. Mountedness must be computed FROM the application outward.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/routes/aggregate.py": AGGREGATOR,
            "omniagentos/api/main.py": ("from fastapi import FastAPI\n\napp = FastAPI()\n"),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"Nothing serves the aggregator, so nothing reaches the handler.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_deep_mount_chain_is_reachable_only_when_the_root_is_served(
    reachability_repo: Path,
) -> None:
    """Three hops: app -> v1 -> section -> notifications. The closure has to iterate."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/routes/section.py": (
                "from fastapi import APIRouter\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "router = APIRouter()\n"
                "router.include_router(notifications_router)\n"
            ),
            "omniagentos/api/routes/v1.py": (
                "from fastapi import APIRouter\n\n"
                "from omniagentos.api.routes.section import router as section_router\n\n"
                "router = APIRouter()\n"
                "router.include_router(section_router)\n"
            ),
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.v1 import router as v1_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(v1_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"Every hop is served, so the handler is reachable.\nstdout:\n{result.stdout}"
    )


def test_application_named_variable_that_is_actually_a_router_refuses(
    reachability_repo: Path,
) -> None:
    """``app = APIRouter()`` is a router with a misleading name, not an application.

    Trusting the NAME re-opens the whole hole: any module can call its router ``app``,
    decorate handlers on it, mount it nowhere, and read as served. What makes an object an
    application is what it was constructed from.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/sneaky.py": (
                "from fastapi import APIRouter\n\n"
                "app = APIRouter()\n\n\n"
                '@app.get("/sneaky")\n'
                "def sneaky_handler() -> dict[str, str]:\n"
                "    return {'ok': 'yes'}\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A router called 'app' is still a router.\nstdout:\n{result.stdout}"
    )
    assert "sneaky_handler()" in result.stdout


def test_handler_on_an_imported_application_is_reachable(
    reachability_repo: Path,
) -> None:
    """The honest version of the case above: the app really is a FastAPI(), imported."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/main.py": ("from fastapi import FastAPI\n\napp = FastAPI()\n"),
            "omniagentos/api/routes/health.py": (
                "from omniagentos.api.main import app\n\n\n"
                '@app.get("/healthz")\n'
                "def health_probe() -> dict[str, str]:\n"
                "    return {'status': 'ok'}\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"An imported FastAPI() is an application.\nstdout:\n{result.stdout}"
    )


def test_include_router_inside_a_type_checking_block_wires_nothing(
    reachability_repo: Path,
) -> None:
    """``if TYPE_CHECKING:`` is dead code at runtime; mounting there mounts nothing."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/main.py": (
                "from typing import TYPE_CHECKING\n\n"
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n\n"
                "if TYPE_CHECKING:\n"
                "    app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A mount that never executes is not a mount.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_router_reexported_through_package_init_is_recognised(
    reachability_repo: Path,
) -> None:
    """The real ``omniagentos.lab.api.routes`` shape: mount the package's re-export.

    ``main.py`` says ``from omniagentos.lab.api.routes import router`` — a name that package's
    ``__init__`` re-exported from ``...routes.lab``. Mounting the re-export mounts the same
    object, so the handler in ``lab.py`` is reachable.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/lab/api/routes/lab.py": ROUTES_MODULE,
            "omniagentos/lab/api/routes/__init__.py": (
                "from omniagentos.lab.api.routes.lab import router\n\n__all__ = ['router']\n"
            ),
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.lab.api.routes import router as lab_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(lab_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A re-exported router that is mounted is mounted.\nstdout:\n{result.stdout}"
    )


def test_reexported_but_unmounted_router_still_refuses(reachability_repo: Path) -> None:
    """Re-export resolution must not become a way to pass without being mounted."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/lab/api/routes/lab.py": ROUTES_MODULE,
            "omniagentos/lab/api/routes/__init__.py": (
                "from omniagentos.lab.api.routes.lab import router\n\n__all__ = ['router']\n"
            ),
            "omniagentos/api/main.py": ("from fastapi import FastAPI\n\napp = FastAPI()\n"),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, f"Nothing mounts this router.\nstdout:\n{result.stdout}"
    assert "mark_all_notifications_read()" in result.stdout


def test_reexport_chain_stops_at_the_module_that_builds_the_router(
    reachability_repo: Path,
) -> None:
    """A mounted module importing the NAME ``router`` does not mount someone else's router.

    ``aggregate.py`` is mounted and happens to import a name called ``router`` from the
    notifications module before building its own. That import is not a mounting, and the
    notifications handler must still be refused.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": ROUTES_MODULE,
            "omniagentos/api/routes/aggregate.py": (
                "from fastapi import APIRouter\n\n"
                "from omniagentos.api.routes.notifications import router\n\n"
                "router = APIRouter()\n"
            ),
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.aggregate import router as aggregate_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(aggregate_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"Importing a name is not mounting a router.\nstdout:\n{result.stdout}"
    )
    assert "mark_all_notifications_read()" in result.stdout


def test_non_route_decorator_grants_no_reachability(reachability_repo: Path) -> None:
    """Only route decorators mean 'the framework calls this' — not decorators in general."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/api/routes/notifications.py": (
                "from functools import lru_cache\n\n"
                "from fastapi import APIRouter\n\n"
                "router = APIRouter()\n\n\n"
                "@lru_cache(maxsize=1)\n"
                "def expensive_lookup() -> int:\n"
                "    return 7\n"
            ),
            "omniagentos/api/main.py": (
                "from fastapi import FastAPI\n\n"
                "from omniagentos.api.routes.notifications import router as notifications_router\n\n"
                "app = FastAPI()\n"
                "app.include_router(notifications_router)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"An @lru_cache'd helper in a routed module is not a route.\nstdout:\n{result.stdout}"
    )
    assert "expensive_lookup()" in result.stdout

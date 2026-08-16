"""HTTP tests for the P2 mounts browse API.

Exercises the pinned contract against the REAL app:

  * ``GET /api/mounts`` -- list configured mounts
  * ``GET /api/mounts/{id}/dir`` -- shallow single-directory listing
  * ``GET /api/mounts/{id}/file`` -- text/image/binary file preview

Every test writes its OWN throwaway ``mounts.yaml`` and swaps
``omniagentos.mounts._default_mounts_path`` to point at it (cache-cleared per
test), so nothing here depends on -- or perturbs -- the real
``configs/mounts.yaml``/process cache. The suite-wide autouse fixture in
``tests/conftest.py`` bypasses the session-token gate for every test except the
dedicated auth probes at the bottom, which opt back in with
``@pytest.mark.real_auth`` (mirrors ``tests/api/test_control_plane_auth.py``).
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.routes.mounts as mounts_routes
import omniagentos.mounts as mounts_module
from omniagentos.api.main import app
from omniagentos.sessions import token


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def write_mounts_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Point the P1 registry loader at a throwaway YAML for the life of a test."""

    def _write(body: str) -> Path:
        target = tmp_path / "mounts.yaml"
        target.write_text(textwrap.dedent(body), encoding="utf-8")
        monkeypatch.setattr(mounts_module, "_default_mounts_path", lambda: target)
        mounts_module.load_mounts.cache_clear()
        return target

    yield _write
    mounts_module.load_mounts.cache_clear()


@pytest.fixture
def client() -> Iterator[httpx.AsyncClient]:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield c
    finally:
        _run(c.aclose())


@pytest.fixture
def home_and_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A fake $HOME (with real credential-store subpaths) plus a 'work' tree.

    The shared secret registry (``omniagentos.policy.secrets.secret_dirs``)
    resolves ``~/.ssh`` etc. against the REAL ``$HOME`` env var, so the secret-
    deny tests point ``$HOME`` at this fixture's tree rather than relying on the
    mount's configured path alone.
    """
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))

    (home / "visible.txt").write_text("hello from home", encoding="utf-8")
    (home / ".hidden").write_text("dotfile", encoding="utf-8")
    (home / "node_modules").mkdir()
    (home / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
    (home / "Library").mkdir()
    (home / "Library" / "junk").write_text("x", encoding="utf-8")
    subdir = home / "subdir"
    subdir.mkdir()
    (subdir / "Library").write_text("not the top-level one", encoding="utf-8")
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("fake-private-key", encoding="utf-8")
    omni_cfg = home / ".config" / "omni"
    omni_cfg.mkdir(parents=True)
    (omni_cfg / "connections.env").write_text("SECRET=x", encoding="utf-8")

    (work / "notes.txt").write_text("y" * 100, encoding="utf-8")
    (work / "connections.env").write_text("SECRET=y", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("should never be reachable", encoding="utf-8")
    (work / "escape").symlink_to(outside)

    return {"home": home, "work": work, "tmp": tmp_path}


def _yaml_for(home: Path, work: Path, *, work_exists: bool = True) -> str:
    ghost = work.parent / "ghost-mount"
    return f"""
    version: 1
    mounts:
      - id: home
        label: Home
        path: "{home}"
        kind: local
        grantable: false
        read_only: true
      - id: work
        label: Work
        path: "{work if work_exists else ghost}"
        kind: local
        grantable: true
        read_only: false
    """


# --- GET /api/mounts ----------------------------------------------------------


def test_list_mounts_contract(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts"))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"mounts"}
    ids = {m["id"] for m in body["mounts"]}
    assert ids == {"home", "work"}
    for row in body["mounts"]:
        assert set(row.keys()) == {
            "id",
            "label",
            "path",
            "resolved",
            "kind",
            "cloud",
            "grantable",
            "read_only",
            "exists",
            "notes",
        }
    home_row = next(m for m in body["mounts"] if m["id"] == "home")
    assert home_row["grantable"] is False
    assert home_row["exists"] is True
    # fix7: 'resolved' is the realpath'd absolute path (usable as a grant root),
    # distinct from the raw configured 'path'.
    assert home_row["resolved"] == str(home_and_work["home"].resolve())
    assert home_row["resolved"].startswith("/")


# --- GET /api/mounts/{id}/dir --------------------------------------------------


def test_browse_dir_lists_shallow_dirs_first_and_skips_noise(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/home/dir", params={"path": ""}))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mount_id"] == "home"
    names = [e["name"] for e in body["entries"]]
    # Dot-prefixed entries, node_modules, and the home-top-level noise list are
    # all hidden; visible.txt and subdir remain.
    assert ".hidden" not in names
    assert ".ssh" not in names
    assert ".config" not in names
    assert "node_modules" not in names
    assert "Library" not in names
    assert "visible.txt" in names
    assert "subdir" in names
    # Dirs sorted before files.
    dir_flags = [e["is_dir"] for e in body["entries"]]
    first_file_index = next(i for i, is_dir in enumerate(dir_flags) if not is_dir)
    assert all(dir_flags[:first_file_index])
    assert not body["truncated"]


def test_browse_dir_only_skips_library_at_top_level(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/home/dir", params={"path": "subdir"}))
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["entries"]]
    # 'Library' inside a non-root subdir is an ordinary file, not the top-level
    # skip target.
    assert "Library" in names


def test_browse_dir_pagination(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    many = home_and_work["work"] / "many"
    many.mkdir()
    for i in range(10):
        (many / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    resp = _run(
        client.get("/api/mounts/work/dir", params={"path": "many", "offset": 0, "limit": 4})
    )
    body = resp.json()
    assert body["total"] == 10
    assert len(body["entries"]) == 4
    assert body["truncated"] is True
    tail = _run(
        client.get("/api/mounts/work/dir", params={"path": "many", "offset": 8, "limit": 4})
    )
    tail_body = tail.json()
    assert len(tail_body["entries"]) == 2
    assert tail_body["truncated"] is False


def test_browse_dir_rejects_traversal_before_fs_access(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    for bad_path in ("../etc", "/etc/passwd", "a/../../etc", "a\x00b"):
        resp = _run(client.get("/api/mounts/work/dir", params={"path": bad_path}))
        assert resp.status_code == 403, (
            f"{bad_path!r} should have been rejected, got {resp.status_code}"
        )


def test_browse_dir_rejects_symlink_escape(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/work/dir", params={"path": "escape"}))
    assert resp.status_code in (403, 404)


def test_browse_dir_denies_secret_directory_even_though_dot_hides_it_from_listings(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/home/dir", params={"path": ".ssh"}))
    assert resp.status_code == 403


def test_browse_dir_unknown_mount_is_404(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/does-not-exist/dir"))
    assert resp.status_code == 404


def test_browse_dir_nonexistent_mount_path_is_error(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"], work_exists=False))
    resp = _run(client.get("/api/mounts/work/dir"))
    assert resp.status_code == 409


# --- GET /api/mounts/{id}/file -------------------------------------------------


def test_read_file_text_preview(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "notes.txt"}))
    assert resp.status_code == 200
    assert resp.text == "y" * 100


def test_read_file_oversized_text_is_413(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    big = home_and_work["work"] / "big.txt"
    big.write_bytes(b"a" * (1_000_001))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "big.txt"}))
    assert resp.status_code == 413


def test_read_file_binary_unknown_extension_is_415(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    binfile = home_and_work["work"] / "blob.bin"
    binfile.write_bytes(bytes(range(256)))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "blob.bin"}))
    assert resp.status_code == 415


def test_read_file_image_streams(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    img = home_and_work["work"] / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    resp = _run(client.get("/api/mounts/work/file", params={"path": "pic.png"}))
    assert resp.status_code == 200
    assert resp.content == b"\x89PNG\r\n\x1a\nfake"


def test_read_file_rejects_traversal(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "../outside.txt"}))
    assert resp.status_code == 403


def test_read_file_denies_ssh_key_via_home_mount(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/home/file", params={"path": ".ssh/id_rsa"}))
    assert resp.status_code == 403


def test_read_file_denies_connections_env_via_home_mount(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(
        client.get("/api/mounts/home/file", params={"path": ".config/omni/connections.env"})
    )
    assert resp.status_code == 403


def test_read_file_denies_connections_env_by_basename_via_any_mount(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    """A connections.env copied into an unrelated mount is still refused --
    the secret-basename check is not directory-scoped (matches SECRET_BASENAMES
    regardless of which mount root the file lives under)."""
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "connections.env"}))
    assert resp.status_code == 403


def test_read_file_unknown_mount_is_404(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/nope/file", params={"path": "x.txt"}))
    assert resp.status_code == 404


def test_read_file_missing_is_404(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/work/file", params={"path": "nope.txt"}))
    assert resp.status_code == 404


# --- home read-guard: loose dotfiles + top-level skip as access control -------


def test_read_file_denies_loose_home_dotfile_claude_json(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    """~/.claude.json holds a live token yet is ABSENT from the secret registry;
    the home-dotfile parity guard (not the secret check) must still 403 it."""
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    (home_and_work["home"] / ".claude.json").write_text('{"token": "x"}', encoding="utf-8")
    resp = _run(client.get("/api/mounts/home/file", params={"path": ".claude.json"}))
    assert resp.status_code == 403


def test_read_file_denies_loose_home_env(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    (home_and_work["home"] / ".env").write_text("SECRET=x", encoding="utf-8")
    resp = _run(client.get("/api/mounts/home/file", params={"path": ".env"}))
    assert resp.status_code == 403


def test_browse_dir_denies_library_direct_request_on_home(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    """The home top-level skip list is a real access control on DIRECT requests,
    not just a listing filter -- home/dir?path=Library is refused, not listed."""
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    resp = _run(client.get("/api/mounts/home/dir", params={"path": "Library"}))
    assert resp.status_code == 403


def test_read_file_denies_symlink_named_without_dot_to_home_dotfile(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    """A non-dotfile-named symlink inside the home mount that RESOLVES to a home
    dotfile is caught by the (post-resolve) dotfile guard, not smuggled through."""
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    home = home_and_work["home"]
    (home / ".claude.json").write_text('{"token": "x"}', encoding="utf-8")
    (home / "notsecret").symlink_to(home / ".claude.json")
    resp = _run(client.get("/api/mounts/home/file", params={"path": "notsecret"}))
    assert resp.status_code == 403


def test_cloud_mounts_under_library_stay_readable(
    client: httpx.AsyncClient, write_mounts_yaml: Any, home_and_work: dict[str, Path]
) -> None:
    """The cloud mounts live UNDER ~/Library (first component 'Library', no dot)
    but carry their own ids, so neither the dotfile guard nor the home-only skip
    list may block them -- an icloud/gdrive file read must still succeed."""
    home = home_and_work["home"]
    work = home_and_work["work"]
    icloud_dir = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    gdrive_dir = home / "Library" / "CloudStorage" / "GoogleDrive-x" / "My Drive"
    icloud_dir.mkdir(parents=True)
    gdrive_dir.mkdir(parents=True)
    (icloud_dir / "note.txt").write_text("icloud ok", encoding="utf-8")
    (gdrive_dir / "note.txt").write_text("gdrive ok", encoding="utf-8")
    write_mounts_yaml(
        f"""
        version: 1
        mounts:
          - id: home
            label: Home
            path: "{home}"
            grantable: false
            read_only: true
          - id: work
            label: Work
            path: "{work}"
            grantable: true
          - id: icloud
            label: iCloud
            path: "{icloud_dir}"
            kind: icloud
            cloud: true
          - id: gdrive
            label: Google Drive
            path: "{gdrive_dir}"
            kind: gdrive
            cloud: true
        """
    )
    ic = _run(client.get("/api/mounts/icloud/file", params={"path": "note.txt"}))
    assert ic.status_code == 200
    assert ic.text == "icloud ok"
    gd = _run(client.get("/api/mounts/gdrive/file", params={"path": "note.txt"}))
    assert gd.status_code == 200
    assert gd.text == "gdrive ok"


def test_download_oversize_text_is_413(
    client: httpx.AsyncClient,
    write_mounts_yaml: Any,
    home_and_work: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fix4: the download=true branch enforces _MAX_FILE_BYTES BEFORE streaming a
    FileResponse -- it no longer bypasses the size ceiling."""
    write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))
    monkeypatch.setattr(mounts_routes, "_MAX_FILE_BYTES", 8)
    big = home_and_work["work"] / "download.txt"
    big.write_text("this is well over eight bytes", encoding="utf-8")
    resp = _run(
        client.get("/api/mounts/work/file", params={"path": "download.txt", "download": True})
    )
    assert resp.status_code == 413
    # A small file still downloads fine under the same (patched) cap.
    small = home_and_work["work"] / "small.txt"
    small.write_text("ok", encoding="utf-8")
    ok = _run(client.get("/api/mounts/work/file", params={"path": "small.txt", "download": True}))
    assert ok.status_code == 200
    assert ok.text == "ok"


# --- app-level session-token gate (real_auth) ----------------------------------


@pytest.mark.real_auth
class TestMountsRoutesAreGated:
    @pytest.fixture(autouse=True)
    def _mounts(self, write_mounts_yaml: Any, home_and_work: dict[str, Path]) -> None:
        write_mounts_yaml(_yaml_for(home_and_work["home"], home_and_work["work"]))

    @pytest.fixture
    def token_header(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
        monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
        return {"X-Session-Token": token.load_or_create_token()}

    def test_list_mounts_401_without_token(self, client: httpx.AsyncClient) -> None:
        resp = _run(client.get("/api/mounts"))
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_dir_401_without_token(self, client: httpx.AsyncClient) -> None:
        resp = _run(client.get("/api/mounts/home/dir"))
        assert resp.status_code == 401

    def test_file_401_without_token(self, client: httpx.AsyncClient) -> None:
        resp = _run(client.get("/api/mounts/work/file", params={"path": "notes.txt"}))
        assert resp.status_code == 401

    def test_list_mounts_forbidden_to_machine_token_but_reachable_to_asserted_principal(
        self, client: httpx.AsyncClient, token_header: dict[str, str]
    ) -> None:
        resp = _run(client.get("/api/mounts", headers=token_header))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "system_principal_forbidden"

        operator_response = _run(
            client.get(
                "/api/mounts",
                headers={**token_header, "X-Omni-Authenticated-Principal": "human:operator"},
            )
        )
        assert operator_response.status_code == 200

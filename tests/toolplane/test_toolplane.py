from __future__ import annotations

from omniagentos.toolplane.manifest import ManifestValidationError, load_manifest
from omniagentos.toolplane.tools import dispatch


def manifest_for(tmp_path, **overrides):
    data = {
        "run_id": "run-1",
        "session_id": "session-1",
        "holder_generation": 3,
        "read_roots": [str(tmp_path / "read")],
        "write_roots": [str(tmp_path / "write")],
        "allowed_ops": ["read_file", "write_file", "hash_file"],
    }
    data.update(overrides)
    (tmp_path / "read").mkdir(exist_ok=True)
    (tmp_path / "write").mkdir(exist_ok=True)
    return load_manifest(data)


def test_out_of_scope_read_denied(tmp_path):
    manifest = manifest_for(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not allowed")

    result = dispatch("read_file", manifest, {"path": str(outside)})

    assert result["ok"] is False
    assert result["error"] == "out_of_scope"


def test_out_of_scope_write_denied(tmp_path):
    manifest = manifest_for(tmp_path)
    outside = tmp_path / "outside.txt"

    result = dispatch("write_file", manifest, {"path": str(outside), "content": "not allowed"})

    assert result["ok"] is False
    assert result["error"] == "out_of_scope"
    assert not outside.exists()


def test_secret_scrubbing_on_output(tmp_path):
    manifest = manifest_for(tmp_path)
    source = tmp_path / "read" / "safe.txt"
    source.write_text(
        "token=sk-this-is-a-secret-key\n"
        "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----"
    )

    result = dispatch("read_file", manifest, {"path": str(source)})

    assert result["ok"] is True
    content = result["result"]["content"]
    assert "sk-this-is-a-secret-key" not in content
    assert "private material" not in content
    assert "[REDACTED]" in content


def test_holder_generation_required(tmp_path):
    raw = {
        "run_id": "run-1",
        "session_id": "session-1",
        "read_roots": [str(tmp_path)],
        "write_roots": [str(tmp_path)],
        "allowed_ops": ["read_file"],
    }

    try:
        load_manifest(raw)
    except ManifestValidationError as exc:
        assert exc.error == "missing_holder_generation"
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("manifest without holder_generation was accepted")


def test_denied_attempts_return_structured_error_without_secret_content(tmp_path):
    manifest = manifest_for(tmp_path)
    secret_named_path = tmp_path / "outside-sk-super-secret-value.txt"

    result = dispatch("read_file", manifest, {"path": str(secret_named_path)})

    assert result == {
        "ok": False,
        "error": "out_of_scope",
        "detail": "read path is outside granted roots",
    }
    assert "sk-super-secret-value" not in str(result)


def test_in_scope_write_read_and_hash(tmp_path):
    manifest = manifest_for(
        tmp_path,
        read_roots=[str(tmp_path / "read"), str(tmp_path / "write")],
    )
    target = tmp_path / "write" / "nested" / "output.txt"

    written = dispatch("write_file", manifest, {"path": str(target), "content": "hello"})
    read = dispatch("read_file", manifest, {"path": str(target)})
    hashed = dispatch("hash_file", manifest, {"path": str(target)})

    assert written["ok"] is True
    assert read["result"]["content"] == "hello"
    assert (
        hashed["result"]["sha256"]
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )

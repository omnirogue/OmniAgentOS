"""Pins bridge/notify.py — the phone-push leg of every loop alert.

Before this file, a loop alert went ONLY to ALERTS.md, a file nobody watches
without a live session (OMNI_NTFY_URL was unset and absent from the estate
secret store). These tests pin the contract that makes an alert reachable:

  (a) the durable ALERTS.md line is ALWAYS written — even with the push URL
      unset, and even when the push fails; the push is additive, never a
      replacement for the file record;
  (b) when OMNI_NTFY_URL IS set, the alert is POSTed with its title in the
      X-Title header and its body as the request body;
  (c) FAIL-SOFT: an unset URL, a network error, or a non-2xx status must
      NEVER raise into the loop — push_alert returns None and (on a genuine
      failure) leaves a one-line transport-failure note in ALERTS.md.

The wiring is also pinned end-to-end at the two named alert writers
(bridge/advice_writer.py's _append_alert and bridge/claim.py's alert_once):
one alert = one ALERTS.md line + at most one push.
"""

from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import notify  # noqa: E402


class _FakeResp:
    """Minimal stand-in for the urlopen() context manager."""

    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status


def _alerts_text(root: Path) -> str:
    p = root / "ALERTS.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# --------------------------------------------------------------------------
# (b) POSTs when the URL is set, with title + body
# --------------------------------------------------------------------------
def test_push_posts_title_and_body_when_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["title"] = req.get_header("X-title")
        captured["priority"] = req.get_header("X-priority")
        captured["tags"] = req.get_header("X-tags")
        captured["timeout"] = timeout
        return _FakeResp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    status = notify.push_alert(
        tmp_path, title="loop is stuck", body="advice.json is 40 min old",
        priority="high", tags="warning",
    )

    assert status == 200
    assert captured["url"] == "https://ntfy.sh/omni-alerts-test"
    assert captured["method"] == "POST"
    assert captured["body"] == b"advice.json is 40 min old"
    assert captured["title"] == "loop is stuck"
    assert captured["priority"] == "high"
    assert captured["tags"] == "warning"
    # a short timeout must be enforced so a slow push can't stall the loop
    assert captured["timeout"] is not None and captured["timeout"] <= 10
    # a successful push leaves NO transport-failure note
    assert "did NOT reach the push channel" not in _alerts_text(tmp_path)


def test_push_explicit_url_arg_overrides_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    status = notify.push_alert(tmp_path, "t", "b", url="https://ntfy.sh/omni-alerts-x")
    assert status == 200
    assert seen["url"] == "https://ntfy.sh/omni-alerts-x"


# --------------------------------------------------------------------------
# (c) fail-soft: unset URL
# --------------------------------------------------------------------------
def test_push_unset_url_is_noop_and_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)

    def boom(*a, **k):  # must never be reached when URL is unset
        raise AssertionError("urlopen called with no URL configured")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)

    # returns None, does not raise, and writes NO note (unset is normal, not a failure)
    assert notify.push_alert(tmp_path, "t", "b") is None
    assert _alerts_text(tmp_path) == ""


def test_push_blank_url_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "   ")
    assert notify.push_alert(tmp_path, "t", "b") is None
    assert _alerts_text(tmp_path) == ""


# --------------------------------------------------------------------------
# (c) fail-soft: network error / non-2xx must not raise, must leave a note
# --------------------------------------------------------------------------
def test_push_network_error_is_swallowed_and_noted(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")

    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    # must NOT raise
    assert notify.push_alert(tmp_path, "t", "b", source="claim.py") is None
    note = _alerts_text(tmp_path)
    assert "did NOT reach the push channel" in note
    assert "claim.py" in note


def test_push_non_2xx_is_swallowed_and_noted(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")
    monkeypatch.setattr(
        notify.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(500)
    )
    assert notify.push_alert(tmp_path, "t", "b") is None
    assert "HTTP 500" in _alerts_text(tmp_path)


# --------------------------------------------------------------------------
# (a) wiring: the durable ALERTS.md line is ALWAYS written at both call sites
# --------------------------------------------------------------------------
def test_advice_writer_always_writes_line_even_without_push(tmp_path, monkeypatch):
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    from bridge import advice_writer

    advice_writer._append_alert(tmp_path, "advice is stale", source="advice")
    line = _alerts_text(tmp_path)
    assert "advice: advice is stale" in line


def test_advice_writer_pushes_when_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")
    from bridge import advice_writer

    calls = []
    monkeypatch.setattr(
        advice_writer._notify.urllib.request,
        "urlopen",
        lambda req, timeout=None: calls.append((req.get_header("X-title"), req.data)) or _FakeResp(200),
    )
    advice_writer._append_alert(tmp_path, "advice is stale", source="advice")
    assert "advice is stale" in _alerts_text(tmp_path)  # (a) line still written
    assert len(calls) == 1  # (b) pushed exactly once
    assert calls[0][1] == b"advice is stale"


def test_claim_alert_once_writes_line_and_pushes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")
    from bridge import claim

    calls = []
    monkeypatch.setattr(
        claim._notify.urllib.request,
        "urlopen",
        lambda req, timeout=None: calls.append(req.data) or _FakeResp(200),
    )

    claim.alert_once(tmp_path, "item-42", "claim stuck at cap", source="claim.py")
    # first alert: one file line + one push
    assert _alerts_text(tmp_path).count("claim stuck at cap") == 1
    assert len(calls) == 1

    # dedup: the SAME key must neither re-write the line nor re-push
    claim.alert_once(tmp_path, "item-42", "claim stuck at cap", source="claim.py")
    assert _alerts_text(tmp_path).count("claim stuck at cap") == 1
    assert len(calls) == 1


def test_claim_alert_once_writes_line_even_if_push_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNI_NTFY_URL", "https://ntfy.sh/omni-alerts-test")
    from bridge import claim

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(claim._notify.urllib.request, "urlopen", fake_urlopen)

    # the durable alert must survive a dead push channel, and alert_once must not raise
    claim.alert_once(tmp_path, "k1", "primary alert body", source="claim.py")
    text = _alerts_text(tmp_path)
    assert "primary alert body" in text
    assert "did NOT reach the push channel" in text

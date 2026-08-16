"""Read-only quota telemetry: claude + codex snapshot parsing.

Hermetic — every test builds its own fixture files under tmp_path and points the
collector at them, so the machine's real ~/.claude / ~/.codex are never read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.accounts import usage as u

_FETCHED_MS = 1784839052881  # 2026-07-23T04:37:32Z
_FETCHED_AT = datetime.fromtimestamp(_FETCHED_MS / 1000.0, UTC)


def _utilization(
    limits: list[dict[str, object]] | None = None, **extra: object
) -> dict[str, object]:
    body: dict[str, object] = dict(extra)
    if limits is not None:
        body["limits"] = limits
    return body


def _claude_file(path: Path, utilization: dict[str, object], email: str | None = None) -> None:
    payload: dict[str, object] = {
        "cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS, "utilization": utilization}
    }
    if email:
        payload["oauthAccount"] = {"emailAddress": email}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _limits_fixture() -> list[dict[str, object]]:
    """The real shape the claude CLI writes: session + weekly + per-model weekly."""
    return [
        {
            "kind": "session",
            "group": "session",
            "percent": 83,
            "severity": "warning",
            "resets_at": "2026-07-23T18:49:59.103386+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 17,
            "severity": "normal",
            "resets_at": "2026-07-30T11:59:59.103407+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 98,
            "severity": "critical",
            "resets_at": "2026-07-30T11:59:59.103649+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ]


# ---------------------------------------------------------------------------- claude


def test_claude_parses_session_weekly_and_fable_windows(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(config_dir / ".claude.json", _utilization(_limits_fixture()), "a@example.com")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    assert snapshot.email == "a@example.com"
    by_kind = {w.kind: w for w in snapshot.windows}
    assert set(by_kind) == {"session", "weekly_all", "weekly_scoped"}
    assert by_kind["session"].percent == 83
    assert by_kind["session"].is_active is True
    assert by_kind["weekly_all"].percent == 17
    # The per-model window is the one the operator asked for by name.
    fable = by_kind["weekly_scoped"]
    assert fable.scope_model == "Fable"
    assert fable.label == "Weekly · Fable"
    assert fable.severity == "critical"
    assert fable.resets_at == "2026-07-30T11:59:59.103649+00:00"
    # worst = closest to exhaustion, which is what a compact row shows.
    assert snapshot.worst is not None and snapshot.worst.scope_model == "Fable"


def test_claude_finds_usage_in_sibling_json_when_inner_one_lacks_it(tmp_path: Path) -> None:
    """The default ~/.claude layout: an inner .claude.json with NO usage, the real
    payload at ~/.claude.json. Stopping at the first parseable file reports nothing."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    _claude_file(Path(f"{config_dir}.json"), _utilization(_limits_fixture()), "b@example.com")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    assert snapshot.email == "b@example.com"
    assert snapshot.source == f"{config_dir}.json"
    assert len(snapshot.windows) == 3


def test_claude_falls_back_to_scalar_windows_without_limits_array(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            None,
            five_hour={"utilization": 4, "resets_at": "2026-07-24T01:20:00+00:00"},
            seven_day={"utilization": 82, "resets_at": "2026-07-28T04:00:00+00:00"},
        ),
    )

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    by_kind = {w.kind: w for w in snapshot.windows}
    assert by_kind["session"].percent == 4
    assert by_kind["session"].severity == "normal"
    # Severity is derived when the older format doesn't report one.
    assert by_kind["weekly_all"].percent == 82
    assert by_kind["weekly_all"].severity == "warning"


def test_claude_converts_credits_to_major_units(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": True,
                "monthly_limit": 101000,
                "used_credits": 39836,
                "utilization": 39.44158415841584,
                "currency": "USD",
                "decimal_places": 2,
                "disabled_reason": None,
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits

    assert credits is not None
    assert credits.enabled is True
    assert credits.used == 39836
    assert credits.used_amount == pytest.approx(398.36)
    assert credits.limit_amount == pytest.approx(1010.0)
    assert credits.percent == pytest.approx(39.44, abs=0.01)
    assert credits.currency == "USD"


def test_claude_credits_percent_derived_when_absent(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": False,
                "monthly_limit": 20000,
                "used_credits": 5000,
                "utilization": None,
                "decimal_places": 2,
                "disabled_reason": "out_of_credits",
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits

    assert credits is not None
    assert credits.percent == pytest.approx(25.0)
    assert credits.disabled_reason == "out_of_credits"


def test_claude_staleness_is_reported_not_hidden(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(config_dir / ".claude.json", _utilization(_limits_fixture()))

    fresh = u.collect_claude(str(config_dir), now=_FETCHED_AT + timedelta(minutes=5))
    assert fresh.stale is False
    assert fresh.age_seconds == pytest.approx(300, abs=1)

    old = u.collect_claude(str(config_dir), now=_FETCHED_AT + timedelta(hours=9))
    assert old.available is True  # still usable data...
    assert old.stale is True  # ...but never presented as live
    assert old.age_seconds == pytest.approx(9 * 3600, abs=1)


def test_claude_unmeasured_age_is_never_presented_as_live(tmp_path: Path) -> None:
    """Missing fetchedAtMs must not default stale=False (live).

    Counterfeit that would fake a weaker fix: always set stale=True even when a
    fresh timestamp IS present. This test still requires a measured-fresh
    snapshot to stay stale=False (covered by test_claude_staleness_is_reported_not_hidden).
    Counterfeit that would fake THIS fix alone: hardcode stale=True only when
    percent==0 or windows empty — we have real windows and a low percent.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    # Real usable windows, but the CLI omitted the capture timestamp.
    payload = {
        "cachedUsageUtilization": {
            "utilization": {
                "limits": [
                    {
                        "kind": "session",
                        "percent": 5,
                        "severity": "normal",
                        "is_active": True,
                    },
                    {"kind": "weekly_all", "percent": 10, "severity": "normal"},
                ]
            }
        }
    }
    (config_dir / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    assert snapshot.fetched_at is None
    assert snapshot.age_seconds is None
    # Could not measure age → must not claim the number is live.
    assert snapshot.stale is True


def test_claude_unparseable_fetched_at_ms_is_never_presented_as_live(tmp_path: Path) -> None:
    """Nonempty but unparseable fetchedAtMs is unknown age — never live.

    Counterfeit: `stale = cached.get("fetchedAtMs") is None` treats a present
    garbage value as measured/fresh. Missing-only tests do not catch that.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    payload = {
        "cachedUsageUtilization": {
            "fetchedAtMs": "not-a-number",
            "utilization": {
                "limits": [
                    {
                        "kind": "session",
                        "percent": 5,
                        "severity": "normal",
                        "is_active": True,
                    },
                    {"kind": "weekly_all", "percent": 10, "severity": "normal"},
                ]
            },
        }
    }
    (config_dir / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    assert snapshot.age_seconds is None
    assert snapshot.stale is True


def test_as_float_rejects_booleans_as_unparseable() -> None:
    """bool is a subclass of int; float(False)==0.0 must never be treated as measured.

    Counterfeit: only reject None/str; leave ``isinstance(value, bool)`` unchecked.
    """
    assert u._as_float(False) is None
    assert u._as_float(True) is None
    assert u._as_float(0) == 0.0
    assert u._as_float(17) == 17.0
    assert u._as_float("12.5") == 12.5
    assert u._as_float(None) is None
    assert u._as_float("nope") is None


def test_as_int_rejects_booleans_as_unparseable() -> None:
    """Credit/minute fields must not accept True/False as 1/0."""
    assert u._as_int(False) is None
    assert u._as_int(True) is None
    assert u._as_int(0) == 0
    assert u._as_int(39836) == 39836
    assert u._as_int(12.0) is None
    assert u._as_int("9") is None


def test_claude_boolean_percent_is_never_presented_as_zero_consumption(
    tmp_path: Path,
) -> None:
    """JSON ``percent: false`` must not become available 0% normal.

    Reviewer spot-check: float(False)==0.0 made windows look healthy-empty.
    Counterfeit: reject only True, or only when severity is missing — we use
    severity=normal + sole boolean window so a skip must yield unavailable.
    Counterfeit: hardcode available=False always — other parse tests still bind.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            [
                {
                    "kind": "session",
                    "percent": False,  # JSON boolean, not a measured 0
                    "severity": "normal",
                    "is_active": True,
                },
                {
                    "kind": "weekly_all",
                    "percent": True,  # not 1% either
                    "severity": "normal",
                },
            ]
        ),
    )

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    # Unparseable percents → no usable windows → not available-as-zero.
    assert snapshot.available is False
    assert snapshot.windows == []
    assert snapshot.reason is not None
    assert "no usable limit windows" in snapshot.reason


def test_claude_boolean_percent_skips_only_bad_window_keeps_real(
    tmp_path: Path,
) -> None:
    """A bool percent on one entry must not zero that bar; real siblings stay.

    Counterfeit: drop the entire limits[] list when any entry is bool.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            [
                {
                    "kind": "session",
                    "percent": False,
                    "severity": "normal",
                    "is_active": True,
                },
                {
                    "kind": "weekly_all",
                    "percent": 42,
                    "severity": "normal",
                },
            ]
        ),
    )

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is True
    assert [w.kind for w in snapshot.windows] == ["weekly_all"]
    assert snapshot.windows[0].percent == 42.0
    assert all(w.percent != 0.0 or w.kind != "session" for w in snapshot.windows)


def test_claude_boolean_credit_fields_are_never_zeroed(tmp_path: Path) -> None:
    """used_credits=false must not become used=0 / used_amount=0.0 / percent=0.

    ``bool`` is an ``int`` in Python; isinstance(False, int) is True. Counterfeit:
    only fix _as_float and leave credit int fields accepting bool.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": True,
                "monthly_limit": False,  # not zero limit
                "used_credits": False,  # not zero usage
                "utilization": False,  # not zero percent
                "currency": "USD",
                "decimal_places": False,
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits

    assert credits is not None
    assert credits.used is None
    assert credits.limit is None
    assert credits.used_amount is None
    assert credits.limit_amount is None
    assert credits.percent is None  # never flattering 0.0 from bools


def test_claude_unparseable_decimal_places_never_invents_major_amounts(
    tmp_path: Path,
) -> None:
    """Real used/limit + unparseable scale must not become used_amount=used/1.

    Reviewer gate: call-site ``places = _as_int(...)`` was green-on-revert because
    no test bound the scale path. Counterfeit that still fails this test:
    ``decimals = int(places) if isinstance(places, int) else 0`` accepts False as
    0 and invents major units. Counterfeit: only reject when used/limit are also
    bool — here they are real integers.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": True,
                "monthly_limit": 5000,
                "used_credits": 1234,
                "utilization": None,
                "currency": "USD",
                "decimal_places": False,  # present but unparseable — not scale 0
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits

    assert credits is not None
    assert credits.used == 1234
    assert credits.limit == 5000
    # Unknown scale → withhold major units (never 1234.0 / 5000.0 from 10**0).
    assert credits.used_amount is None
    assert credits.limit_amount is None
    # Scale itself must stay unknown — serializing 0 presents unknown as known zero.
    assert credits.decimal_places is None
    # Raw ratio is still honest in minor units.
    assert credits.percent == pytest.approx(24.68, abs=0.01)


def test_claude_missing_decimal_places_never_invents_major_amounts(
    tmp_path: Path,
) -> None:
    """Absent decimal_places is unknown scale — same as unparseable.

    Counterfeit: default scale to 0 only when the key is missing, while still
    inventing amounts; binds the ``places is None`` branch of the caller.
    Counterfeit: withhold amounts but still expose decimal_places=0.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": True,
                "monthly_limit": 5000,
                "used_credits": 1234,
                "utilization": 10.0,
                "currency": "USD",
                # decimal_places omitted on purpose
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits

    assert credits is not None
    assert credits.used == 1234
    assert credits.limit == 5000
    assert credits.used_amount is None
    assert credits.limit_amount is None
    assert credits.decimal_places is None


def test_unknown_decimal_places_serializes_as_json_null_not_zero(
    tmp_path: Path,
) -> None:
    """Public dump must keep unknown scale as JSON null — never 0.

    Counterfeit: model_dump coerce/default to 0 (unknown presented as measured
    scale zero). Counterfeit: omit the key so clients re-default to 0.
    """
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            _limits_fixture(),
            extra_usage={
                "is_enabled": True,
                "monthly_limit": 5000,
                "used_credits": 1234,
                "currency": "USD",
                "decimal_places": False,
            },
        ),
    )

    credits = u.collect_claude(str(config_dir), now=_FETCHED_AT).credits
    assert credits is not None
    payload = credits.model_dump(mode="json")
    assert "decimal_places" in payload
    assert payload["decimal_places"] is None
    assert payload["used_amount"] is None
    assert payload["limit_amount"] is None
    # Wire JSON matches the same three-valued contract.
    wire = json.loads(credits.model_dump_json())
    assert wire["decimal_places"] is None


@pytest.mark.parametrize(
    "body",
    [
        "{not json",  # corrupt
        json.dumps({"projects": {}}),  # no cachedUsageUtilization
        json.dumps({"cachedUsageUtilization": None}),  # null payload
        json.dumps({"cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS}}),  # no utilization
        json.dumps([1, 2, 3]),  # not an object
    ],
)
def test_claude_degrades_to_unavailable_never_raises(tmp_path: Path, body: str) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(body, encoding="utf-8")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is False
    assert snapshot.reason  # always explains itself
    assert snapshot.windows == []


def test_claude_missing_dir_is_unavailable(tmp_path: Path) -> None:
    snapshot = u.collect_claude(str(tmp_path / "nope"), now=_FETCHED_AT)
    assert snapshot.available is False
    assert snapshot.reason is not None
    # Missing files must say missing — not the empty-object phrasing.
    assert "missing usage cache" in snapshot.reason


def test_claude_missing_source_reason_differs_from_empty_object(tmp_path: Path) -> None:
    """Missing file vs present empty JSON must not share one reason KIND.

    Reviewer spot-check: both previously collapsed to
    ``no cached usage in …/.claude.json or ….json``.

    Counterfeit that still fails: keep one shared reason template and only
    change path text — reasons can differ solely by path while the KIND is
    identical (``"no cached usage"``). We bind on the kind prefix, not bare
    inequality of full reason strings.
    """
    missing_dir = tmp_path / "missing_cfg"
    # Do not create any json under missing_dir.
    missing = u.collect_claude(str(missing_dir), now=_FETCHED_AT)

    empty_dir = tmp_path / "empty_cfg"
    empty_dir.mkdir()
    (empty_dir / ".claude.json").write_text("{}", encoding="utf-8")
    empty = u.collect_claude(str(empty_dir), now=_FETCHED_AT)

    assert missing.available is False and empty.available is False
    assert missing.reason is not None and empty.reason is not None
    # Kind prefixes must diverge — path-only differences do not count.
    assert missing.reason.startswith("missing usage cache:")
    assert empty.reason.startswith("empty usage cache")
    assert not missing.reason.startswith("empty usage cache")
    assert not empty.reason.startswith("missing usage cache:")
    # Shared legacy template is the exact regression.
    assert "no cached usage in" not in missing.reason
    assert "no cached usage in" not in empty.reason


def test_claude_corrupt_source_reason_differs_from_missing(tmp_path: Path) -> None:
    """Corrupt JSON is unreadable, not missing and not empty-of-usage.

    Counterfeit: map corrupt back to the same generic ``no cached usage``
    template as missing (path text may still differ). Bind on kind prefixes.
    """
    missing_dir = tmp_path / "miss"
    missing = u.collect_claude(str(missing_dir), now=_FETCHED_AT)

    corrupt_dir = tmp_path / "corrupt_cfg"
    corrupt_dir.mkdir()
    (corrupt_dir / ".claude.json").write_text("{not json", encoding="utf-8")
    corrupt = u.collect_claude(str(corrupt_dir), now=_FETCHED_AT)

    assert missing.reason is not None and corrupt.reason is not None
    assert missing.reason.startswith("missing usage cache:")
    assert corrupt.reason.startswith("unreadable usage cache:")
    assert not corrupt.reason.startswith("missing usage cache:")
    assert "no cached usage in" not in missing.reason
    assert "no cached usage in" not in corrupt.reason


def test_read_json_non_object_root_is_corrupt_not_missing(tmp_path: Path) -> None:
    """Valid JSON that is not an object is corrupt — never the missing-file kind.

    Reviewer gate: ``if not isinstance(data, dict): return None, "missing"`` left
    the full accounts suite green because no test bound the non-object branch
    separately from absent files / invalid syntax.

    Counterfeit: collapse non-object roots into ``"missing"`` (or bare None that
    later renders as missing). This binds the classification token itself.
    """
    path = tmp_path / "root.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    data, err = u._read_json(path)

    assert data is None
    assert err == "corrupt"
    assert err != "missing"

    # Public surface: reason kind must not be the missing-file template.
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)
    assert snapshot.available is False
    assert snapshot.reason is not None
    assert snapshot.reason.startswith("unreadable usage cache:")
    assert not snapshot.reason.startswith("missing usage cache:")


def test_read_json_oserror_is_unreadable_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError on open is unreadable — never the missing-file kind.

    Reviewer gate: ``except OSError: return None, "missing"`` left the full
    accounts suite green because existing tests only cover absent files, empty
    objects, and invalid JSON syntax — never a present file that fails to read.

    Counterfeit: fold OSError into ``"missing"`` (or into a single generic None).
    This binds the classification token and the public reason kind.

    Patch ``usage.open`` (module global lookup) rather than ``builtins.open`` with
    path-string equality — macOS ``/var`` vs ``/private/var`` made the latter
    miss and silently exercise the happy path.
    """
    path = tmp_path / "present.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    assert path.is_file()
    target = path.resolve()

    real_open = open

    def _open_boom(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(file).resolve() == target:
            raise OSError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    # Bind on the module so ``open(...)`` inside usage.py hits this first (LEGB).
    monkeypatch.setattr(u, "open", _open_boom, raising=False)

    data, err = u._read_json(path)
    assert data is None
    assert err == "unreadable"
    assert err != "missing"

    # Public surface through collect_claude (same open() path).
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    claude_path = config_dir / ".claude.json"
    claude_path.write_text('{"ok": true}', encoding="utf-8")
    claude_target = claude_path.resolve()

    def _open_claude_boom(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(file).resolve() == claude_target:
            raise OSError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(u, "open", _open_claude_boom, raising=False)
    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)
    assert snapshot.available is False
    assert snapshot.reason is not None
    assert snapshot.reason.startswith("unreadable usage cache:")
    assert not snapshot.reason.startswith("missing usage cache:")


def test_claude_unusable_limit_entries_are_skipped(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    _claude_file(
        config_dir / ".claude.json",
        _utilization(
            [
                {"kind": "mystery_future_window", "percent": 50},  # unknown kind
                {"kind": "session", "percent": None},  # unusable percent
                {"kind": "weekly_all", "percent": 17, "severity": "normal"},
            ]
        ),
    )

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert [w.kind for w in snapshot.windows] == ["weekly_all"]
    assert snapshot.available is True


# ----------------------------------------------------------------------------- codex


def _rollout(path: Path, entries: list[tuple[str, dict[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "timestamp": ts,
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": rl},
            }
        )
        for ts, rl in entries
    ]
    # Interleave unrelated events — the collector must skip them cheaply.
    lines.insert(0, json.dumps({"timestamp": "2026-07-23T20:00:00.000Z", "type": "message"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rate_limits(primary_pct: float, secondary_pct: float | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "limit_id": "codex",
        "plan_type": "pro",
        "primary": {
            "used_percent": primary_pct,
            "window_minutes": 10080,
            "resets_at": 1785258237,
        },
        "secondary": None,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }
    if secondary_pct is not None:
        body["secondary"] = {
            "used_percent": secondary_pct,
            "window_minutes": 300,
            "resets_at": 1785258237,
        }
    return body


def test_codex_parses_weekly_and_session_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    _rollout(
        tmp_path / "sessions/2026/07/23/rollout-a.jsonl",
        [("2026-07-23T20:55:01.857Z", _rate_limits(15.0, secondary_pct=42.0))],
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert snapshot.available is True
    assert snapshot.plan == "pro"
    by_kind = {w.kind: w for w in snapshot.windows}
    # window_minutes is what distinguishes them — 10080 weekly, 300 session.
    assert by_kind["weekly_all"].percent == 15.0
    assert by_kind["weekly_all"].window_minutes == 10080
    assert by_kind["session"].percent == 42.0
    assert by_kind["session"].label == "Session (5h)"
    # Epoch seconds become UTC ISO like every other resets_at in the contract.
    assert by_kind["weekly_all"].resets_at == datetime.fromtimestamp(1785258237, UTC).isoformat()
    assert snapshot.stale is False


def test_codex_last_event_in_file_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    _rollout(
        tmp_path / "sessions/2026/07/23/rollout-a.jsonl",
        [
            ("2026-07-23T19:00:00.000Z", _rate_limits(9.0)),
            ("2026-07-23T20:55:01.857Z", _rate_limits(15.0)),
        ],
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert [w.percent for w in snapshot.windows] == [15.0]
    assert snapshot.fetched_at == "2026-07-23T20:55:01.857Z"


def test_codex_prefers_newest_rollout_and_skips_ones_without_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    old = tmp_path / "sessions/2026/07/22/rollout-old.jsonl"
    _rollout(old, [("2026-07-22T10:00:00.000Z", _rate_limits(3.0))])
    # Newest file has no rate_limits at all — the collector must fall through to
    # the next-newest rather than reporting "unavailable".
    barren = tmp_path / "sessions/2026/07/23/rollout-barren.jsonl"
    barren.parent.mkdir(parents=True, exist_ok=True)
    barren.write_text(json.dumps({"timestamp": "x", "type": "message"}) + "\n", encoding="utf-8")

    import os

    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(barren, (1_800_000_000, 1_800_000_000))

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert snapshot.available is True
    assert [w.percent for w in snapshot.windows] == [3.0]
    assert snapshot.source == str(old)


def test_codex_stale_snapshot_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    _rollout(
        tmp_path / "sessions/2026/07/23/rollout-a.jsonl",
        [("2026-07-23T20:55:01.857Z", _rate_limits(15.0))],
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 24, 6, 0, tzinfo=UTC))

    assert snapshot.stale is True
    assert snapshot.age_seconds is not None and snapshot.age_seconds > 3600


def test_codex_missing_event_timestamp_is_never_presented_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate_limits payload without a parseable event time is not 'fresh'.

    Counterfeit: set stale=True unconditionally on every codex snapshot — then
    test_codex_parses_weekly_and_session_windows would still pass its available
    checks but would break the fresh path here we pin: when timestamp IS present
    and recent, stale must remain False (see test_codex_parses_weekly_and_session_windows).
    Counterfeit for this test alone: flag stale only when windows are empty.
    """
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # No "timestamp" field on the event — age cannot be measured.
    path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": _rate_limits(5.0)},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert snapshot.available is True
    assert snapshot.fetched_at is None
    assert snapshot.age_seconds is None
    assert snapshot.stale is True


def test_codex_unparseable_event_timestamp_is_never_presented_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonempty garbage timestamp fails fromisoformat — never claim live.

    Counterfeit: `stale = not bool(timestamp)` treats any nonempty string as
    fresh. Missing-only fixtures do not exercise datetime.fromisoformat failure.
    """
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "not-a-valid-iso-timestamp",
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": _rate_limits(5.0)},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert snapshot.available is True
    assert snapshot.age_seconds is None
    assert snapshot.stale is True


def test_codex_boolean_used_percent_is_never_presented_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON ``used_percent: false`` must not become available 0% normal.

    Same governing defect as claude percent: float(False)==0.0 looked healthy.
    Counterfeit: only fix claude path; leave _codex_window on bare float().
    """
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-23T20:55:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {
                            "used_percent": False,
                            "window_minutes": 10080,
                            "resets_at": 1785258237,
                        },
                        "secondary": {
                            "used_percent": True,
                            "window_minutes": 300,
                            "resets_at": 1784839500,
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))

    assert snapshot.available is False
    assert snapshot.windows == []
    assert snapshot.reason is not None


def test_codex_boolean_window_minutes_is_never_a_valid_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boolean window_minutes must not manufacture a session/weekly bar.

    Reviewer gate: reverting ``minutes = _as_int(...)`` to
    ``int(x) if isinstance(x, int) else None`` accepts False as 0 and yields a
    session window. This test fails on that call-site revert.
    Counterfeit: only reject when used_percent is also bool — percent here is real.
    """
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-23T20:55:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {
                            "used_percent": 5.0,
                            "window_minutes": False,  # not 0 minutes
                            "resets_at": 1785258237,
                        },
                        "secondary": None,
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))
    direct = u._codex_window(
        {"used_percent": 5.0, "window_minutes": False, "resets_at": 1785258237}
    )

    assert direct is None
    assert snapshot.available is False
    assert snapshot.windows == []
    assert not any(w.kind == "weekly_all" for w in snapshot.windows)
    assert not any(w.window_minutes == 0 for w in snapshot.windows)


def test_codex_missing_window_minutes_is_never_a_weekly_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing window_minutes is unknown duration — not a valid Weekly bar.

    Reviewer probe: None fell through the else branch into kind=weekly_all with
    severity=normal. Counterfeit: only reject bool minutes; leave missing as weekly.
    """
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-23T20:55:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {
                            "used_percent": 5.0,
                            # window_minutes omitted
                            "resets_at": 1785258237,
                        },
                        "secondary": None,
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = u.collect_codex(now=datetime(2026, 7, 23, 21, 0, tzinfo=UTC))
    direct = u._codex_window({"used_percent": 5.0, "resets_at": 1785258237})

    assert direct is None
    assert snapshot.available is False
    assert snapshot.windows == []
    assert snapshot.reason is not None


def test_codex_no_sessions_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)

    snapshot = u.collect_codex()

    assert snapshot.available is False
    assert snapshot.reason is not None and "no session rollouts" in snapshot.reason


def test_codex_corrupt_lines_do_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)
    path = tmp_path / "sessions/2026/07/23/rollout-a.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"rate_limits": broken\n{"rate_limits": "not-an-object"}\n', encoding="utf-8")

    snapshot = u.collect_codex()

    assert snapshot.available is False
    assert snapshot.reason is not None and "no rate_limits event" in snapshot.reason


# ------------------------------------------------------------------------ aggregate


def test_severity_thresholds_match_the_cli_stamped_ones() -> None:
    assert u.severity_for(0) == "normal"
    assert u.severity_for(79.9) == "normal"
    assert u.severity_for(80) == "warning"
    assert u.severity_for(94.9) == "warning"
    assert u.severity_for(95) == "critical"
    assert u.severity_for(100) == "critical"


def test_collect_all_reports_telemetry_less_providers_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(u, "detect_config_dirs", lambda: [])
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path)

    by_provider = {s.provider: s for s in u.collect_all()}

    for provider in ("grok", "gemini", "kimi", "qwen"):
        snapshot = by_provider[provider]
        # A gap must read as a known gap, never as a confident zero.
        assert snapshot.available is False
        assert snapshot.windows == []
        assert snapshot.reason


def test_collect_all_covers_every_detected_claude_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "cfgA"
    _claude_file(good / ".claude.json", _utilization(_limits_fixture()), "a@example.com")
    empty = tmp_path / "cfgB"
    empty.mkdir()
    monkeypatch.setattr(
        u, "detect_config_dirs", lambda: [(str(good), "a@example.com"), (str(empty), None)]
    )
    monkeypatch.setattr(u, "_codex_home", lambda: tmp_path / "nocodex")

    claude = [s for s in u.collect_all(now=_FETCHED_AT) if s.provider == "claude"]

    assert len(claude) == 2
    assert [s.available for s in claude] == [True, False]


def test_account_credits_default_scale_is_unknown_not_zero() -> None:
    """Model default for decimal_places must be unknown (None), never known 0.

    Reviewer gate: mutating ``decimal_places: int | None = 0`` left the full
    lane green because Claude's producer always passes ``decimal_places=...``
    explicitly, so the model field default used by ``_codex_credits()`` and
    bare constructors was never bound.

    Counterfeit: keep the field optional but default to 0 (unknown presented
    as measured scale zero — invents major units via 10**0 for any caller that
    trusts the default).
    """
    # Direct constructor — binds the Pydantic field default only.
    bare = u.AccountCredits()
    assert bare.decimal_places is None
    assert bare.model_dump(mode="json")["decimal_places"] is None

    # Codex producer never sets decimal_places; must still serialize as null.
    codex = u._codex_credits({"has_credits": True, "balance": "12.5", "unlimited": False})
    assert codex is not None
    assert codex.decimal_places is None
    wire = json.loads(codex.model_dump_json())
    assert wire["decimal_places"] is None
    assert wire["balance"] == "12.5"


@pytest.mark.parametrize(
    "body",
    [
        # Present cachedUsageUtilization, utilization key absent.
        {"cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS}},
        # Present cachedUsageUtilization, utilization null.
        {"cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS, "utilization": None}},
        # Present cachedUsageUtilization, utilization not an object.
        {"cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS, "utilization": "nope"}},
        {"cachedUsageUtilization": {"fetchedAtMs": _FETCHED_MS, "utilization": []}},
    ],
)
def test_claude_present_cache_without_utilization_is_empty_not_missing(
    tmp_path: Path, body: dict[str, object]
) -> None:
    """Present file + cachedUsageUtilization but no parseable utilization.

    Reviewer gate: ``path_status.append((path, "missing"))`` on the
    non-dict utilization branch left the full lane green — existing tests
    bound empty-object ``{}`` (no cachedUsageUtilization) and absent files,
    not this present/unparseable-versus-absent branch.

    Counterfeit: relabel this branch as ``"missing"`` so a real on-disk cache
    that failed to carry utilization is reported as if the file did not exist.
    """
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(json.dumps(body), encoding="utf-8")

    snapshot = u.collect_claude(str(config_dir), now=_FETCHED_AT)

    assert snapshot.available is False
    assert snapshot.reason is not None
    assert snapshot.reason.startswith("empty usage cache"), snapshot.reason
    assert not snapshot.reason.startswith("missing usage cache:")
    assert "no cached usage in" not in snapshot.reason

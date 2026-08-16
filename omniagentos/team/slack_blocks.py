"""Block Kit rendering for the team Slack surfaces.

One vocabulary, four message types (tracker, pulse, morning DM, overnight).
Every string that reaches a block passes the same ``_safe_title`` egress
guardrail as the plain-text paths; every assembler returns
``(color, blocks, fallback_text)`` where ``fallback_text`` is the pre-existing
plain rendering — Slack shows it in notifications and anywhere blocks cannot
render, so the styled path can never say MORE than the audited plain path.

Slack hard limits respected here: ≤50 blocks per message, ≤10 fields per
section, ≤3000 chars per text object. Color is only expressible as an
attachment side-bar; the whole block list rides inside one attachment.

Emoji vocabulary (keep consistent — a glyph that means two things means
nothing):
  🟢 healthy · 🟡 attention · 🔴 blocked/red · ⚪ no data
  ▶️ active session · ▫️ recent-but-idle
  📋 pool · ⚙️ merge queue · ✅ merged · ❌ failed merge · 🤖 inference
  🌙 overnight suggestion · 🔋 capacity available
"""

from __future__ import annotations

from typing import Any

from omniagentos.team.notify import _safe_title

GREEN = "#2eb67d"
AMBER = "#ecb22e"
RED = "#e01e5a"
GREY = "#868686"

_MAX_TEXT = 2900  # headroom under Slack's 3000/char text-object limit
_MAX_BLOCKS = 48  # headroom under Slack's 50-block limit


def _t(value: object) -> str:
    return _safe_title(str(value or ""))


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": _t(text)[:150]}}


def _section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _t(mrkdwn)[:_MAX_TEXT]}}


def _fields(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": _t(f"*{label}*\n{value}")[:_MAX_TEXT]}
            for label, value in pairs[:10]
        ],
    }


def _context(text: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": _t(text)[:_MAX_TEXT]}],
    }


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _clip(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) <= _MAX_BLOCKS:
        return blocks
    kept = blocks[: _MAX_BLOCKS - 1]
    kept.append(_context(f"…{len(blocks) - len(kept)} more blocks trimmed"))
    return kept


def tracker_color(bottleneck: str, failed_last_hour: int) -> str:
    if bottleneck.startswith("merge gate red") or failed_last_hour > 0:
        return RED
    if bottleneck != "none":
        return AMBER
    return GREEN


def _health_glyph(color: str) -> str:
    return {GREEN: "🟢", AMBER: "🟡", RED: "🔴"}.get(color, "⚪")


def tracker_blocks(
    overall: Any,
    roster: list[tuple[str, str]],
    reports: dict[str, dict[str, Any]],
    *,
    stamp: str,
    fresh_seconds: float,
) -> tuple[str, list[dict[str, Any]]]:
    """The hourly session-tracker message, styled.

    ``overall`` is the tracker's ``Overall`` dataclass; kept duck-typed so this
    module never imports the tracker (which imports notify, which this file's
    guardrail import already touches — no cycles).
    """
    color = tracker_color(str(overall.bottleneck), int(overall.failed_merges_last_hour))
    blocks: list[dict[str, Any]] = [
        _header("📊 Session Tracker"),
        _context(stamp),
        _fields(
            [
                ("Proposals", str(overall.proposals)),
                ("Candidates", str(overall.candidates)),
                ("⚙️ Merge queue (48h)", str(overall.merge_queue)),
                (
                    "Merges (1h)",
                    f"✅ {overall.merged_last_hour} · ❌ {overall.failed_merges_last_hour}",
                ),
                ("Active sessions", str(overall.active_sessions)),
                ("Bottleneck", f"{_health_glyph(color)} {overall.bottleneck}"),
            ]
        ),
    ]
    for note in getattr(overall, "notes", []):
        blocks.append(_context(f"note: {note}"))
    blocks.append(_divider())
    for employee_id, name in roster:
        report = reports.get(employee_id)
        if report is None:
            blocks.append(_section(f"*{name}* ⚪ — _no session report received_"))
            continue
        if report.get("_unreadable"):
            # A stub for a corrupt drop-file: rendering its empty sessions as
            # "0 active / 0 recent" would present fabricated zeros as measured
            # truth in Slack, where the message is frozen (review TRK-06-R5).
            blocks.append(_section(f"*{name}* ⚪ — _drop-file unreadable — state unknown_"))
            continue
        if report.get("opted_out"):
            # The owner used the kill switch: render the stop as a CHOICE.
            # "0 active / 0 recent" here would be exactly the looks-offline
            # outcome the opt-out marker exists to prevent.
            since = str(report.get("opted_out_since") or "unknown time")
            blocks.append(
                _section(
                    f"*{name}* ⚪ — _telemetry off (opted out since {since}) — no data collected_"
                )
            )
            continue
        sessions = [s for s in report.get("sessions", []) if isinstance(s, dict)]
        active_n = int(report.get("active_count") or sum(1 for s in sessions if s.get("active")))
        recent_n = int(report.get("recent_count") or len(sessions))
        stale = report.get("_age_seconds", 0) > fresh_seconds
        glyph = "⚪" if stale else ("🟢" if active_n else "🟡")
        suffix = " · _stale report_" if stale else ""
        lines = [
            f"*{name}* {glyph} — *{active_n} active* / {recent_n} recent"
            f" · {report.get('host', '?')}{suffix}"
        ]
        for session in sessions[:8]:
            marker = "▶️" if session.get("active") else "▫️"
            description = str(session.get("description") or "")
            project = str(session.get("project") or "")
            account = str(session.get("account") or "")
            bits = [f"{marker} {description[:110]}"]
            if project and project not in description:
                bits.append(f"_{project.rsplit('/', 1)[-1]}_")
            if account and account != "default":
                bits.append(f"`@{account}`")
            lines.append("  ".join(bits))
        blocks.append(_section("\n".join(lines)))
    return color, _clip(blocks)


def pulse_blocks(
    person_lines: list[tuple[str, bool]],
    pool_depth: int | None,
    pool_low: bool,
    *,
    stamp: str,
    overnight: list[str] | None = None,
    companies_line: str | None = None,
    footer: str | None = None,
    test: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """The hourly pulse. ``person_lines`` = (rendered line, alarming).

    ``companies_line`` (v3 alerts) is the one per-company queue-depth line;
    ``footer`` is the claim/assign CTA context; ``test`` marks a demo post.
    """
    any_alarm = any(alarming for _, alarming in person_lines)
    color = AMBER if (pool_low or any_alarm) else GREEN
    title = "🌙 Team Pulse — overnight edition" if overnight else "📈 Team Pulse"
    if test:
        title = f"🧪 TEST — {title}"
    blocks = [_header(title), _context(stamp)]
    body, used = [], 0
    for index, (line, alarming) in enumerate(person_lines):
        # Trim per PHYSICAL line: a person entry may carry a second
        # task-deadline line ("📌 T1 🔴⏰…") that a whole-string slice at 150
        # would cut off — exactly the line the v4 spec wants front-and-center.
        parts = [part[:150] for part in line.lstrip("• ").split("\n")]
        rendered = f"{'🔴' if alarming else '🟢'} " + "\n".join(parts)
        # Size-aware: the trim marker must ALWAYS fit, so stop while there is
        # still headroom for it (2900-char text-object guard minus marker room).
        if used + len(rendered) > 2700:
            body.append(f"… {len(person_lines) - index} more people trimmed")
            break
        body.append(rendered)
        used += len(rendered) + 1
    if companies_line:
        body.append(companies_line[:200])
    pool_glyph = "🟡" if pool_low else "🟢"
    low_note = " ⚠ low" if pool_low else ""
    depth_text = "unavailable" if pool_depth is None else f"{pool_depth} claimable"
    body.append(f"{pool_glyph} 📋 *Pool* — {depth_text}{low_note}")
    blocks.append(_section("\n".join(body)))
    if overnight:
        lines_o, used_o = [], 0
        for index, raw in enumerate(overnight):
            rendered = raw[:150]
            if used_o + len(rendered) > 2600:
                lines_o.append(f"… {len(overnight) - index} more suggestions trimmed")
                break
            lines_o.append(rendered)
            used_o += len(rendered) + 1
        blocks.append(_divider())
        blocks.append(_section("*🌙 Overnight suggestions*\n" + "\n".join(lines_o)))
    if footer:
        blocks.append(_context(footer))
    return color, _clip(blocks)


def daybrief_blocks(
    header_text: str,
    company_sections: list[tuple[str, list[str]]],
    person_sections: list[tuple[str, list[str]]],
    *,
    footer: str,
    stamp: str,
    alarming: bool = False,
    targets_line: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The channel-wide morning brief (v3 alerts, 2026-08-13).

    Layout, top to bottom: header (already 🧪-prefixed by the caller for a
    test post), the standing targets line (2026-08-14, when given — SAME
    string ``notify._targets_line`` renders in text), stamp context, one
    section per company (fixed five-company order, '— empty' companies stay
    visible as one-liners), a divider, one section per person, and the
    claim/assign footer as context. Card-line caps live in ``notify`` (the
    gatherer), so text and blocks agree; this assembler only enforces
    Slack's own limits (section chars, 50 blocks).

    ``alarming`` (an overdue deadline somewhere on the board) turns the
    side-bar AMBER — the brief is a queue statement, not an incident page,
    so it never goes RED.
    """
    color = AMBER if alarming else GREEN
    blocks = [_header(header_text)]
    if targets_line:
        blocks.append(_section(f"*{targets_line}*"))
    blocks.append(_context(stamp))
    for title, lines in company_sections:
        blocks.append(_section("\n".join([title, *lines])))
    blocks.append(_divider())
    for title, lines in person_sections:
        blocks.append(_section("\n".join([title, *lines])))
    blocks.append(_context(footer))
    return color, _clip(blocks)


def commitments_block(lines: list[str]) -> dict[str, Any]:
    """The '*Today's commitments*' section for the styled morning DM (WP-B).

    ``lines`` are the SAME bullet strings the plain-text DM renders (from
    ``notify._commitments_lines``) — one gather, two skins, so the styled and
    plain renderings can never disagree about what a person committed to.
    Ordinary ``_section``/``_MAX_TEXT`` clipping applies, same as every other
    block here; the 10-line cap matches the other morning-DM line caps.
    """
    body = "\n".join(f"• {line}" for line in lines[:10])
    return _section("*\U0001f4c5 Today's commitments*\n" + body)


def morning_dm_blocks(
    name: str,
    active_lines: list[str],
    assigned_lines: list[str],
    capacity_line: str,
    pool_lines: list[str],
    *,
    stamp: str,
    commitments_lines: list[str] | None = None,
    targets_line: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    color = GREEN if active_lines else AMBER
    blocks = [_header(f"☀️ Good morning, {name}")]
    if targets_line:
        blocks.append(_section(f"*{targets_line}*"))
    blocks.append(_context(stamp))
    if active_lines:
        blocks.append(_section("*▶️ In flight*\n" + "\n".join(active_lines[:10])))
    if assigned_lines:
        blocks.append(
            _section("*📥 Assigned to you (not started)*\n" + "\n".join(assigned_lines[:10]))
        )
    blocks.append(_context(f"🔋 {capacity_line}"))
    if pool_lines:
        blocks.append(_divider())
        blocks.append(
            _section("*📋 Grab from the pool* — reply `claim <REF>`\n" + "\n".join(pool_lines[:5]))
        )
    if commitments_lines:
        blocks.append(_divider())
        blocks.append(commitments_block(commitments_lines))
    return color, _clip(blocks)


def append_proposals(blocks: list[dict[str, Any]], proposals_text: str) -> list[dict[str, Any]]:
    """Repair proposals ride the tracker message as their own final section."""
    extended = list(blocks)
    extended.append(_divider())
    extended.append(_section(proposals_text))
    return _clip(extended)


def append_capacity(
    blocks: list[dict[str, Any]],
    aggregate: str,
    machines: list[str],
) -> list[dict[str, Any]]:
    """Compute-pool capacity as ONE styled group (divider + a single section):
    the aggregate as the bolded header, one line per machine beneath it.

    Deliberately one block, not one-per-machine — 7+ machines are coming and a
    block-per-row would blow the 50-block ceiling. The section text rides the
    same ``_MAX_TEXT`` clip as every other body, so an oversized pool is trimmed,
    never dropped. The strings are the SAME ones the plaintext COMPUTE lines
    render, so the two surfaces cannot disagree on a number.
    """
    extended = list(blocks)
    extended.append(_divider())
    body = "\n".join([f"*{aggregate}*", *machines])
    extended.append(_section(body))
    return _clip(extended)


def append_balances(blocks: list[dict[str, Any]], balances_text: str) -> list[dict[str, Any]]:
    """AI balances ride the tracker message as one section (twin of
    ``append_proposals`` — a single block, never one per account, so a large
    fleet cannot blow the 50-block budget).

    Unlike ``append_proposals``, this reserves room BEFORE appending: on a
    report already at the block cap, roster tail blocks are trimmed so the
    balances section itself is never the thing Slack drops — plaintext and
    Slack must agree that balances are present (review UI-01)."""
    extended = list(blocks)
    reserve = 3  # divider + section + potential trim marker
    if len(extended) > _MAX_BLOCKS - reserve:
        kept = extended[: _MAX_BLOCKS - reserve]
        kept.append(_context(f"…{len(extended) - len(kept)} more blocks trimmed"))
        extended = kept
    extended.append(_divider())
    extended.append(_section(balances_text))
    return _clip(extended)

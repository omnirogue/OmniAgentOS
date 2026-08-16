# Dashboard Conventions and Style Guidelines

This document outlines the standard front-end development conventions for the OmniAgentOS mission-control dashboard.

---

### Rule 1: Status-Color-per-Meaning (Semantic Color Mapping)
Semantic colors must be assigned strictly by outcome or state meaning:
- **Success / Healthy:** Green (`var(--ok)`, `tone="ok"`)
- **Warning / Degraded / Pending Action:** Yellow/Orange (`var(--warn)`, `tone="warn"`)
- **Error / Failure / Blocked:** Red (`var(--danger)`, `tone="danger"`)
- **Accent / In Progress:** Blue (`var(--accent)`, `tone="accent"`)
- **Promote / Optimization:** Purple (`var(--promote)`, `tone="promote"`)
- **Neutral:** Grey (`var(--text-muted)` on `var(--surface-2)`, `tone="neutral"`)

### Rule 2: UNKNOWN is Never Success-Styled
When no data is available (e.g. an aggregate score or status that cannot be fetched), it must **never** be success-styled.
- No data = no score.
- Render empty/unknown aggregates with a neutral or faint placeholder (e.g. `—`), never a confident green or a zero. This avoids the "unknown-as-favourable" anti-pattern.

### Rule 3: Monospace Machine Values via Central Formatters
All low-level machine identifiers (e.g. UUIDs, SHAs, timestamps, CPU metrics, memory sizes) must be rendered in monospace styling using central formatters defined in `dashboard/src/lib/format.ts`. Do not format values inline.

### Rule 4: Design Tokens Only
No hardcoded hex or RGB colors are allowed in component styles or inline properties. All features must use custom CSS properties (design tokens) mapped from the central theme (see the token constants in `dashboard/src/design/tokens.ts` and `theme.css`).
- *Cross-Reference:* See `docs/architecture/ui.md` (§ "Design system" / `dashboard/src/design/`) for structural details; do not restate token definitions here.

### Rule 5: Clickable-Row Deep-Link Contract
5. **Deep links resolve or explain.** A clickable row links by stable id (`?run=`, `?task=`, `?files=`). When the id is absent from the current data the page MUST render the closest-match error shape — `{ requested: <id>, closest: <id|null>, reason: "not_found" | "out_of_window" | "unauthorized" }` — and offer the closest match as a link when one exists. Never redirect silently to a different record and never render an empty page as if it were a result.

### Rule 6: No Dead UI
Every visual element on the dashboard must be functional. There should be no static texts or buttons masquerading as interactive controls:
- Interactive widgets must show proper hover states and follow keyboard accessibility.
- If a control is disabled or loading, its state must be visually explicit (e.g., using `disabled`, skeletons, or loading spinners).

### Rule 7: Armed-Delete Two-Step
Any destructive mutation (e.g., deleting a run, removing an integration, wiping a sandbox, rolling back a proposal) must utilize a two-step confirmation pattern. A single click must never trigger a deletion; it must require a confirmation step (such as changing the button to "Confirm Delete", or opening a confirmation modal).

### Rule 8: Four-State Connect Card
Every integration or connection card (e.g. LLM providers, database connections, external tools) must explicitly support and render four distinct operational states:
1. **Disconnected:** Safe, inactive, unconfigured state.
2. **Pending:** Active connection/handshake attempt.
3. **Connected:** Active, verified connection.
4. **Error:** Failed connection attempt with a readable diagnostic traceback or error code.

---

### Exemption: Dynamic-Value Runtime Styles
Runtime-computed styling that depends on arbitrary, unmapped, or live incoming streaming data (e.g. ANSI escape colorization inside Terminal terminal streams like `TerminalView.tsx` line 183 and 198) is explicitly exempt from the Design Tokens rule.

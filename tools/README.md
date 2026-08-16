# OmniAgentOS Tool Library

A curated set of **MCP (Model Context Protocol) servers** so any agent — a
Hermes agent, or an OmniAgentOS-driven `claude -p` / `codex exec` — can get
web search, vision, document conversion, browsing, files, and memory without
anyone hand-rolling a tool integration per agent.

**11 MCP servers → 70+ individual tools**, 9 of them fully keyless (nothing
to sign up for). All were verified to actually exist and actually run as
part of building this library (see [Verified](#verified) below — real
commands, real output, not asserted).

Everything here is new, additive, and outside `omniagentos/**` product code:

```
tools/
  README.md               <- you are here (the registry)
  mcp-servers.json        <- this library's roster (NOT the file the runtime loads -- see below)
  mcp-servers.local.json  <- same, keyless servers only
  mcp-servers.keyed.json  <- same, needs-a-key servers only
  install-tools.sh        <- installer + doctor report
  state/                  <- local server state (sqlite db etc.), gitignored
  examples/               <- sample files + their MarkItDown output
../.mcp.json              <- the file Claude Code actually loads. A SEPARATE tracked
                             regular file, NOT a symlink to the above.
```

### Which roster is live: read this before editing either one

`../.mcp.json` and `tools/mcp-servers.json` are **two independent tracked
regular files**. This README used to say `.mcp.json` was a symlink to the
mirror; `00000000` (2026-08-02) replaced the symlink with a regular file for
an unrelated reason, and they have disagreed ever since:

```
$ git ls-files -s .mcp.json tools/mcp-servers.json
100644 e24835a8fe49f52b838aacb4d07258ea2cf19b43 0    .mcp.json
100644 a9369d82695cfaf4d20d032dfd21b360fd848cd0 0    tools/mcp-servers.json
```

Mode `100644` on both — a symlink would be `120000`. **Editing
`tools/mcp-servers.json` alone changes nothing about what any agent loads.**

**Interim editing rule, until the owner picks a single roster: change both
files, and leave them identical.** `scripts/gates/mech_gate.sh
--check-mcp-roster` and `scripts/health-sentinel/audit_checks.py`
(`mcp_roster`) both refuse when the two disagree, precisely so this cannot
drift again unnoticed. Approval still lives in `configs/mcp-approved.yaml`,
so adding a server is a reviewed change to all three.

## Quick start

```bash
# 1. Install the keyless servers + get a doctor report
~/OmniAgentOS/tools/install-tools.sh

# 2a. Use it from Claude Code interactively — .mcp.json is already wired,
#     approve the project's servers once via the /mcp panel the first time.
cd ~/OmniAgentOS && claude

# 2b. Use it from claude -p headlessly (what OmniAgentOS's runner needs) —
#     explicit --mcp-config skips the approval gate, --allowedTools grants
#     the specific servers (see "Point claude -p at this library" for why
#     both flags are needed and where the prompt goes):
claude --print "search the web for X and summarize" \
  --mcp-config tools/mcp-servers.local.json --strict-mcp-config \
  --allowedTools "mcp__duckduckgo,mcp__fetch"

# 3. Use it from Hermes — paste the YAML block from "Point Hermes at this
#    library" below into ~/.hermes/config.yaml, once.

# 4. (optional) add a search API key
export TAVILY_API_KEY=tvly-...     # https://tavily.com  (free tier)
# or
export BRAVE_API_KEY=BSA...        # https://brave.com/search/api (free tier, 2k/mo)
```

No key is required for document conversion, web fetch, web search
(DuckDuckGo), browsing, files, git, sqlite, memory, or reasoning.

---

## Governance — who gets the whole library

OmniAgentOS's runner already enforces a real action-class policy
(`configs/policy.yaml`, loaded by `omniagentos/policy`). This library does
**not** change that policy — it plugs into the trust boundary that already
exists:

- **The action classes** (`read_only`, `sandboxed_creation`,
  `internal_reversible`, `external_reversible`, `consequential` — blueprint
  §12) currently gate `generate` / `file_read` / `file_write` / `shell`.
  There is **no `network` action class yet** — meaning the runner's harness
  sandbox flags (`--sandbox read-only` / `workspace-write` for
  `cli-codex`/`cli-grok`, `plain -p` vs `--permission-mode acceptEdits` for
  `cli-claude`) do not currently model "may call the internet." An MCP
  config that grants fetch/search/browser tools sits **outside** that
  existing enforcement, so it must not be handed to anything the policy
  hasn't vetted.
- **Operator agents** — Hermes, or a `claude -p` / `codex exec` **you**
  launch directly as the operator (not inside `omniagentos/lab`'s
  champion/challenger loop) — may use this whole library freely. That's the
  entire point of it: point `~/.hermes/config.yaml` at
  `tools/mcp-servers.json` and go. (Claude Code loads `.mcp.json` — the
  separate file, not this one; see the editing rule above.)
- **Lab candidates** — challenger agents being split-tested by
  `omniagentos/lab` (see `omniagentos/lab/contracts.py`: `Surface.CHALLENGER`,
  `EvalSplit.HELD_OUT`) — must **not** get this library dropped into their
  workspace as `.mcp.json`, and must not get an MCP config with `fetch`,
  `duckduckgo`, `tavily`, `brave-search`, or `playwright` in it. Those are
  unsupervised network egress, and `EvalCase.expected` for `split=HELD_OUT`
  is deliberately never serialized into anything a candidate subprocess can
  open (`CandidateEvalCase` strips it structurally). Giving a candidate a
  live web-search/fetch tool would reopen exactly the exfiltration path
  that structural defense exists to close — a challenger could, in
  principle, phone held-out answers or task internals out over a fetch
  call, or pull in outside context that invalidates a blind, apples-to-apples
  comparison against the champion. If a lab experiment genuinely needs a
  challenger to have `file_read`/`file_write`/`shell`, that already goes
  through the existing `tools_allowed` allowlist + sandbox mapping in
  `configs/policy.yaml` — extend *that* mechanism (and route anything
  `external_reversible`/`consequential` through its existing approval gate),
  don't hand out this MCP config as a shortcut around it.
- Practically: only wire `tools/mcp-servers.json` (or `.mcp.json`, which is
  its own separate file) into a workspace when you (the operator) are driving the
  session. Lab/evaluation harnesses should keep using
  `configs/policy.yaml`'s `tools.known` allowlist and leave MCP servers out
  of a candidate's spawn args entirely. (Aside: Hermes itself also runs a
  `_filter_suspicious_mcp_servers` pass — `tools/mcp_tool.py` — that drops
  exfiltration-shaped MCP configs before any stdio spawn; that's a useful
  second layer, not a substitute for the boundary above.)

No code in `omniagentos/**` was changed to write this note — it's
documentation of an existing boundary, per the brief.

---

## The catalog

"How invoked" = the MCP **server name** → **tool name(s)** an agent actually
calls once the server is wired in. Every server below speaks stdio unless
noted.

### Web & search

| Tool | What it does | How invoked | Install | Key? |
|---|---|---|---|---|
| **DuckDuckGo** (`duckduckgo-mcp-server`) | Keyless web search + page-text extraction. Verified working live (no signup). | `duckduckgo` → `search`, `fetch_content` | `uvx duckduckgo-mcp-server` (pip: `duckduckgo-mcp-server`) | No |
| **Tavily** (`tavily-mcp`) | Purpose-built search API for LLM agents — search + page extraction, more reliable/faster than scraping under load. **Priority pick** for production search. | `tavily` → `tavily-search`, `tavily-extract` | `npx -y tavily-mcp@latest` (npm: `tavily-mcp`) | **Yes** — `TAVILY_API_KEY` (free tier at tavily.com) |
| **Brave Search** (`server-brave-search`) | Web + local search via Brave's API. Official MCP org server, now living in the *archived* `servers-archived` repo (still works, less actively maintained than Tavily). | `brave-search` → `brave_web_search`, `brave_local_search` | `npx -y @modelcontextprotocol/server-brave-search` | **Yes** — `BRAVE_API_KEY` (free tier: 2,000 queries/mo at brave.com/search/api) |

Honesty note: DuckDuckGo scraping-based search can get rate-limited or
break if DuckDuckGo changes markup — it worked in live testing (see
Verified) but treat it as the free/no-friction default, and reach for
Tavily (a real API with an SLA) if you need production reliability.

### Vision & media

There is **no MCP server in this library for vision** — and that's a
deliberate, verified choice, not an oversight:

| Path | What it does | How invoked | Key? |
|---|---|---|---|
| **Native model vision** (recommended) | `claude -p` and Hermes both have first-class multimodal vision built in — no MCP server needed. `claude -p`'s own `Read` tool reads image files (png/jpg/etc.) and hands them to the model visually. Hermes has a dedicated `vision` toolset with a `vision_analyze` tool (`toolsets.py`: `"vision": {"description": "Image analysis and vision tools", "tools": ["vision_analyze"]}`). | Just point the agent at an image path/URL and ask it to look. For Hermes, make sure the `vision` toolset is enabled (`hermes chat --toolsets vision,...` or add `vision` to `platform_toolsets` in `config.yaml`). | No |
| **MarkItDown** (assist, not OCR) | Pulls literal embedded text/metadata out of documents (PDF text layer, docx/pptx alt-text, EXIF). Not scanned-image OCR. | `markitdown` → `convert_to_markdown` | No |

I looked for a genuinely maintained, dedicated OCR/vision MCP server to
bundle here (searched npm for "mcp ocr" and similar) and didn't find one
worth recommending — the results were either unofficial single-author
packages with no track record, or unrelated libraries. Rather than bundle
something shaky, the honest answer is: **use the calling model's native
vision** (both consumers already have it, verified above), and revisit this
row if a well-maintained option shows up later.

### Documents (priority)

| Tool | What it does | How invoked | Install | Key? |
|---|---|---|---|---|
| **MarkItDown** (Microsoft) | Converts pdf/docx/pptx/xlsx/html/csv/json/xml/images/audio → Markdown. **The core "document converting" tool.** Verified end-to-end on a real .docx and a real .pdf (see Verified). | `markitdown` → `convert_to_markdown(uri)` (uri can be `file:`, `http(s):`, or `data:`) | MCP server: `uvx markitdown-mcp` (pip: `markitdown-mcp`). Plain CLI (no MCP, useful inside a governed shell-only sandbox): `uvx --from 'markitdown[all]' markitdown file.pdf` | No |
| **pandoc** (optional, not installed by default) | The universal document converter — and unlike MarkItDown it also converts the *other* direction (Markdown → docx/pdf/pptx/etc). Not an MCP server on its own; wrap with the community `mcp-pandoc` (PyPI) if you want it MCP-native. | `brew install pandoc`, or shell out to it directly from a `shell`-permitted agent | `brew install pandoc` (system binary, not uvx/npx — that's why it's not in the default keyless install) | No |

Important, verified detail: the **bare** `markitdown` CLI needs the
`[docx]`/`[all]` extra to read .docx/.pptx/.xlsx (`pip install
"markitdown[all]"` / `uvx --from 'markitdown[all]' markitdown ...`) — a
plain `uvx markitdown` install fails on .docx with
`MissingDependencyException`. The **`markitdown-mcp`** server does **not**
have this problem: its own dependency chain already pulls in full format
support, so `uvx markitdown-mcp` converts .docx/.pdf out of the box with no
extra flags. This is documented nowhere obviously, I found it by actually
running both and comparing (see Verified).

`mcp-pandoc` exists on PyPI (`pip install mcp-pandoc` / `uvx mcp-pandoc`,
requires the `pandoc` binary on PATH) but has thin, unverifiable provenance
(no listed project URLs) — mentioned per the brief's "note pandoc if
useful," not bundled as a default recommendation.

### Files & data

| Tool | What it does | How invoked | Install | Key? |
|---|---|---|---|---|
| **Filesystem** (official) | Read/write files, list/create/move directories, search files, file metadata — 13 tools. | `filesystem` → `read_text_file`, `read_media_file`, `read_multiple_files`, `write_file`, `edit_file`, `create_directory`, `list_directory`, `list_directory_with_sizes`, `move_file`, `search_files`, `directory_tree`, `get_file_info`, `list_allowed_directories` | `npx -y @modelcontextprotocol/server-filesystem <allowed-dir>` | No |
| **Git** (official) | Read/search/manipulate a git repo — status, diff, commit, add, reset, log, branch, checkout, show — 12 tools. | `git` → `git_status`, `git_diff_unstaged`, `git_diff_staged`, `git_diff`, `git_commit`, `git_add`, `git_reset`, `git_log`, `git_create_branch`, `git_checkout`, `git_show`, `git_branch` | `uvx mcp-server-git` (each call takes `repo_path`; pass `-r <repo>` to pin a default) | No |
| **SQLite** (origin: official MCP org, now in the *archived* `servers-archived` repo — still functional, less actively maintained) | Query/modify a local SQLite db, list tables/schema, and an "insights memo" resource for BI-style workflows — 6 tools. | `sqlite` → `read_query`, `write_query`, `create_table`, `list_tables`, `describe-table`, `append_insight` | `uvx mcp-server-sqlite --db-path <file>` | No |

Default `filesystem` root in `mcp-servers.json` is `/Users/youruser`
(broad, by design — this is the *operator's own* general-purpose config).
Narrow it to a specific project directory if you want less blast radius for
a given agent; **never** point it at `/` or hand this server's config to a
lab candidate (see Governance).

### Web fetch / browser

| Tool | What it does | How invoked | Install | Key? |
|---|---|---|---|---|
| **fetch** (official) | Fetches a URL, converts HTML → Markdown. Verified live against a real URL over the actual MCP protocol (see Verified). | `fetch` → `fetch(url, max_length?, start_index?, raw?)` | `uvx mcp-server-fetch` (pip: `mcp-server-fetch`) | No |
| **Playwright MCP** (Microsoft, official) | Real browser automation — navigate, click, type, screenshot, read the accessibility tree, manage tabs/network/console — 20+ tools. | `playwright` → `browser_navigate`, `browser_click`, `browser_type`, `browser_take_screenshot`, `browser_snapshot`, … | `npx -y @playwright/mcp@latest` | No |

Playwright MCP needs an actual browser binary the first time it navigates
anywhere. Either run `npx playwright install chromium` once (one-time
~150–300MB download, not done automatically by `install-tools.sh` to avoid
a surprise download), or add `"--browser", "msedge"` (or `"chrome"`) to its
`args` in `mcp-servers.json` to reuse a browser you already have installed
instead.

### Memory & code

| Tool | What it does | How invoked | Install | Key? |
|---|---|---|---|---|
| **Memory** (official) | Persistent knowledge graph (entities/relations/observations) so an agent can remember things across sessions — 9 tools. | `memory` → `create_entities`, `create_relations`, `add_observations`, `delete_entities`, `delete_observations`, `delete_relations`, `read_graph`, `search_nodes`, `open_nodes` | `npx -y @modelcontextprotocol/server-memory` | No |
| **Sequential Thinking** (official) | Structured step-by-step reasoning scratchpad tool — helps an agent plan/backtrack on hard problems. | `sequential-thinking` → `sequentialthinking` | `npx -y @modelcontextprotocol/server-sequential-thinking` | No |

`memory`'s storage file defaults to `memory.jsonl` inside the server's own
install location. Point it somewhere durable with `MEMORY_FILE_PATH` in its
`env` block if you want it to persist in a specific place (e.g.
`tools/state/memory.jsonl` — already covered by `tools/.gitignore`).

---

## How agents actually call these tools (MCP mechanics, briefly)

An agent doesn't "install a tool" per call. The **host** (Claude Code,
Hermes, Codex) reads its MCP config once at session start, **spawns each
configured server as a subprocess** (stdio, talking JSON-RPC), asks it
"what tools do you have," and merges the answer into the model's tool list
for that session. From then on, calling e.g. `mcp__fetch__fetch` (Claude
Code's naming) or `fetch` (Hermes) is just a normal tool call — the host
routes it to the right subprocess. That's why `uvx`/`npx` need to be able
to resolve the packages (this library's install step is really a *cache
warm*, not a persistent install) but nothing needs to be running in the
background between sessions.

## Point Hermes at this library

Hermes's `~/.hermes/config.yaml` has an `mcp_servers:` block (see the
commented example around line 951 in that file). Paste this in (uncomment
the `mcp_servers:` key) — it's the same server set as
`tools/mcp-servers.json`, just YAML, and Hermes resolves `${VAR}` /
Cursor-style `${env:VAR}` in `env:` values from the process environment
(verified in `hermes-agent/tools/mcp_tool.py`, `_interpolate_env_vars`):

```yaml
mcp_servers:
  fetch:
    command: uvx
    args: ["mcp-server-fetch"]
  duckduckgo:
    command: uvx
    args: ["duckduckgo-mcp-server"]
  markitdown:
    command: uvx
    args: ["markitdown-mcp"]
  playwright:
    command: npx
    args: ["-y", "@playwright/mcp@latest"]
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/youruser"]
  git:
    command: uvx
    args: ["mcp-server-git"]
  sqlite:
    command: uvx
    args: ["mcp-server-sqlite", "--db-path", "/Users/youruser/OmniAgentOS/tools/state/sqlite-mcp.db"]
  memory:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory"]
  sequential-thinking:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-sequential-thinking"]
  tavily:
    command: npx
    args: ["-y", "tavily-mcp@latest"]
    env:
      TAVILY_API_KEY: "${TAVILY_API_KEY}"
  brave-search:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: "${BRAVE_API_KEY}"
```

Vision doesn't go in this block — enable the `vision` toolset instead
(`hermes chat --toolsets vision,web,file,terminal` or add it to
`platform_toolsets` in `config.yaml`).

Sanity-check any single server with Hermes's own tester: `hermes mcp test
fetch` (defined in `hermes_cli/subcommands/mcp.py`; also `hermes mcp list`
to see everything currently configured).

## Point `claude -p` at this library

Two real paths here — tested both, they behave differently, use the right
one for the situation:

**Interactive (you, at a terminal):** `~/OmniAgentOS/.mcp.json` is a tracked
file in its own right (not a symlink to `tools/mcp-servers.json` — see the
editing rule above), and Claude Code auto-loads `.mcp.json`
from the project root. Run `claude` from inside `~/OmniAgentOS` — Claude
Code will flag the project's `.mcp.json` servers as **pending approval**
(this is real, verified behavior: `claude mcp list` showed exactly this)
the first time, since project-scoped `.mcp.json` servers are untrusted by
default until a human approves them (security feature — stops a cloned repo
from silently auto-running arbitrary commands). Approve once via the `/mcp`
panel inside the session, and they stay approved for that project.

**Headless / automated (this is what OmniAgentOS's runner needs — verified
end-to-end, real `claude -p` calls, real output):** don't rely on
`.mcp.json` at all — pass `--mcp-config` explicitly, which loads the
servers **without** the pending-approval gate, then explicitly allow the
servers you want with `--allowedTools`:

```bash
claude --print "<prompt>" \
  --mcp-config tools/mcp-servers.local.json --strict-mcp-config \
  --allowedTools "mcp__fetch,mcp__markitdown,mcp__duckduckgo"
```

Verified, non-obvious details (found by actually running this, not assumed):

- Put the prompt right after `--print`/`-p`. `--mcp-config` and
  `--allowedTools` both take a variadic list of values, and if the prompt
  comes *after* them the CLI parser can swallow it into the flag's value
  list instead — reproduced this exact failure
  (`Error: Input must be provided either through stdin or as a prompt
  argument when using --print`) before moving the prompt earlier.
- `--mcp-config <file>` makes the servers **visible** to the model
  (confirmed: a bare `--mcp-config ... --strict-mcp-config` run correctly
  listed all 9 keyless servers by name) but does **not** by itself let it
  **call** them — an actual `mcp__fetch__fetch` call came back "blocked...
  you haven't granted it yet" until `--allowedTools` was added.
- `--permission-mode acceptEdits` (the flag `configs/policy.yaml`'s
  `sandbox_mapping` already uses for `workspace_write` tasks) does **not**
  grant MCP tool calls by itself — tested, still blocked. MCP tools need
  their own entry in `--allowedTools`.
- You don't need to enumerate every individual tool: the bare server name
  (`mcp__fetch`, no tool suffix, no `*`) allow-lists every tool on that
  server — confirmed with a real `fetch` call.
- `${VAR}` in `.mcp.json`/`--mcp-config` `env` values is real and
  documented (Claude Code docs, `docs.claude.com/en/docs/claude-code/mcp`:
  plugin example `"DB_URL": "${DB_URL}"`) — `export TAVILY_API_KEY=...`
  before launching and the `tavily` server picks it up; leave it unset and
  only that one server fails to connect (`claude mcp list` surfaced this
  exactly: `Missing environment variables: TAVILY_API_KEY`).

If OmniAgentOS's `cli-claude` adapter is extended to use this library, the
`--mcp-config` + `--allowedTools` recipe above — not the implicit
`.mcp.json` pickup — is the form that works unattended.

## Point `codex exec` at this library

`codex` (this machine has `codex-cli`) has its own native MCP client —
`codex mcp add/list/get/remove` and a `[mcp_servers.<name>]` table in
`~/.codex/config.toml`. This wasn't wired automatically (that file is
global machine state outside this library's `tools/` + `.mcp.json` scope),
but here are the exact commands — run once per server you want:

```bash
codex mcp add fetch                 -- uvx mcp-server-fetch
codex mcp add duckduckgo            -- uvx duckduckgo-mcp-server
codex mcp add markitdown            -- uvx markitdown-mcp
codex mcp add git                   -- uvx mcp-server-git
codex mcp add sqlite                -- uvx mcp-server-sqlite --db-path /Users/youruser/OmniAgentOS/tools/state/sqlite-mcp.db
codex mcp add filesystem            -- npx -y @modelcontextprotocol/server-filesystem /Users/youruser
codex mcp add memory                -- npx -y @modelcontextprotocol/server-memory
codex mcp add sequential-thinking   -- npx -y @modelcontextprotocol/server-sequential-thinking
codex mcp add playwright            -- npx -y @playwright/mcp@latest
codex mcp add tavily --env TAVILY_API_KEY="$TAVILY_API_KEY" -- npx -y tavily-mcp@latest
codex mcp add brave-search --env BRAVE_API_KEY="$BRAVE_API_KEY" -- npx -y @modelcontextprotocol/server-brave-search
```

Or hand-edit `~/.codex/config.toml` directly — verified schema (this file
already has one entry, `node_repl`, in exactly this shape):

```toml
[mcp_servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]
```

Same Governance boundary applies: this is for the operator's own
`codex exec` runs, not for wiring into `omniagentos/lab` challenger specs.

---

## Verified

Everything below is real output from actually running these tools during
this build, not a claim. Reproduce any of it with the MCP Inspector CLI:
`npx @modelcontextprotocol/inspector --cli <command> --method tools/call --tool-name <tool> --tool-arg k=v`.

**1. MarkItDown converts a real file to Markdown** — via the actual
`markitdown-mcp` MCP server (not just the bare CLI), calling
`convert_to_markdown` over the real MCP protocol on a generated `.docx`:

```
$ npx @modelcontextprotocol/inspector --cli uvx markitdown-mcp \
    --method tools/call --tool-name convert_to_markdown \
    --tool-arg uri=file:///.../sample.docx
{
  "content": [{ "type": "text", "text":
    "# OmniAgentOS Tool Library\n\nThis is a tiny sample .docx generated to
    smoke-test the MarkItDown MCP server as part of the OmniAgentOS curated
    tool library.\n\n## Headline capabilities\n\n* Web search (Tavily /
    Brave)\n* Document conversion (MarkItDown)\n* Web fetch (fetch
    MCP)\n\nIf you can read this as Markdown, the pipeline works." }],
  "isError": false
}
```

...and again on a generated `.pdf`:

```
$ npx @modelcontextprotocol/inspector --cli uvx markitdown-mcp \
    --method tools/call --tool-name convert_to_markdown \
    --tool-arg uri=file:///.../sample.pdf
{
  "content": [{ "type": "text", "text":
    "OmniAgentOS Tool Library - PDF sample\n\nThis tiny PDF smoke-tests
    MarkItDown MCP PDF conversion.\n\nHeadline capability: document
    conversion via MarkItDown.\n\n" }],
  "isError": false
}
```

Both source files and their converted output are saved in
[`examples/`](examples/) for reference.

**2. fetch retrieves a URL** — via the actual `mcp-server-fetch` MCP
server, calling `fetch` over the real MCP protocol on a live URL:

```
$ npx @modelcontextprotocol/inspector --cli uvx mcp-server-fetch \
    --method tools/call --tool-name fetch --tool-arg url=https://example.com
{
  "content": [{ "type": "text", "text":
    "Contents of https://example.com/:\nThis domain is for use in
    documentation examples without needing permission. Avoid use in
    operations.\n\n[Learn more](https://iana.org/domains/example)" }],
  "isError": false
}
```

**3. DuckDuckGo search actually returns results, no key** — live query,
same MCP-protocol method:

```
$ npx @modelcontextprotocol/inspector --cli uvx duckduckgo-mcp-server \
    --method tools/call --tool-name search \
    --tool-arg query="Model Context Protocol" --tool-arg max_results=3
{
  "content": [{ "type": "text", "text":
    "Found 3 search results:\n\n1. Official site\n   URL:
    https://modelcontextprotocol.io\n   ...\n\n2. What is the Model Context
    Protocol (MCP)?\n   URL: https://modelcontextprotocol.io/docs/getting-
    started/intro\n   ...\n\n3. Introducing the Model Context Protocol \\
    Anthropic\n   URL: https://www.anthropic.com/news/model-context-
    protocol\n   ..." }],
  "isError": false
}
```

**4. `install-tools.sh` end-to-end doctor report** — real run, 7.4s on a
warm cache (first run, cold, takes longer while uvx/npx actually download
packages):

```
==================================================================
 OmniAgentOS Tool Library — installer / doctor
 tools dir: /Users/youruser/OmniAgentOS/tools
==================================================================

-- runtimes --
  node                   ... found (v22.22.0)
  npm                    ... found (10.9.4)
  npx                    ... found (10.9.4)
  python3                ... found (Python 3.12.1)
  uv                     ... found (uv 0.10.4 (60847fc09 2026-02-17))
  uvx                    ... found (uvx 0.10.4 (60847fc09 2026-02-17))
  git                    ... found (git version 2.43.0)
  curl                   ... found (curl 8.7.1 ...)

-- keyless / local MCP servers (installing + verifying) --
  fetch (web fetch)      ... OK
  markitdown (doc conversion) ... OK
  git (repo tools)       ... OK
  sqlite (local db)      ... OK
  filesystem (file tools) ... OK
  memory (knowledge graph) ... OK
  sequential-thinking (reasoning) ... OK
  playwright (browser)   ... OK
  duckduckgo (keyless web search) ... OK

-- needs-key MCP servers (NOT installed here — verified only) --
  tavily (web search)    ... package OK, key not set (export TAVILY_API_KEY to use)
  brave-search (web search) ... package OK, key not set (export BRAVE_API_KEY to use)

-- local state dirs --
  tools/state/           ... OK (/Users/youruser/OmniAgentOS/tools/state)

==================================================================
 DOCTOR REPORT
==================================================================
 KEYLESS SERVER                   STATUS
 fetch (web fetch)                OK
 markitdown (doc conversion)      OK
 git (repo tools)                 OK
 sqlite (local db)                OK
 filesystem (file tools)          OK
 memory (knowledge graph)         OK
 sequential-thinking (reasoning)  OK
 playwright (browser)             OK
 duckduckgo (keyless web search)  OK

 NEEDS-KEY SERVER                 KEY?       ENV VAR TO SET
 tavily (web search)              not set    TAVILY_API_KEY
 brave-search (web search)        not set    BRAVE_API_KEY

 keyless servers: 9 OK, 0 FAIL
 needs-key servers documented: 2 (set the env var above, then just use them — nothing else to install)
==================================================================
```

Re-run twice more (including once with a dummy `TAVILY_API_KEY` exported)
to confirm idempotency and that the "key SET vs not set" detection actually
works — both confirmed, `9 OK / 0 FAIL` every time, `SET` shown correctly
when the var was exported.

**5. `claude -p` actually calling a tool end-to-end, headlessly** — real
`claude --print` invocation, real MCP tool call, real result:

```
$ claude --print "Call the fetch tool on https://example.com and tell me the first line of text it returned." \
    --mcp-config tools/mcp-servers.local.json --strict-mcp-config \
    --allowedTools "mcp__fetch"

The fetch succeeded. The first line of text it returned was:
> This domain is for use in documentation examples without needing
  permission. Avoid use in operations.
```

This is also where the `--allowedTools`-needs-a-bare-server-name and
`--permission-mode acceptEdits`-isn't-enough-for-MCP findings (written up
in "Point `claude -p` at this library" above) came from — confirmed by
first reproducing the blocked-without-`--allowedTools` case, then fixing it.

---

## Adding a new tool later

1. Verify it actually exists and actually runs (`uvx <pkg> --help` /
   `npx -y <pkg> --help`, or the Inspector CLI `tools/list` trick above —
   don't add anything you haven't run).
2. Add it to `mcp-servers.json` **and to `../.mcp.json`** (and `.local.json`
   or `.keyed.json` as appropriate). Both, identically — `.mcp.json` is the
   one the runtime loads and is not a symlink to the other; the mech gate and
   the `mcp_roster` audit refuse when they disagree.
3. Add it to `configs/mcp-approved.yaml` with a justification, or both
   controls fail the roster as unapproved.
4. Add a row to the right table above with: what it does, how it's
   invoked, install method, and key Y/N + exact env var.
5. If it's local/keyless, add a `check_banner`/`check_registry_only` line
   in `install-tools.sh` so the doctor report covers it.
6. If it can reach the network or exec shell commands, re-read the
   Governance section before deciding whether it's ever appropriate for a
   lab candidate — default answer is no.

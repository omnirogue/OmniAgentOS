# system-prompts — how to add or change an agent's prompt

This folder is the one place that records **every agent role in the estate and where
its system prompt lives**. `ROLE-REGISTRY.yaml` is the list; the folders beside it
hold prompt text.

You do not need to be a developer to use it. Everything below is copy-paste.

Run every command from the repo root:

```bash
cd ~/OmniAgentOS
```

---

## 1. See what already exists

```bash
.venv/bin/python -m omniagentos.prompts list
```

Narrow it down:

```bash
.venv/bin/python -m omniagentos.prompts list --live          # only what is in use today
.venv/bin/python -m omniagentos.prompts list --kind daemon   # agent | loop | daemon | task | fragment
```

Read one prompt:

```bash
.venv/bin/python -m omniagentos.prompts show job.implementer
```

Or see who owns it, what version it is, and what reads it:

```bash
.venv/bin/python -m omniagentos.prompts show job.implementer --meta
```

If a role's prompt lives outside this repo (`location: external`) or inside a Python
file (`location: embedded`), that command will **refuse and tell you where the text
actually is**. That is on purpose — it never invents a prompt and never returns a
blank one.

---

## 2. Add a brand-new prompt

Say you want a role called `agent.ad-copywriter`.

**Step 1 — copy the worked example into place.**

```bash
mkdir -p system-prompts/agent.ad-copywriter
cp system-prompts/example.hello/v1.md system-prompts/agent.ad-copywriter/v1.md
```

**Step 2 — write the prompt.** Open the new file and replace everything in it with
your prompt. Plain Markdown. What you type is exactly what the agent receives.

```bash
open -a TextEdit system-prompts/agent.ad-copywriter/v1.md
```

**Step 3 — add it to the list.** Open `system-prompts/ROLE-REGISTRY.yaml` and add
this block at the end of the `roles:` list. Copy it exactly and change the parts in
your own words:

```yaml
  - id: agent.ad-copywriter
    description: Writes Facebook ad copy variants from a brand profile and a winning angle.
    owner: owner
    version: 1
    live: true
    kind: agent
    location: registry
    prompt_file: system-prompts/agent.ad-copywriter/v1.md
    consumers: []
```

Two rules that the checker enforces, so you cannot get them subtly wrong:

- `id` and the folder name must match.
- `prompt_file` must be `system-prompts/<id>/v<version>.md` — the version in the
  filename and the `version:` field can never disagree.

**Step 4 — check it.**

```bash
.venv/bin/python -m omniagentos.prompts check
```

`OK — …` means the system can find and read your prompt. If you got something
wrong, the message names the file, the field, and the fix. Then read it back:

```bash
.venv/bin/python -m omniagentos.prompts show agent.ad-copywriter
```

The full test suite for the registry, if you want it, is:

```bash
.venv/bin/python -m pytest -q tests/prompts
```

---

## 3. Change an existing prompt

**Small correction** (typo, one clearer sentence): just edit the `v1.md` file in
place. Nothing else to do.

**Real change you might want to undo** — make a new version instead, so the old one
stays readable:

```bash
cp system-prompts/agent.ad-copywriter/v1.md system-prompts/agent.ad-copywriter/v2.md
open -a TextEdit system-prompts/agent.ad-copywriter/v2.md
```

Then in `ROLE-REGISTRY.yaml` change **both** lines for that role:

```yaml
    version: 2
    prompt_file: system-prompts/agent.ad-copywriter/v2.md
```

Re-run `.venv/bin/python -m omniagentos.prompts check`. To roll back, point the two
lines at `v1.md` and `1` again — the old file was never deleted.

---

## 4. Prompts that live somewhere else

Some prompts are recorded here but their text is **not** kept in this folder. That
is deliberate, not an oversight:

| `location` | Where the text is | Can the loader read it? |
|---|---|---|
| `registry` | `system-prompts/<id>/v<version>.md` — this folder | Yes |
| `repo` | Another path already in this repo, e.g. `vault/prompts/roles/implementer.md` | Yes, from the live file |
| `external` | Outside the repo, e.g. `~/.claude/agents/opus-critic.md` | No — refuses, and prints the path |
| `embedded` | A string inside a `.py` file | No — refuses, and prints module + constant |

Why `repo` points at the live file instead of keeping a copy here: a copy that
nothing reads is a copy that drifts, and then this folder would be confidently
describing a prompt that is no longer the one running. One file, one truth.

Why `external` prompts are never copied in: prompts outside this repo can carry
private instructions and credential paths. Registering them by reference means the
inventory is complete without this repo becoming a place secrets leak into.

To bring an external or embedded prompt under version control later: copy the text
to `system-prompts/<id>/v1.md`, change `location:` to `registry`, replace
`source_ref:` with `prompt_file:`, and update whatever code was reading the old
location to call `get_prompt("<id>")` instead.

---

## 5. Reading a prompt from code

```python
from omniagentos.prompts import get_prompt, get_role, list_roles

text  = get_prompt("job.implementer")   # the prompt body, verbatim
entry = get_role("job.implementer")     # id, owner, version, live, consumers, notes
roles = list_roles(live_only=True)      # everything currently in use
```

Nothing here ever returns an empty string as a fallback. Each failure raises its own
error so it cannot be mistaken for a working prompt:

| Error | Means |
|---|---|
| `UnknownRoleError` | That role id is not in the registry (the message suggests near matches) |
| `PromptFileMissingError` | The registry points at a file that is not on disk |
| `EmptyPromptError` | The file is there but blank |
| `UnresolvablePromptError` | The role is `external` or `embedded` — read it where the message says |
| `RegistryFileError` | `ROLE-REGISTRY.yaml` itself is malformed (names the entry and the field) |

---

## 6. What this is not, yet

This is the inventory and the loader — Phase 0 of the System-Prompt Optimization &
Certification Loop (proposal `sha256:1d02c9a9…`). There are deliberately **no**
evals, tournaments, or certification gate here yet, and **most existing consumers
still read their prompts by their own path rather than through this registry**.
Registering a role does not by itself change what any agent runs.

`ROLE-REGISTRY.yaml` ends with a `KNOWN AND DELIBERATELY NOT REGISTERED` block
listing what was found and left out, and why.

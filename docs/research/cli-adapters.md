# Three-CLI Headless Adapter Cheat Sheet

## 1. CLAUDE (v2.1.207)

### Headless Command Template
```bash
claude -p "<prompt>" --output-format json --model <model> [--verbose] [--max-turns N]
```

### Key Flags for Headless Subprocess
- `-p, --print` : Print response and exit (required for non-interactive)
- `--output-format <format>` : `json` | `stream-json` (requires --verbose) | `plain`
- `--model <model>` : haiku, opus, sonnet (etc) — no CLI enumeration, error on invalid
- `--verbose` : Required when using `--output-format stream-json`
- `--resume [<session-id>]` : Resume by UUID or open picker
- `--fork-session` : With --resume, create new session ID instead of reusing
- `--session-id <uuid>` : Specify custom session UUID for new conversation
- `--add-dir <directories>` : Additional read/write directory access
- `--permission-mode <mode>` : acceptEdits, auto, bypassPermissions, manual, dontAsk, plan
- `--continue` : Resume most recent conversation in current directory

### JSON Output Envelope (--output-format json)
**Top-level fields:**
- `type` : string = "result"
- `subtype` : string = "success" | "error"
- `is_error` : boolean
- `api_error_status` : null | integer (HTTP status)
- `result` : string (the model response text)
- `stop_reason` : string = "end_turn" (reason model stopped)
- `terminal_reason` : string = "completed" (session termination reason)
- `session_id` : string (UUID, persists across --continue)
- `uuid` : string (unique message ID)
- `num_turns` : integer
- `duration_ms` : integer (total wall-clock time)
- `duration_api_ms` : integer (API call time only)
- `ttft_ms` : integer (time to first token)
- `ttft_stream_ms` : integer (stream-specific TTFT)
- `time_to_request_ms` : integer (latency to first request)
- `total_cost_usd` : float (sum of all model costs this session)
- `fast_mode_state` : string = "off" | "on"
- `permission_denials` : list (denied tool requests)
- `usage` : object (aggregate token counts)
  - `input_tokens` : integer
  - `output_tokens` : integer
  - `cache_creation_input_tokens` : integer
  - `cache_read_input_tokens` : integer
  - `service_tier` : string
  - `speed` : string
  - `inference_geo` : string
  - `server_tool_use` : object { web_search_requests, web_fetch_requests }
  - `cache_creation` : object { ephemeral_5m_input_tokens, ephemeral_1h_input_tokens }
  - `iterations` : array of { input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, type }
- `modelUsage` : object keyed by model ID, each with:
  - `inputTokens` : integer
  - `outputTokens` : integer
  - `cacheReadInputTokens` : integer
  - `cacheCreationInputTokens` : integer
  - `webSearchRequests` : integer
  - `costUSD` : float (this specific model's cost)
  - `contextWindow` : integer
  - `maxOutputTokens` : integer

### Cost/Token/Usage Location
- **Total cost:** `total_cost_usd` (top-level)
- **Token counts (aggregate):** `usage.input_tokens`, `usage.output_tokens`, `usage.cache_*`
- **Per-model cost:** `modelUsage.<model-id>.costUSD`
- **Cache metrics:** `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`

### Resume Mechanism
```bash
# Resume by ID
claude -p "<prompt>" --resume <uuid>

# Resume most recent, open picker
claude -p "<prompt>" --resume

# Resume and fork (new session ID)
claude -p "<prompt>" --resume <uuid> --fork-session

# Create new session with specific ID
claude -p "<prompt>" --session-id <new-uuid>
```

### Exit Code Behavior
- **0** : Success
- **1** : Error (invalid model, auth failure, etc.) — error message on stderr

### Latency (Empirical Probe)
- Probe: `claude -p "Reply with exactly: OK" --output-format json --model haiku`
- Real time: **~2.5s** (mostly API latency)
- Model latency: `duration_api_ms` = 2503ms

---

## 2. CODEX (v0.144.1)

### Headless Command Template
```bash
echo "<prompt>" | codex exec -m <model> -c model_reasoning_effort='"low"' --sandbox read-only --skip-git-repo-check -o <output-file> -
```
or
```bash
codex exec -m <model> -c model_reasoning_effort='"low"' --sandbox read-only --skip-git-repo-check -o <output-file> "<prompt>"
```

### Key Flags for Headless Subprocess
- `-m, --model <MODEL>` : gpt-5.6-luna, o1, o3, etc
- `-c, --config key=value` : Override config; `model_reasoning_effort='"low"'` (nested TOML, note quotes)
- `--sandbox <mode>` : read-only | workspace-write | danger-full-access
- `--skip-git-repo-check` : Allow running outside git repo
- `-C, --cd <DIR>` : Working directory
- `-o, --output-last-message <FILE>` : Write last agent message to file
- `--json` : Emit JSONL events to stdout (not plain text summary)
- `--ephemeral` : Don't persist session to disk
- `--ignore-user-config` : Skip `~/.codex/config.toml`

### JSONL Output Event Types (--json flag)
Each line is a separate JSON object. Event sequence:
1. `{"type":"thread.started","thread_id":"<UUID>"}`
2. `{"type":"turn.started"}`
3. `{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}`
4. `{"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":N,"output_tokens":N,"reasoning_output_tokens":N}}`

### Plain Text Output (default, no --json)
```
OpenAI Codex v0.144.1
--------
workdir: <path>
model: <model>
provider: openai
approval: never
sandbox: read-only
reasoning effort: low
session id: <UUID>
--------
user
<input prompt>

codex
<response text>

tokens used
<count>
<response text again>
```

### Cost/Token/Usage Location
- **Tokens (JSONL):** In `turn.completed` event: `usage.input_tokens`, `usage.cached_input_tokens`, `usage.output_tokens`, `usage.reasoning_output_tokens`
- **Cost:** Not reported in CLI output (check API response/logs separately)
- **Session ID:** In `thread.started` event (`thread_id` field); also in plain-text header
- **Last message text:** In `item.completed` event (`item.text`) and also written to `-o <file>`

### Resume Mechanism
```bash
# Resume specific session
codex exec resume <thread-id> -m <model> - < <prompt-stdin>

# Resume most recent session
codex exec resume --last -m <model> - < <prompt-stdin>

# Resume with new prompt
codex exec resume <thread-id> "<new-prompt>"
```

### Exit Code Behavior
- **0** : Success
- **1** : Error (invalid model, API failure, etc.) — error with JSON structure on stderr

### Latency (Empirical Probe)
- Probe: `echo "Reply with exactly: OK" | codex exec -m gpt-5.6-luna -c model_reasoning_effort='"low"' --sandbox read-only --skip-git-repo-check - `
- Real time: **~2.5s** (mixed CLI init + API)
- Token count: 2698 total

---

## 3. GROK (v0.2.93)

### Headless Command Template
```bash
grok -p "<prompt>" --output-format json --sandbox read-only --cwd <workdir>
```

### Key Flags for Headless Subprocess
- `-p, --single <PROMPT>` : Single-turn prompt, print response and exit
- `--prompt-file <PATH>` : Read single-turn prompt from file
- `--prompt-json <JSON>` : Single-turn prompt as JSON content blocks
- `--output-format <FORMAT>` : plain | json | streaming-json
- `--sandbox <PROFILE>` : read-only | workspace-write | danger-full-access (env: GROK_SANDBOX=)
- `--cwd <CWD>` : Working directory
- `--model <MODEL>` : grok-4.5 (default), grok-composer-2.5-fast, etc
- `--reasoning-effort <EFFORT>` : Aliases: --effort (for reasoning models)
- `--resume [<SESSION_ID>]` : Resume session by ID or most recent if omitted
- `--continue` : Continue most recent session for current working directory
- `--fork-session` : With --resume/--continue, create new session ID
- `--session-id <UUID>` : Use specific session UUID for new conversation
- `--always-approve` : Auto-approve all tool executions
- `--max-turns <N>` : Maximum agent turns
- `--permission-mode <MODE>` : default | acceptEdits | auto | dontAsk | bypassPermissions | plan

### JSON Output Envelope (-p "prompt" --output-format json)
**Top-level fields (single object, not JSONL):**
- `text` : string (model response)
- `stopReason` : string = "EndTurn" (stop condition)
- `sessionId` : string (UUID for this session)
- `requestId` : string (unique request ID)
- `thought` : string (model's internal reasoning/thought)

### Cost/Token/Usage Location
- **Cost:** Not reported in CLI output
- **Token counts:** Not reported in CLI output
- **Session ID:** `sessionId` field (needed for --resume)

### Models
```bash
grok models
```
Output shows available models and auth status. Default: `grok-4.5`

### Resume Mechanism
```bash
# Resume specific session
grok -p "<prompt>" --resume <session-uuid> --sandbox read-only

# Resume most recent session
grok -p "<prompt>" --resume --sandbox read-only

# Resume and fork (new session)
grok -p "<prompt>" --resume <session-uuid> --fork-session

# Create new session with specific ID
grok -p "<prompt>" --session-id <new-uuid>
```

### Exit Code Behavior
- **0** : Success (or device error on headless without tty)
- **1** : Error (invalid model, auth failure, etc.) — error message on stderr

### Latency (Empirical Probe)
- Probe: `grok -p "Reply with exactly: OK" --output-format json --sandbox read-only`
- Real time: **~2.6s**

---

## Comparison Table

| Feature | Claude | Codex | Grok |
|---------|--------|-------|------|
| **Headless flag** | `-p` | `codex exec` | `-p` |
| **JSON output** | `--output-format json` | `--json` (JSONL) | `--output-format json` |
| **Streaming** | `stream-json` (+ --verbose) | Not exposed | `streaming-json` |
| **Model selection** | `--model` | `-m` | `--model` |
| **Sandbox** | (via permissions) | `--sandbox` | `--sandbox` |
| **Session ID in output** | `session_id` (JSON) | `thread_id` (JSONL) | `sessionId` (JSON) |
| **Cost reported** | Yes, in `total_cost_usd` | No | No |
| **Token counts** | Yes, in `usage` | Yes, in `turn.completed` | No |
| **Resume by ID** | `--resume <uuid>` | `codex exec resume <id>` | `--resume <uuid>` |
| **Max turns** | (implicitly 1 with -p) | (implicit single-turn) | `--max-turns N` |
| **Exit code (error)** | 1 | 1 | 1 |
| **Latency (probe)** | ~2.5s | ~2.5s | ~2.6s |

---

## Subprocess Integration Recipes

### Claude in Python subprocess (json mode)
```python
import subprocess
import json

result = subprocess.run(
    ["claude", "-p", "Reply with exactly: OK",
     "--output-format", "json", "--model", "haiku"],
    capture_output=True, text=True, timeout=30
)
data = json.loads(result.stdout)
print(f"Response: {data['result']}")
print(f"Cost: ${data['total_cost_usd']}")
print(f"Session: {data['session_id']}")
print(f"Exit code: {result.returncode}")
```

### Codex in Python subprocess (--json mode)
```python
import subprocess
import json

proc = subprocess.run(
    ["echo", "Reply with exactly: OK"],
    capture_output=True, text=True
)
result = subprocess.run(
    ["codex", "exec", "--json", "-m", "gpt-5.6-luna",
     "-c", "model_reasoning_effort='\"low\"'",
     "--sandbox", "read-only", "--skip-git-repo-check", "-"],
    input=proc.stdout, capture_output=True, text=True, timeout=30
)
for line in result.stdout.strip().split('\n'):
    event = json.loads(line)
    if event.get('type') == 'item.completed':
        print(f"Response: {event['item']['text']}")
    elif event.get('type') == 'thread.started':
        print(f"Session: {event['thread_id']}")
    elif event.get('type') == 'turn.completed':
        print(f"Tokens: {event['usage']}")
print(f"Exit code: {result.returncode}")
```

### Grok in Python subprocess (json mode)
```python
import subprocess
import json

result = subprocess.run(
    ["grok", "-p", "Reply with exactly: OK",
     "--output-format", "json", "--sandbox", "read-only"],
    capture_output=True, text=True, timeout=30
)
data = json.loads(result.stdout)
print(f"Response: {data['text']}")
print(f"Session: {data['sessionId']}")
print(f"Thought: {data['thought']}")
print(f"Exit code: {result.returncode}")
```


"""Gemini narrative pass for Living Architecture Document (§8).

Query Google Gemini (or LiteLLM fallback) to update the hand-written narrative sections
of `ARCHI.md` based on recent git commits since the last documentation stamp.
On any error returns False and the CLI exits non-zero (failure is never shell-success).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from omniagentos.adapters.common import extract_trailing_json
from omniagentos.archdocs.generate import _repo_root_default
from omniagentos.archdocs.staleness import _STAMP_RE, parse_stamp_comment
from omniagentos.archdocs.update import apply_update


def run_narrative_update(
    repo_root: str | Path,
    model: str = "gemini-3.6-flash",
    timeout_s: int = 180,
) -> bool:
    """Run the narrative doc improvement pass.

    Reads previous HEAD from `ARCHI.md`'s stamp, gets git log since then,
    prompts Gemini, validates the response, and writes through `apply_update`.
    """
    repo_root = Path(repo_root).resolve()
    archi_path = repo_root / "ARCHI.md"

    try:
        if not archi_path.exists():
            sys.stderr.write(f"ARCHI.md does not exist at {archi_path}\n")
            return False

        archi_content = archi_path.read_text(encoding="utf-8")
        stamp = parse_stamp_comment(archi_content)
        if stamp is None:
            sys.stderr.write("No stamp comment found in ARCHI.md\n")
            return False

        old_head = stamp.git_head

        # Get git log since old_head
        try:
            cmd = ["git", "log", "--oneline", f"{old_head}..HEAD"]
            log_res = subprocess.run(
                cmd, capture_output=True, text=True, check=True, cwd=str(repo_root)
            )
            git_log = log_res.stdout.strip()
        except Exception as e:
            sys.stderr.write(f"Failed to get git log: {e}\n")
            return False

        if not git_log:
            sys.stderr.write(
                "No new commits since last stamp. Narrative pass will proceed with empty git log.\n"
            )

        git_log_lines = git_log.splitlines()[:200]
        git_log_capped = "\n".join(git_log_lines)

        # Build prompt
        prompt = f"""You are an expert AI software architect updating the Living Architecture Document for OmniAgentOS.

Below are the recent changes to the repository since the last documentation update (from git log):
```
{git_log_capped}
```

Below is the full current ARCHI.md file:
```markdown
{archi_content}
```

INSTRUCTIONS:
1. Return the COMPLETE, updated ARCHI.md text. Do not omit any sections or use placeholders.
2. Only improve the hand-written narrative sections to reflect the recent changes listed in the git log. The narrative sections are:
   - Subsystems
   - Entry points
   - Ports & env flags
   - How to extend
3. You MUST PRESERVE BYTE-FOR-BYTE the following parts from the current ARCHI.md text:
   - The first line stamp comment (e.g., <!-- archdocs:stamp ... -->).
   - All 4 generated blocks, including their begin/end delimiters (<!-- generated:begin:migrations --> ... <!-- generated:end:migrations -->, etc.). Do not change any contents inside these blocks!
   - The "## Notes (human)" section and everything below it to the end of the file.
4. Your response must only be the complete text of the updated ARCHI.md. Do not include any conversational preamble or postamble. Do not wrap the entire file in markdown code blocks unless it's the exact content of the file.
"""

        response_text = None

        # Attempt gemini CLI
        cli_cmd = [
            "gemini",
            "-p",
            prompt,
            "-o",
            "json",
            "-m",
            model,
        ]

        sys.stderr.write(f"Invoking gemini CLI with model {model}...\n")
        try:
            # start_new_session + killpg: subprocess.run(timeout=) cannot reach the
            # CLI's grandchildren, which keep the stdout pipe open and hang the
            # caller past its deadline. No approval mode: this is a pure
            # text-generation call, so attempted tool use should fail loudly.
            proc = subprocess.Popen(
                cli_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                cli_out, cli_err = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Best effort: the group is gone or unsignalable; killing
                    # the leader is all that is left without process walking.
                    proc.kill()
                try:
                    # Bounded reap: a survivor outside the group could still
                    # hold the pipe — never let the reap itself hang the caller.
                    proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        # wait() reaps the SIGKILLed leader without reading
                        # pipes, so no zombie survives this handler.
                        proc.wait(timeout=2)
                raise
            if proc.returncode == 0:
                decoded = extract_trailing_json(cli_out, envelope_keys=("response", "session_id"))
                if decoded and isinstance(decoded.get("response"), str):
                    response_text = decoded["response"]
                else:
                    sys.stderr.write(
                        "gemini CLI stdout parsing did not return a valid 'response' string.\n"
                    )
            else:
                sys.stderr.write(
                    f"gemini CLI returned non-zero exit code {proc.returncode}. Stderr: {cli_err}\n"
                )
        except subprocess.TimeoutExpired:
            sys.stderr.write("gemini CLI call timed out.\n")
        except Exception as e:
            sys.stderr.write(f"Error invoking gemini CLI: {e}\n")

        # Fallback to LiteLLM proxy
        if response_text is None:
            sys.stderr.write("Falling back to LiteLLM proxy...\n")
            url = "http://localhost:4000/v1/chat/completions"
            api_key = os.environ.get("GEMINI_API_KEY", "sk-local-litellm-proxy-secure")

            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

            data = {"model": model, "messages": [{"role": "user", "content": prompt}]}

            req = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
            )

            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                response_text = resp_data["choices"][0]["message"]["content"]

        if response_text is None:
            sys.stderr.write(
                "Error: Obtained empty response text from both CLI and LiteLLM fallback.\n"
            )
            return False

        # Validate the response
        # 1. 4 generated:begin markers
        markers = [
            "<!-- generated:begin:migrations -->",
            "<!-- generated:begin:launchd -->",
            "<!-- generated:begin:packages -->",
            "<!-- generated:begin:routes -->",
        ]
        for m in markers:
            if m not in response_text:
                sys.stderr.write(f"Validation failed: missing marker '{m}'\n")
                return False

        # 2. stamp line present
        if not _STAMP_RE.search(response_text):
            sys.stderr.write("Validation failed: missing or invalid stamp line\n")
            return False

        # 3. ## Notes (human) present
        if "## Notes (human)" not in response_text:
            sys.stderr.write("Validation failed: missing '## Notes (human)' section\n")
            return False

        # 4. length within 0.5x-2x
        current_len = len(archi_content)
        resp_len = len(response_text)
        if resp_len < 0.5 * current_len or resp_len > 2.0 * current_len:
            sys.stderr.write(
                f"Validation failed: response length ({resp_len}) is not within 0.5x-2x of current ({current_len})\n"
            )
            return False

        # Write only through update.py apply_update
        sys.stderr.write("Validation passed. Writing update through apply_update...\n")
        apply_update(
            repo_root=repo_root,
            target_path="ARCHI.md",
            new_content=response_text,
            actor="gemini-narrative",
            autocommit=None,
        )
        return True

    except Exception as e:
        sys.stderr.write(f"run_narrative_update failed: {e}\n")
        return False


def main() -> int:
    """CLI entry: 0 on successful narrative write, 1 when the pass fails."""
    repo_root = _repo_root_default()
    return 0 if run_narrative_update(repo_root) else 1


if __name__ == "__main__":
    sys.exit(main())

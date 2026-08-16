# Team telemetry — privacy contract

This is the plain-language contract for the monitoring that runs on team
machines. It is written for the people being monitored. If the code and this
document ever disagree, that is a bug — say so and it gets fixed.

## What runs on your machine

Exactly two scripts, both single self-contained Python files you can read:

| Feed | Script | What it sends | How often |
|---|---|---|---|
| Session heartbeat | `omniagentos/team/session_collector.py` | Which AI/terminal sessions were recently active: project name, one line per session (the session's first user message, truncated to 140 chars), last-active time, host name, and Claude account balance percentages | hourly |
| Transcript archive | `omniagentos/team/transcript_uploader.py` | Redacted copies of AI session transcripts (Claude Code, Codex, Kimi, Gemini, Grok) into the private `Globex/ai-transcripts` repo | hourly |

## What is NEVER collected

- No screen capture, no screenshots, no screen recording.
- No keystroke logging.
- No browser history, no browser content.
- No files outside AI session transcript directories (the exact glob list is
  at the top of `transcript_uploader.py` — nothing else is ever read).
- No microphone, camera, location, or personal directories.
- No hidden agents: the two scripts above are the entire footprint, run from
  your own crontab, visible with `crontab -l`.

## Read-only guarantee

Both scripts only **read** your files. Originals are never modified, moved,
or opened for writing. The only things written on your machine are the
scripts' own small state files in your home directory (watermarks, logs) and
the local clone of the transcript archive repo.

## Redaction

Transcript copies are redacted before they leave the machine: API keys,
tokens, private-key blocks and other live-credential shapes are replaced with
`[REDACTED:<kind>]` labels (see `SECRET_SHAPES` in `transcript_uploader.py`).
Originals are untouched.

## Your kill switch — no approval needed, any time

```sh
python3 ~/bin/telemetry_ctl.py off      # stop both feeds immediately
python3 ~/bin/telemetry_ctl.py on       # resume when you choose
python3 ~/bin/telemetry_ctl.py status   # see which state you're in
```

While off, **nothing is scanned and nothing is sent**. The only thing
reported is an "opted out since \<time\>" marker so the dashboard shows your
feed stopped *by your choice* — you never just look offline or AWOL. Nothing
on the server side can turn it back on; only you can, by running `on`.
(Equivalent manual form: `touch ~/.ai-telemetry-off` to stop,
`rm ~/.ai-telemetry-off` to resume.)

## Audit it yourself

See the exact payloads before they are sent:

```sh
python3 ~/bin/telemetry_ctl.py show-payloads --employee emp_you
```

This dry-runs both feeds and prints precisely what each would transmit.
Nothing is sent by this command.

## Task dispatch is independent

Receiving, claiming, and working queue tasks (the Team Work OS board, Slack
queue commands) does not depend on telemetry. With telemetry off you can
still be dispatched work, claim it, and complete it normally.

## Where the data goes, and retention

- Heartbeat reports go to the team tracker (drop-files under
  `var/team-sessions/` on the hub) and the hourly Slack team report. They are
  overwritten as newer reports arrive.
- Transcripts go to the private GitHub repo `Globex/ai-transcripts`,
  visible to the team, retained as git history.
- Telemetry exists for coordination and accountability (who is working on
  what, is a machine stuck, do we have Claude/Codex balance) — not for
  surveillance. Questions or concerns go straight to the operator.

# blocked-session fixtures

A minimal `~/.claude*`-shaped store used by tests/acceptance/s23_blocked_detector.sh.
`store/projects/-Users-fixture/` holds three transcripts:

* `1111...` last turn_duration carries `pendingBackgroundAgentCount: 3` -> the session is
  WORKING, not blocked. The test flips that field to 0 in a temp copy and asserts the
  same file then IS flagged.
* `2222...` the reference blocked shape (unanswered Bash tool_use is the last record).
* `3333...` the turn ENDED (last record is system/turn_duration) — the shape of 25 of
  account-3's 44 long gaps, and the false positive this detector exists to avoid.

Every transcript body contains the token `FIXTURE-SECRET-BODY-TOKEN-...` so the payload
step can prove no transcript content reaches an alert.

# Governed toolplane session attach

Sessions should prefer `omniagentos-tool` for governed tool use rather than an
unrestricted shell with ambient credentials.

The toolplane is manifest-scoped: an agent receives declared capabilities, not a
general credential-bearing environment. Calls are broker-backed, so the reviewed
HTTP boundary and action class are checked at execution time. Secret values stay
with the broker and are scrubbed from the session-facing surface.

Operator guidance for P2.5 session attach:

- Steer session prompts and hooks toward `omniagentos-tool` capability calls.
- Grant the smallest manifest scope that completes the task.
- Treat a shell path requiring ambient credentials as a governance exception,
  not the normal session workflow.
- Keep consequential capabilities in the approval path even when the toolplane
  is attached.

This is operating guidance only. It deliberately does not alter supervisor
prompt injection or session wiring.

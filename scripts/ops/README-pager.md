# Fleet pager

`fleet_pager.py` is a read-only launchd poll-and-diff pager for
`com.omniagentos.*` jobs. It observes `launchctl list`; it never loads,
unloads, kickstarts, or otherwise changes launchd state.

Run one pass from the repository root:

```sh
uv run python scripts/ops/fleet_pager.py --verbose
```

Schedule that command with cron or a LaunchAgent at the desired cadence. The
pager writes two durable files under `var/fleet-pager/`:

- `state.json` is the most recent loaded-unit baseline.
- `alert-state.json` records successful alert timestamps by unit and condition.

It pages at high severity when a previously observed unit changes to a negative
last-exit signal, or when a previously loaded unit is absent on the next
successful poll. The same unit and exact condition are suppressed for five
minutes. Notification records use `ref_type="fleet_pager"` and a date-scoped
unit `ref_id`.

If `launchctl list` or notification delivery fails, the error is logged and the
process exits normally. A failed `launchctl` call deliberately leaves the prior
baseline untouched, preventing it from being interpreted as every unit having
disappeared.

# Remote team board access — runbook

This runbook publishes the local Next.js dashboard's `/team` page at a hostname
protected by Cloudflare Tunnel and Cloudflare Access. It does not change the
dashboard, FastAPI API, or its server-side session-token boundary.

- Dashboard origin (do not target directly): `http://127.0.0.1:3003`
- **Tunnel target (Caddy hop):** `http://127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}` —
  check the running launcher's actual `OMNIAGENTOS_CADDY_PORT` before filling in
  `config.yml`; it defaults to `OMNIAGENTOS_DASH_PORT + 10`.
- Public hostname used below: `team-board.<your-zone>`
- Tunnel name: `omnios-team-board`
- Intended users: the operator, Alice, and Bob

## Who can see what (read this before touching Cloudflare)

**Session-report trust scope (updated 2026-08-11):** collector drop-files
(`var/team-sessions/<employee>.json`, landed via `POST /api/team/sessions/report`)
are no longer purely informational — the session-liveness pass advances that
employee's OWN open/claimed cards to in-progress based on them. The write is
owner-scoped and forward-only, so a forged report can at most mark the named
employee's own cards as being worked on; it can never touch anyone else's
cards or complete anything. Still: the shared tunnel principal cannot
distinguish who posted a report, which is one more reason per-person
principals remain the follow-up of record.


Port 3003 is hop-protected: the dashboard middleware and `serverProxy`'s
`requireTrustedHop` return 403 on any request that lacks the
`X-Omni-Trusted-Hop` secret, and only Caddy injects that secret
(`curl http://127.0.0.1:3003/api/team/board` → 403 "trusted proxy required",
measured). So the tunnel must target the Caddy hop
(`http://127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}`), not 3003 directly.

Caddy (`configs/dashboard-caddy/Caddyfile:82`) vouches **one static
principal** for every request it forwards, via
`Tailscale-User-Login {env.OMNIAGENTOS_DASHBOARD_PRINCIPAL}`. That means every
Access-allowlisted user — the operator, Alice, and Bob alike — is granted the exact
same identity: `OMNIAGENTOS_DASHBOARD_PRINCIPAL`. There is no per-person
principal today.

| # | What | Detail |
| --- | --- | --- |
| a | What the path-scoped hostname in this runbook exposes | `/team` (the board page); `/api/team/*` (team data); `/api/board` and `/api/board/{id}` (live board list plus single-card reads for the drawer — any card on the whole board, not just team-owned cards); `/api/ledger/tail` (ledger tail lines the drawer streams); `/api/health` (topbar status pill only — NOT `/api/pause`); `/api/auth/*` (login/logout); `/_next/*` and `/favicon*`/`manifest.json`/`icon*`/`apple-touch-icon.png` (static assets). Everything else is `http_status:404` at the Cloudflare edge, before it ever reaches Caddy. cloudflared matches path only, never method, so every rule above allows every HTTP verb the origin accepts on that path. |
| b | Shared identity | Every allowlisted user authenticates as one operator-grade principal — the value of `OMNIAGENTOS_DASHBOARD_PRINCIPAL` — because that is what Caddy injects for all traffic it forwards. There is no per-person distinction inside the app once past Access. |
| c | What is exposed WITHOUT the path scoping below | The entire dashboard control plane behind the shared principal: account metadata, `filesearch` (machine-wide file search), `workfs` (the whole `~/Work` tree), `employee-transcripts`, `provision`, and `system` — all reachable by anyone Access allows in. Do not widen the ingress `path:` rules in `configs/cloudflare/tunnel-config.yml.example` without re-reading this section. |

Per-person principals (mapping each Access identity to its own
`Tailscale-User-Login` value instead of one shared operator principal) are
documented follow-up work and are **out of scope for this runbook.**

**STOP.** Do not run `cloudflared service install` until the operator has read this
section and explicitly accepted the shared-principal residual risk described
in row (b) above.

## Preconditions

- the operator controls a Cloudflare zone and can edit its DNS and Access settings.
- The full stack, including Caddy, is running via
  `scripts/launch-omniagentos.sh all` (starting only the dashboard is not
  enough — the tunnel needs the Caddy hop, not the dashboard's own port).
- `brew` is installed on the Mac that runs the dashboard.
- Choose `<your-zone>` as a domain in a Cloudflare zone the operator controls; a dedicated
  subdomain such as `team-board.example.com` keeps this board separate from other
  public services.

Before any Cloudflare work, confirm the hop is actually up:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}/"
```

Expected result: a response code from the Caddy hop (not connection-refused).
If this fails, start the stack with
`scripts/launch-omniagentos.sh all` before continuing — do not proceed to
tunnel/DNS/Access steps against a hop that is not running.

## Install cloudflared

Install the client on the dashboard Mac:

```sh
brew install cloudflared
cloudflared --version
```

Expected result: the command prints a cloudflared version and exits `0`. If the
binary is not found, fix the Homebrew PATH before continuing.

## Create the named tunnel

Authenticate the client in a browser, then create the tunnel:

```sh
cloudflared tunnel login
cloudflared tunnel create omnios-team-board
```

Expected result: login completes in the browser; tunnel creation prints a tunnel
UUID and writes a credentials JSON file under `~/.cloudflared/`. Record the UUID
and the exact credentials-file path. Do not commit the credentials file.

If either command's flags or output differ, stop and verify the installed syntax
with `cloudflared tunnel --help` rather than guessing.

## Configure ingress

Copy the checked-in template. **This step overwrites
`~/.cloudflared/config.yml`.** If one already exists (for example, from
another named tunnel on this Mac), it is backed up to a timestamped `.bak`
file first — that `.bak` is your only restore point, so verify it was
actually written before proceeding:

```sh
mkdir -p ~/.cloudflared
if [ -e ~/.cloudflared/config.yml ]; then
  BAK=~/.cloudflared/config.yml.bak.$(date +%Y%m%dT%H%M%S)
  cp ~/.cloudflared/config.yml "$BAK"
  [ -s "$BAK" ] || { echo "STOP: backup at $BAK is missing or empty — do not proceed" >&2; exit 1; }
  echo "Backed up existing config.yml to $BAK"
fi
cp /Users/youruser/OmniAgentOS/configs/cloudflare/tunnel-config.yml.example \
  ~/.cloudflared/config.yml
```

The final `cp` intentionally has no `-n`: it always overwrites
`~/.cloudflared/config.yml` with the fresh template. Do not add `-n` back —
a no-clobber copy silently no-ops on every re-run once `config.yml` exists,
which means edits below never actually apply and the failure looks like a
validation problem instead of a stale file. If you need the previous config
back, restore it from the `.bak` file written above.

```sh
# Edit ~/.cloudflared/config.yml:
#   tunnel: <UUID printed by `tunnel create`>
#   credentials-file: the generated credentials JSON path
#   hostname: team-board.<your-zone> (every ingress rule uses the same hostname)
```

The ingress rules target the **Caddy hop**
(`http://127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}`), not the dashboard's own port —
see "Who can see what" above for why. The template is **path-scoped**: it
routes only `/team`, `/api/team/*`, `/api/board` and `/api/board/{id}`,
`/api/ledger/tail`, `/api/health`, `/api/auth/*`, `/_next/*`, and the static
assets (`/favicon*`, `manifest.json`, `icon*`, `apple-touch-icon.png`) to the
hop; everything else hits a per-hostname `http_status:404` rule before the
final catch-all. Each rule in
`configs/cloudflare/tunnel-config.yml.example` carries a one-line comment
explaining why it exists — including the caveat that cloudflared matches
`path:` only, never HTTP method, so a matched rule allows every verb the
origin accepts on that path. Do not widen these rules without re-reading "Who
can see what".

One consequence to state plainly: `/api/collab/board/{id}/claim` is **not** in
the allowlist, so remote users cannot claim cards from the board UI — that is
deliberate (the shared principal cannot attribute a claim to the right person).
Remote users claim via Slack — `claim <REF>` in the team channel or DM — which
resolves the sender's identity from `configs/team_slack_map.yaml` and
transfers ownership correctly.

Check the configuration before starting the service:

```sh
cloudflared tunnel ingress validate --config ~/.cloudflared/config.yml
```

Expected result: validation succeeds with exit code `0`. If this subcommand or
the global `--config` flag differs in the installed version, verify the
current spelling with `cloudflared tunnel --help` and use the documented
equivalent — do not guess.

Before starting the service, confirm no placeholder survived the edit:

```sh
grep -n '<your-\|<alice-email>\|<bob-email>' ~/.cloudflared/config.yml && \
  echo "STOP: unreplaced placeholder(s) above" || echo "OK: no placeholders remain"
```

## Route DNS

Create the DNS route for the hostname:

```sh
cloudflared tunnel route dns omnios-team-board team-board.<your-zone>
```

Expected result: cloudflared reports that the hostname was routed to the named
tunnel. Confirm that `<your-zone>` is the Cloudflare zone the operator controls and that
the hostname exactly matches the `hostname` entries in `config.yml`.

## Run it as a launchd service

**STOP before this step.** Confirm the operator has read "Who can see what" above and
explicitly accepted the shared-principal residual risk (every allowlisted
user authenticates as `OMNIAGENTOS_DASHBOARD_PRINCIPAL`) before installing the
service — this is the step that makes the tunnel reachable from the internet.

Install the service using the client-managed launchd setup:

```sh
cloudflared service install
```

On macOS this conventionally requires `sudo`, and the resulting root
LaunchDaemon reads `/etc/cloudflared/config.yml`, **not**
`~/.cloudflared/config.yml`. Verify the exact behavior of the installed
client with `cloudflared service --help` before running this — do not assume
it will pick up the per-user config file. If it does not, the alternative is
to copy the validated config to the root path it expects:

```sh
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
sudo cloudflared service install
```

Expected result: cloudflared installs a launchd service and reports the plist
location. Verify it is loaded and that the tunnel is running:

```sh
launchctl list | grep cloudflared
cloudflared tunnel info omnios-team-board
```

If `service install` is unavailable or requires a different privilege/flag on
this client version, stop and verify with `cloudflared service --help`; do not
invent a replacement command. A hand-written plist must use absolute paths,
an explicit PATH, a `WorkingDirectory`, and log paths under
`/Users/youruser/OmniAgentOS/var/log`, following the existing
`configs/launchd/com.omniagentos.team-report.plist` convention.

## Put Cloudflare Access in front

In the Cloudflare dashboard, create an Access application:

1. Open **Zero Trust → Access → Applications** and add a **Self-hosted** application.
2. Set the application domain to `team-board.<your-zone>` (the exact hostname,
   not the whole zone).
3. Choose a session duration appropriate for this team, for example **24 hours**.
   If the dashboard labels or available durations differ, follow the current
   dashboard UI.
4. Create one **Allow** policy whose selector is **Emails**, with exactly these
   two entries:
   `<alice-email>` and `<bob-email>` (replace both placeholders with the real
   addresses before saving).
5. Do not add an allow policy for everyone. The application must default-deny
   every other identity; leave the implicit default-deny behavior in place.

For a two-person allowlist, the Cloudflare Access free tier covers this team
size. the operator should confirm the account's current plan and displayed Access limits
before saving, because plan limits can change.

Remember: Access authenticates the *edge identity*. It does not distinguish
Alice from Bob once past Caddy — see "Who can see what" above.

## What Access does not replace

Cloudflare Access authenticates an identity at the edge. It does **not** replace
the dashboard application's own server-side session-token boundary. The Next.js
server-only proxy reads `var/secrets/sessions-token` and attaches it when
forwarding protected API requests; the browser never receives that token.

Keep both layers:

- Access prevents unauthenticated internet users from reaching the dashboard.
- The application token boundary still protects FastAPI routes if a request
  reaches the local service through another path, and preserves the app's
  authorization model behind Access.

Do not put the session token in the tunnel config, Access policy, browser URL,
or any client-side environment variable.

## Verification

With the dashboard running and the tunnel service loaded, test the edge without
cookies:

```sh
curl -sS -D - -o /dev/null https://team-board.<your-zone>/team
```

Expected result: an HTTP redirect to the Cloudflare Access login flow (or the
current Access login response), not the dashboard HTML. A direct `200` with the
dashboard before authentication is a failure: inspect the Access application
hostname and policy.

Open `https://team-board.<your-zone>/team` in a browser, authenticate as an
allowlisted user, and confirm that the team board renders. Confirm that an
address not in the allowlist is denied. In the authenticated browser, verify
that board data loads; the browser should not contain the session-token file's
value or path.

Check the Caddy hop origin separately when diagnosing the edge (do not check
port 3003 directly — it will 403):

```sh
curl -sS -D - -o /dev/null "http://127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}/team"
```

Expected result: the Caddy-fronted app responds. A local response does not
prove that the tunnel or Access policy is correct.

## Exit conditions

The runbook is complete only when all are true:

- `cloudflared tunnel info omnios-team-board` reports a connected/running tunnel.
- DNS resolves `team-board.<your-zone>` to Cloudflare edge addresses.
- Unauthenticated `/team` requests reach the Access login flow.
- Alice and Bob can authenticate and load `/team`.
- A non-allowlisted identity is denied.
- The local session-token boundary remains unchanged and server-only.
- `~/.cloudflared/config.yml` (and, if used, `/etc/cloudflared/config.yml`)
  contains no unreplaced `<...>` placeholder (verified with the `grep` command
  in "Configure ingress").

## Troubleshooting

| Symptom | Checks | Resolution / exit condition |
| --- | --- | --- |
| Tunnel not connecting | Check `launchctl list \| grep cloudflared`; run `cloudflared tunnel info omnios-team-board`; inspect the service log location reported by the installer. | Confirm the tunnel's `service:` targets the Caddy hop (`127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}`), the tunnel UUID matches `config.yml`, and the credentials file is readable by the login user. Exit when tunnel info shows a live connection. |
| DNS not resolving | Run `dig +short team-board.<your-zone>` and compare the hostname with both `config.yml` and the Access application. | Re-run the documented `cloudflared tunnel route dns` command only after correcting the hostname/zone. If the command syntax is unclear, verify `cloudflared tunnel --help`. Exit when DNS returns Cloudflare edge addresses. |
| Edits to `config.yml` never take effect after re-running the copy step | Check whether the copy command in "Configure ingress" was ever run with a `-n`/no-clobber flag added back — that silently no-ops once the file exists, so the template never actually lands and every subsequent edit is being made to a stale file. | Use the documented plain `cp` (no `-n`) so the template always overwrites; restore from the timestamped `.bak` first if you need to recover a prior manual edit. Exit when `diff ~/.cloudflared/config.yml configs/cloudflare/tunnel-config.yml.example` shows only your intended edits. |
| Access login loop | Check the browser's hostname, system clock, Access application domain, session duration, and email policy. Test in a private window and remove stale Access cookies. | Ensure the exact email is one of `<alice-email>` or `<bob-email>`, the IdP login returns to the same hostname, and no second Access application overlaps it. Exit when a fresh private-window login reaches `/team`. |
| Access succeeds but board data is 401/403 | **Diagnose the trusted-hop chain first, before the session-token file:** is `service:` in `config.yml` pointed at the Caddy hop (`127.0.0.1:${OMNIAGENTOS_CADDY_PORT:-3013}`), not `127.0.0.1:3003`? Is Caddy actually running (`scripts/launch-omniagentos.sh all` started the whole stack, not just the dashboard)? Does `OMNIAGENTOS_DASHBOARD_PRINCIPAL` resolve to a non-empty value? Only if all three check out, check that the local app's server-only session-token file exists and is readable by the dashboard process. | Point the tunnel at the Caddy hop and restart Caddy/the stack if either was wrong. Do not weaken Access or put the token client-side. Restore the app's existing session-token configuration and retest locally through the hop, then through Access. |

## Rollback / stop

To stop publishing the board, stop or unload the cloudflared launchd service
using the service name and commands shown by the installed client:

```sh
cloudflared service --help
```

Remove the Access application and DNS route from the Cloudflare dashboard only
after the operator confirms the exact hostname and intended rollback. This runbook does
not perform that change.

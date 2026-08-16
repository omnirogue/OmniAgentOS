# Local dashboard authentication

Use this only for a loopback `next dev` dashboard. Deployed dashboard traffic
must arrive through Caddy with `X-Omni-Trusted-Hop`; do not configure these
development variables in a deployed runtime.

The dashboard never trusts loopback by itself. This procedure supplies a
browser-native HTTP Basic credential, which middleware verifies before it adds
the private trusted-hop assertion and the configured local principal. The
credential works for navigation and same-origin EventSource, unlike a custom
request header.

## Start a local session

In the same terminal that starts the dashboard, generate a fresh local access
secret and configure the operator identity:

```sh
export OMNIAGENTOS_TRUSTED_HOP_SECRET="$(openssl rand -base64 32)"
export OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET="$(openssl rand -base64 32)"
export OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL="owner@example.test"
export OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP=1
make dash
```

Open `http://localhost:3003/api/auth/login?returnTo=/`. When the browser asks
for credentials, use username `omniagentos` and the value of
`OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET` as the password. The login route
then mints the normal signed, HttpOnly browser credential from the configured
principal; the FastAPI bearer token remains server-only.

Keep that terminal private and discard both generated values when the session
ends. A missing, malformed, or incorrect credential is refused. Setting only
`OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP=1` does not grant access: all four
variables above must be present, and `NODE_ENV=production` disables the local
mechanism unconditionally.

## Security boundary

This is an explicit local-development substitute for Caddy, not a deployed
authentication topology. The dashboard binds to `127.0.0.1`; if it is exposed
through any proxy or non-loopback listener, remove the development variables
and use Caddy's strip-and-reinject hop-secret configuration instead.

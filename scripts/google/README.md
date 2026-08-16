# Google Workspace OAuth setup

One-time setup to give the broker a refresh token for the `google_sheets` /
`google_docs` / `google_drive_files` connectors (see `configs/connectors.yaml`,
group `google`). The token lives ONLY in `~/.config/omni/connections.env` and is
never injected into agent subprocesses (group `google` is not in
`broker.INJECTABLE_GROUPS`).

## Steps

1. **Desktop OAuth client** — in Google Cloud Console → APIs & Services →
   Credentials, create an **OAuth client ID** of type **Desktop app** (Desktop
   clients allow the `http://localhost` loopback redirect this script uses; a
   Web-app client returns `redirect_uri_mismatch`). Note its client ID + secret.
2. **Enable the APIs** in the same project: Google Drive API, Google Sheets API,
   Google Docs API (`gcloud services enable drive.googleapis.com
   sheets.googleapis.com docs.googleapis.com --project=<id>`, or click ENABLE in
   the API Library). A call before this returns `403 … has not been used …`.
3. **Mint the refresh token**:
   ```sh
   OAUTH_CLIENT_ID=<desktop-client-id> \
   OAUTH_CLIENT_SECRET=<desktop-client-secret> \
   OAUTH_PORT=8765 \
   python3 scripts/google/oauth_loopback.py
   ```
   Open the printed `AUTH_URL=` link, approve consent (scopes: spreadsheets,
   drive, documents), and the loopback captures the code and writes
   `GOOGLE_OAUTH_REFRESH_TOKEN` / `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` into the vault (chmod 600).
4. **Reload** so the broker picks up the new env:
   `launchctl kickstart -k gui/$(id -u)/com.omniagentos.api`.

## Verify

```python
from omniagentos.connectors import broker
broker.call("google_sheets.read", ["google_sheets.read"], method="GET",
            path="/v4/spreadsheets/<id>/values/A1:B1")["body"]
```

Scopes requested are Drive + Sheets + Docs only (no `cloud-platform`); the broker
path allowlist further pins each capability to specific endpoints.

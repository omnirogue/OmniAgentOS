# Email access setup — foundation for the triage + reply system

This wires the system to **all four of the operator's mailboxes** so it can read (triage) and
reply (send) to each. The code foundation is built; the remaining steps require **the operator's
own authentication** and only the operator can do them (an agent must never enter the operator's passwords
or complete an OAuth consent on his behalf).

| # | Mailbox | Provider | Read | Reply |
|---|---------|----------|------|-------|
| 1 | `owner@initech.example` | Google Workspace | broker (Gmail REST) | broker `gmail_initech.send` |
| 2 | `example-org@example.com` | Personal Gmail | broker (Gmail REST) | broker `gmail_ownera.send` |
| 3 | `owner@acmeuni.example` | Google Workspace | broker (Gmail REST) | broker `gmail_acmeuni.send` |
| 4 | `owner@globex.example` | **Titan Mail** | IMAP poller | SMTP sender |

**Access model (already the estate's architecture):** Google accounts read + reply
through the capability broker over OAuth + the Gmail REST API — one shared OAuth client,
one refresh token per account. Titan is not Google, so it uses stdlib IMAP (read) + a
stdlib SMTP sender (reply — the sender module lands with the triage layer that calls it;
this foundation verifies the SMTP send capability). Registry of all four: `configs/mailboxes.yaml`.

**Safety:** every reply is **draft-then-approve**. This foundation makes replying
*possible and approval-gated* — it never auto-sends. `gmail.send` is `consequential` in
the broker; the Titan send path will be human-approval-gated the same way.

Verify progress at any time (prints a per-account READ/SEND matrix; sends nothing):

```bash
cd ~/OmniAgentOS && uv run python scripts/email/verify_mailboxes.py
```

---

## What's already done (no action needed)

- `gmail_acmeuni` connector added; `.send` capabilities added to the ownera, initech,
  and acmeuni connectors (all `consequential` / approval-gated).
- `configs/mailboxes.yaml` — the unified account registry.
- `scripts/email/verify_mailboxes.py` — the access verifier (Gmail token+scope+identity;
  Titan IMAP read + SMTP send-capability, login only).
- (Follow-up) the Titan SMTP **sender** module ships with the triage/reply layer that
  calls it — kept out of this foundation to avoid landing an unwired code path.

Current state (from the verifier): accounts 1–2 have refresh tokens but they now return
**`invalid_grant`** (expired/revoked) — they must be re-minted regardless of scope;
account 3 (`acmeuni`) has **no** token yet; account 4 (`globex`) has **no** Titan
credentials yet. Step 1 mints fresh read+send tokens for accounts 1–3; Step 2 adds
account 4. The steps below close every gap.

---

## Step 0 — one-time Google Cloud Console prep (accounts 1–3)

The shared OAuth client needs the **`gmail.send`** scope allowed on its consent screen,
and each Gmail account must be permitted to consent.

1. Google Cloud Console → the project that owns `GOOGLE_OAUTH_CLIENT_ID` → **APIs &
   Services → OAuth consent screen**.
2. Under **Scopes**, ensure both are present: `.../auth/gmail.readonly` and
   `.../auth/gmail.send`. Add `gmail.send` if missing.
3. If the app is in **Testing**, add each of the three Google addresses under **Test
   users** (`owner@initech.example`, `example-org@example.com`,
   `owner@acmeuni.example`). Publishing is not required for personal use.
4. Ensure the **Gmail API** is enabled for the project (APIs & Services → Enabled APIs).

> When you run Step 1 you'll see a "Google hasn't verified this app" screen — that's
> expected for a personal OAuth client. Click **Advanced → Continue** to proceed.

---

## Step 1 — mint the three Google tokens with read + reply scope (the operator runs)

Run each block from `~/OmniAgentOS`. Each prints `AUTH_URL=...`; **open that URL,
sign in as the exact account named, and click Allow.** The refresh token is written to
`~/.config/omni/connections.env` automatically — no secret is ever printed.

> **Important:** sign in as the *correct* account each time. Use the Google account
> chooser, or run each block in a separate **Incognito** window, so account 3's consent
> doesn't accidentally get granted by account 1's logged-in session.

```bash
cd ~/OmniAgentOS
set -a; . ~/.config/omni/connections.env; set +a   # loads GOOGLE_OAUTH_CLIENT_ID/SECRET

# --- account 1: owner@initech.example ---
OAUTH_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID" OAUTH_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET" \
OAUTH_KEY_SUFFIX=INITECH \
OAUTH_SCOPES="openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send" \
python3 scripts/google/oauth_loopback.py
```

```bash
# --- account 2: example-org@example.com ---
OAUTH_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID" OAUTH_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET" \
OAUTH_KEY_SUFFIX=OWNERA \
OAUTH_SCOPES="openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send" \
python3 scripts/google/oauth_loopback.py
```

```bash
# --- account 3: owner@acmeuni.example  (new token) ---
OAUTH_CLIENT_ID="$GOOGLE_OAUTH_CLIENT_ID" OAUTH_CLIENT_SECRET="$GOOGLE_OAUTH_CLIENT_SECRET" \
OAUTH_KEY_SUFFIX=AcmeUni \
OAUTH_SCOPES="openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send" \
python3 scripts/google/oauth_loopback.py
```

Each ends with `SUCCESS=stored GOOGLE_OAUTH_REFRESH_TOKEN_<SUFFIX> ...`.

> Want the triage layer to also **label/archive/mark-read** while triaging (not just
> read)? Add `https://www.googleapis.com/auth/gmail.modify` to `OAUTH_SCOPES`. Not
> required for read + reply; can be added later by re-running the block.

---

## Step 2 — Titan credentials for owner@globex.example (the operator adds to the vault)

Titan authenticates IMAP/SMTP with the **mailbox password** (get/reset it in the Titan
control panel for `globex.example`; ensure IMAP/SMTP external access is enabled). Add
these lines to `~/.config/omni/connections.env` (the file stays mode `600`):

```
IMAP_HOST_GLOBEX=imap.titan.email
IMAP_USER_GLOBEX=owner@globex.example
IMAP_PASSWORD_GLOBEX=<the mailbox password>
SMTP_HOST_GLOBEX=smtp.titan.email
SMTP_PORT_GLOBEX=465
SMTP_USER_GLOBEX=owner@globex.example
SMTP_PASSWORD_GLOBEX=<the mailbox password>
```

Then register the read source (creates the `comms_sources` row + cursor):

```bash
cd ~/OmniAgentOS && uv run python -m omniagentos.comms.poll --source globex --once
```

---

## Step 3 — verify + reload

```bash
cd ~/OmniAgentOS
uv run python scripts/email/verify_mailboxes.py      # expect 4/4 READ+SEND ready
launchctl kickstart -k gui/$(id -u)/com.omniagentos.api   # reload broker with the new connector
```

When the verifier shows **4/4**, the foundation is in place: the system can read and
(with human approval) reply from all four mailboxes. The triage/classification + reply-
drafting layer is the next build on top of this.

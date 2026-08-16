# Server Inventory

This document is the single source of truth for `omniagentos.sessions.ssh_keys`:
`read_server_inventory_hosts` reads ONLY the `## Summary Table` below and fails
closed to `[]` on any malformed row (it is the SSH-grant-eligible allowlist);
`read_server_inventory` best-effort parses every section here (Summary +
Ephemeral + Legacy/Stale) as a display feeder for the dashboard's Servers
section and is never used to authorize anything. Edit this table when a box
is added, retired, or renamed — do not hand-edit a session's grant file
instead.

IP addresses below are placeholders drawn from the documentation ranges
reserved by RFC 5737 (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`);
this checkout does not carry live infrastructure addresses.

## Summary Table

| Alias | IP | User | Key | Status | Runs | Sites |
|---|---|---|---|---|---|---|
| initech-roi-calculator | 192.0.2.11 | deploy | ~/.ssh/initech_roi_ed25519 | ACTIVE | ROI calculator app | initech-roi-calculator.example.com |
| acmeuniapp | 192.0.2.12 | deploy | ~/.ssh/acmeuni_app_ed25519 | ACTIVE | AcmeUni member app | acmeuniapp.example.com |
| acmeuni | 192.0.2.13 | deploy | ~/.ssh/acmeuni_ed25519 | ACTIVE | AcmeUni marketing site | acmeuni.example.com |
| acmeunistudio | 192.0.2.14 | deploy | ~/.ssh/acmeuni_studio_ed25519 | ACTIVE | AcmeUni content studio | acmeunistudio.example.com |
| initech-crmnew | 192.0.2.15 | deploy | ~/.ssh/initech_crmnew_ed25519 | ACTIVE | Initech CRM (new) | initech-crmnew.example.com |
| acmeuniunlimited.com | 192.0.2.16 | deploy | ~/.ssh/acmeuni_unlimited_ed25519 | ACTIVE | AcmeUni unlimited-tier site | acmeuniunlimited.com |
| initechapp.com | 192.0.2.17 | deploy | ~/.ssh/initech_app_ed25519 | ACTIVE | Initech member app | initechapp.com |
| acmeuni-claude | 192.0.2.18 | deploy | ~/.ssh/acmeuni_claude_ed25519 | ACTIVE | AcmeUni Claude workspace | acmeuni-claude.example.com |
| agentproacademy | 192.0.2.19 | deploy | ~/.ssh/agentproacademy_ed25519 | ACTIVE | Agent Pro Academy site | agentproacademy.example.com |

## Ephemeral Servers

Rented/spun-up compute — never SSH-grant eligible; excluded from
`read_server_inventory_hosts` because these rows live outside the Summary
Table.

| Alias / Destination | Host | User | Key | Status | Runs |
|---|---|---|---|---|---|
| RunPod LipForcing pod | 198.51.100.20:2222 | root | ~/.ssh/runpod_lipforcing_ed25519 | EPHEMERAL | lip-sync render job |
| RunPod Qwen3.5-122B pod | 198.51.100.21:2222 | root | ~/.ssh/runpod_qwen35_122b_ed25519 | EPHEMERAL | Qwen3.5 122B inference job |

## Legacy / Stale Servers

Boxes kept for record-keeping only — never SSH-grant eligible; excluded from
`read_server_inventory_hosts` because these rows live outside the Summary
Table.

| Alias | IP | User | Key | Status | Runs | Sites |
|---|---|---|---|---|---|---|
| legacy-site-a | 203.0.113.30 | root | ~/.ssh/legacy_771_ed25519 | LEGACY-STALE | decommissioned member site | legacy-site-a.example.com |
| tryacmeuni | 203.0.113.31 | root | ~/.ssh/legacy_771_ed25519 | LEGACY-STALE | decommissioned trial site | tryacmeuni.example.com |

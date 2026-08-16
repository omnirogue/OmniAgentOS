# Jira saved filters — canonical set

Operational record for the `example-team.atlassian.net` saved filters that back
taxonomy §10 (c). Companion to `configs/jira_fields.yaml` (field ids) and
`contracts/jira-goals-api.md` (route contract).

Live project keys: `ACM` · `CA` · `INI` · `HOO` · `OAOS`.

## The set

All shared to `authenticated` (every logged-in user), `favourite=false`.

| id | Name | JQL |
|----|------|-----|
| 10011 | AI proposals awaiting triage | `"AI Proposal State" = "AI-proposed" AND statusCategory != Done` |
| 10012 | Sales — all companies | `Department = Sales AND statusCategory != Done ORDER BY priority DESC` |
| 10013 | Customer Service — all companies | `Department = "Customer Service" AND statusCategory != Done ORDER BY priority DESC` |
| 10014 | Operations — all companies | `Department = Operations AND statusCategory != Done ORDER BY priority DESC` |
| 10007 | Blocked — all companies | `status = Blocked ORDER BY updated ASC` |
| 10008 | My work | `assignee = currentUser() AND statusCategory != Done ORDER BY priority DESC` |

10011–10014 are the four field-dependent filters; they were blocked until the ten
per-project custom-field ids existed. 10007/10008 predate them.

Filter **10002 `Filter for GLO board`** (`project = "GLO"`) is a leftover from a
deleted project and matches nothing. Safe to delete.

## Why the bare field name works

`Department` and `AI Proposal State` each exist as **five separate custom fields**
— one per project, by design (see `configs/jira_fields.yaml`). That raises an
obvious question for JQL: does a bare `Department = Sales` resolve, or does it need
the explicit `cf[...]` form?

It resolves. Jira ORs across every field sharing the name.

This is worth recording because the read-only signals are misleading:

- `/jql/autocompletedata` offers **no** bare `Department` entry — only
  `cf[10043]`, `cf[10046]`, `cf[10048]`, `cf[10050]`, `cf[10051]`. Autocomplete
  alone suggests the bare name is unusable. It is usable.
- `POST /search/jql` with a **nonexistent option value** returns `200` with zero
  results rather than an error, so a bogus query is indistinguishable from a
  correct-but-empty one. You cannot validate a filter by running it against empty
  data.

Verified empirically instead: set `Department = Sales` on `INI-8`
(`customfield_10050`), confirmed both `Department = Sales` and `cf[10050] = Sales`
returned `INI-8`, then cleared the field. Do not "verify" these filters by
observing zero matches — that proves nothing until issues carry Department values.

## API gotchas

**Share type has three spellings.** Creating a share permission requires
lowercase `authenticated`. It reads back as `loggedin`. The error message
advertises `AUTHENTICATED`, which is rejected.

```
POST /rest/api/3/filter/{id}/permission   {"type":"authenticated"}   -> 200, reads back as "loggedin"
                                          {"type":"AUTHENTICATED"}   -> 400
                                          {"type":"loggedin"}        -> 400
```

**`sharePermissions` inline on create is rejected.** `POST /rest/api/3/filter`
with a `sharePermissions` array fails with the same 400. Create the filter first,
then add the permission via the `/permission` sub-resource. Two calls, always.

**Quick filters cannot be created via REST.**
`POST /rest/agile/1.0/board/{id}/quickfilter` returns **405 Method Not Allowed**;
the endpoint is GET-only. Board quick filters (taxonomy §10 (d)) are UI-only —
Board → ⋯ → Configure board → Quick filters. All five boards currently have none.

## Boards

| id | Board | Project |
|----|-------|---------|
| 2 | KAN board | INI |
| 4 | HOO board | HOO |
| 5 | CA board | CA |
| 6 | ACM board | ACM |
| 8 | OH board | OAOS |

Board 8 is still named `OH board` from before the `OH` → `OAOS` key rename.
Cosmetic only.

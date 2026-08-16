---
id: copywriting_brain_vault
type: playbook
discipline: advertising
created: '2026-07-27T00:00:00Z'
source_run: null
confidence: high
status: active
supersedes: null
---
<!-- skill-library
slug: copywriting_brain_vault
category: Advertising
subcategory: Copywriting
title: Ground Ad Copy in the Copywriting Reference Vault
summary: Where the copywriting reference vault lives and how to use it when writing any ad copy
preferred_method: Search the reference vault for proven structural patterns, adapt skeletons to the brand's real claims
fallback_method: Write from the brand pack alone and flag that vault grounding was unavailable
-->

# Skill: Ground Ad Copy in the Copywriting Reference Vault

## Purpose

Any task that writes ad copy, hooks, or sales pages should ground itself in
this estate's copywriting reference vault before drafting from scratch. The
vault holds a large corpus of prior high-performing copy, organized by
format, angle, and discipline — but it is a REFERENCE for structure, not a
source of sentences to lift.

## When to Use

Use this skill whenever a task's discipline is advertising or copy-adjacent:
ad hooks, landing page sections, email subject lines, webinar scripts, and
similar. Skip it for purely technical or operational tasks that never touch
customer-facing copy.

## Preferred Method

### Steps

1. Identify the format and angle the task actually needs (hook, headline,
   objection-handling block, etc.) before searching the vault.
2. Search the vault for entries matching that format and angle.
3. Extract structural skeletons, never sentences — the pattern of how a
   proven piece opens, builds tension, and closes, not its literal wording.
4. Re-fill the extracted skeleton with the CURRENT brand's real claims,
   offer, and voice. A skeleton filled with someone else's specifics is
   still someone else's copy.
5. Validate the result reads as this brand's voice, not a reskin of the
   reference entry.

### Prompts / Scripts / Configs

[Vault search tooling and its own agent guide are linked from the vault
note.]

### Inputs / Outputs

- Inputs: task discipline, target format/angle, current brand pack
- Outputs: copy grounded in a validated structural pattern
- Cost: negligible beyond the normal drafting pass
- Time: a few minutes of vault search before drafting

## Fallback Method

Write from the brand pack alone and flag that vault grounding was
unavailable — this is a degraded pass, not a silent equivalent, and should
be named as such in the output.

## Known Failures

Copying a matched entry's sentences verbatim instead of its structure is the
one failure mode this skill exists to prevent — it produces copy that reads
as someone else's brand wearing this brand's name.

## Last Validated

- Version: 1
- Date: 2026-07-27
- Changelog: Initial seed

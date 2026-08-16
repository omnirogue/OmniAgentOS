"""Bounded standing campaign grants.

Campaign grants are deliberately separate from one-shot approvals: an approval
may authorize creation of a bounded grant, while this package tracks and
atomically exhausts the actions performed under that grant.
"""

from omniagentos.grants.reader import format_grant_lines
from omniagentos.grants.store import ConsumeResult, GrantsStore, is_grant_live
from omniagentos.grants.validation import (
    ApprovalValidation,
    normalize_grant_ref,
    validate_approval_token,
)

__all__ = [
    "ApprovalValidation",
    "ConsumeResult",
    "GrantsStore",
    "format_grant_lines",
    "is_grant_live",
    "normalize_grant_ref",
    "validate_approval_token",
]

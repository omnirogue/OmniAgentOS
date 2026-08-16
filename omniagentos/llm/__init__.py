"""LLM short-call package.

Re-exports the core ShortCallClient and its associated budget guards and exceptions.
"""

from omniagentos.llm.budget import (
    BudgetGuard,
    LLMBudgetExceededError,
    LLMBudgetUnknownError,
    LLMClientError,
    LLMError,
    LLMInvalidResponseError,
    LLMTransportError,
)
from omniagentos.llm.client import ShortCallClient
from omniagentos.llm.error_taxonomy import (
    NonRetryableClass,
    RetryClass,
    classify,
)

__all__ = [
    "ShortCallClient",
    "BudgetGuard",
    "LLMError",
    "LLMClientError",
    "LLMBudgetExceededError",
    "LLMBudgetUnknownError",
    "LLMTransportError",
    "LLMInvalidResponseError",
    "RetryClass",
    "NonRetryableClass",
    "classify",
]

"""Bounded in-process implementation of the credential-free Echo connector.

Echo is the first-light capability: it proves identity, grant, broker audit,
and revocation without a secret, network dependency, paid call, write, or
communications surface.  The registry's ``inprocess://echo`` spec is reviewed
as narrowly as an HTTP allowlist (POST ``/ping`` only); the broker dispatches it
here after normal authorization and request-path validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _EchoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message: str = Field(min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class EchoRequestError(ValueError):
    """The Echo request failed its closed, bounded schema."""


def _dispatch_echo(body: Any) -> dict[str, Any]:
    """Validate a ping and return its message without doing I/O."""
    try:
        request = _EchoRequest.model_validate(body)
    except ValidationError as exc:
        raise EchoRequestError("echo request must contain only a non-empty message <=500 chars") from exc
    return {"pong": True, "message": request.message}

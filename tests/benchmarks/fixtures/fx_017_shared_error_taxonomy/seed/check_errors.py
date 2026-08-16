"""
Visible checks for seed error taxonomy.
This ensures the seed implementation of errors works as expected before refactoring.
"""

from __future__ import annotations

import api
import cli
import errors
import worker


def test_seed_errors() -> None:
    # NotFound
    exc_nf = errors.NotFound("item is missing")
    assert api.to_response(exc_nf) == {"code": "E_NOT_FOUND", "status": 404, "retryable": False}
    assert worker.should_retry(exc_nf) is False
    assert worker.dead_letter_reason(exc_nf) == "E_NOT_FOUND: item is missing"
    assert cli.exit_code_for(exc_nf) == 4
    assert cli.render(exc_nf) == "error [E_NOT_FOUND]: item is missing"

    # Internal / general
    exc_err = ValueError("boom")
    assert api.to_response(exc_err) == {"code": "E_INTERNAL", "status": 500, "retryable": True}
    assert worker.should_retry(exc_err) is True
    assert worker.dead_letter_reason(exc_err) == "E_INTERNAL: boom"
    assert cli.exit_code_for(exc_err) == 1
    assert cli.render(exc_err) == "error [E_INTERNAL]: boom"


if __name__ == "__main__":
    test_seed_errors()
    print("All seed checks passed!")

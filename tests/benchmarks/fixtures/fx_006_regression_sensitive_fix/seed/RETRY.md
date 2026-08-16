# Retry contract

`should_retry(status, attempt, max_attempts)` returns True only when another
attempt is both permitted and useful.

1. Never retry once `attempt >= max_attempts`. `attempt` is 1-indexed: the
   value passed is the number of the attempt that just finished.
2. Never retry a success (`status < 400`).
3. Never retry a permanent client error: any 4xx **except** the two the
   contract classifies as transient.
4. Always retry the transient client errors `408 Request Timeout` and
   `429 Too Many Requests`.
5. Always retry a server error (`status >= 500`).

`backoff_delay(attempt, base, cap)` returns `min(base * 2 ** (attempt - 1), cap)`
and is already correct — do not change it.

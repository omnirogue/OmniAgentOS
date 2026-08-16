"""Closed vocabularies and identifiers for employee transcript uploads."""

from __future__ import annotations

TRANSCRIPT_ID_PREFIX = "tu"
TRANSCRIPT_SOURCES = ("manual", "chat", "dev_session", "slack")
TRANSCRIPT_STATUSES = ("uploaded", "analyzed", "error")

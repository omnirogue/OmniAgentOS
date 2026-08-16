"""PiedPiper (AcmeUni) sales-pipeline sensing — read-only, dark-safe by default.

This package boundary imports no write/send path. Everything here reads
through the credential broker under ``piedpiper_acmeuni.read`` (GET-only, four fixed
CRM rollups) and persists scalars via ``StewardStore.insert_metric_snapshot``.
It never resolves a credential, calls the broker, or writes a row unless an
operator-issued ``piedpiper_acmeuni.read`` grant already exists for the collector's
holder identity — see :mod:`omniagentos.piedpiper.pipeline_report` for the
preflight that keeps this dark until that grant is issued.
"""

from __future__ import annotations

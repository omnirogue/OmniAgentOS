"""Read-oriented Jira Cloud REST v3 client (internal — C7/C8).

Uses GET ``/rest/api/3/search/jql`` only (legacy ``/search`` removed; POST search
is a JG-5 / post-P3-BROKER hand-off). Paginate with ``nextPageToken`` / ``isLast``;
never invent ``total``/``startAt`` loops. Sanitizes errors like
``comms/pollers/slack.py`` — never ``str(exc)`` on ``httpx.HTTPStatusError``
(embeds request URL / auth).

Logical custom-field names resolve through ``configs/jira_fields.yaml``; raw
``customfield_*`` ids are refused at the public boundary (A1).
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from omniagentos.connectors import broker, load_registry
from omniagentos.connectors.jira_retry import (
    RetryDecision,
    classify_rate_limit_reason,
    issue_key_from_path,
    parse_rate_limit_reset,
    with_retries,
)

DEFAULT_FIELDS: tuple[str, ...] = (
    "summary",
    "status",
    "assignee",
    "priority",
    "duedate",
    "description",
    "issuetype",
    "project",
    "parent",
    "labels",
    "updated",
    "created",
)

_CUSTOMFIELD_PREFIX = "customfield_"


def _credential(env_name: str, default: str = "") -> str:
    """Resolve Jira configuration through its connector-scoped audit path."""
    try:
        return broker.resolve_one_for(load_registry().capability("jira.read"), env_name)
    except broker.BrokerDenied:
        return default


class JiraError(RuntimeError):
    """Sanitized Jira client failure — message must never carry secret material."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class JiraMyself:
    account_id: str
    display_name: str
    email_address: str | None = None


@dataclass(frozen=True, slots=True)
class JiraProject:
    id: str
    key: str
    name: str
    project_type_key: str | None = None


@dataclass(frozen=True, slots=True)
class JiraStatus:
    id: str
    name: str
    status_category_key: str | None = None


@dataclass(frozen=True, slots=True)
class JiraIssue:
    id: str
    key: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JiraTransition:
    id: str
    name: str
    to_status_name: str | None = None


@dataclass(frozen=True, slots=True)
class JiraUser:
    account_id: str
    display_name: str
    email_address: str | None = None


def _default_fields_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "jira_fields.yaml"


def load_jira_fields_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load ``configs/jira_fields.yaml`` (live keys + logical field map).

    Production callers: project PATCH live-key invariant; field resolution.
    """
    target = Path(path) if path is not None else _default_fields_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise JiraError("jira_fields.yaml must be a mapping")
    return raw


def _resolve_custom_fields(
    project_key: str,
    logical_fields: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Map logical names → ``customfield_*`` payload for one project.

    Raises :class:`JiraError` if any key is a raw ``customfield_*`` id.
    """
    for key in logical_fields:
        if str(key).startswith(_CUSTOMFIELD_PREFIX):
            raise JiraError(f"raw custom field id refused at boundary: {key}")
    cfg = dict(config) if config is not None else load_jira_fields_config(config_path)
    fields_cfg = cfg.get("fields") or {}
    if not isinstance(fields_cfg, dict):
        raise JiraError("jira_fields.yaml fields must be a mapping")
    out: dict[str, Any] = {}
    project = str(project_key).strip().upper()
    for logical, value in logical_fields.items():
        name = str(logical).strip()
        entry = fields_cfg.get(name)
        if not isinstance(entry, dict):
            raise JiraError(f"unknown logical jira field: {name}")
        project_ids = entry.get("project_ids") or {}
        if not isinstance(project_ids, dict):
            raise JiraError(f"project_ids missing for logical field {name}")
        field_id = project_ids.get(project) or project_ids.get(project_key)
        if not field_id:
            raise JiraError(f"no field id for {name!r} in project {project}")
        # single_select values are sent as {"value": "..."}; pass-through dicts as-is.
        if isinstance(value, str):
            out[str(field_id)] = {"value": value}
        else:
            out[str(field_id)] = value
    return out


def _adf_paragraph(text: str) -> dict[str, Any]:
    """Build a minimal ADF document with one paragraph of plain text (C12)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}] if text else [],
            }
        ],
    }


def _error_detail(exc: Exception) -> str:
    """Fixed non-secret shape — never ``str(exc)`` on HTTPStatusError."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Jira API error (HTTP {exc.response.status_code})"
    if isinstance(exc, httpx.TimeoutException):
        return "Jira API error (timeout)"
    if isinstance(exc, httpx.RequestError):
        return f"Jira API error (request failed: {type(exc).__name__})"
    if isinstance(exc, JiraError):
        return str(exc)
    return f"Jira API error ({type(exc).__name__})"


def _sanitize(message: str, secrets: Sequence[str]) -> str:
    out = message
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


class JiraClient:
    """Typed httpx client for Jira Cloud REST v3 (internal use only)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
        fields_config_path: str | Path | None = None,
    ) -> None:
        self._email = email if email is not None else _credential("JIRA_EMAIL")
        self._api_token = api_token if api_token is not None else _credential("JIRA_API_TOKEN")
        raw_base = (
            base_url
            if base_url is not None
            else _credential("JIRA_BASE_URL", "https://example-team.atlassian.net")
        )
        self._site_base = raw_base.rstrip("/")
        # API root is always {site}/rest/api/3 — accept either form of JIRA_BASE_URL.
        if self._site_base.endswith("/rest/api/3"):
            self._api_base = self._site_base
            self._site_base = self._site_base[: -len("/rest/api/3")]
        else:
            self._api_base = f"{self._site_base}/rest/api/3"
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)
        self._sleep = sleep or time.sleep
        self._fields_config_path = fields_config_path
        self._secrets = tuple(
            s for s in (self._api_token, self._email, self._basic_auth_header()) if s
        )
        # Rate-limit state (spec §12): quota-wide pause + per-issue backoff.
        self._quota_pause_until: float = 0.0
        self._issue_backoff_until: dict[str, float] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> JiraClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _basic_auth_header(self) -> str:
        if not self._email or not self._api_token:
            return ""
        token = base64.b64encode(f"{self._email}:{self._api_token}".encode()).decode("ascii")
        return f"Basic {token}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        auth = self._basic_auth_header()
        if auth:
            headers["Authorization"] = auth
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/rest/api/3"):
            return f"{self._site_base}{path}"
        if path.startswith("/"):
            return f"{self._api_base}{path}" if not path.startswith("/rest/") else f"{self._site_base}{path}"
        return f"{self._api_base}/{path}"

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _sanitize(_error_detail(exc), self._secrets)
            raise JiraError(detail, status_code=response.status_code) from None

    def _await_rate_limit_pauses(self, path: str) -> None:
        """Honor client-wide quota pause and per-issue backoff before a request."""
        now = time.time()
        if self._quota_pause_until > now:
            self._sleep(self._quota_pause_until - now)
            now = time.time()
        issue_key = issue_key_from_path(path)
        if issue_key is not None:
            until = self._issue_backoff_until.get(issue_key, 0.0)
            if until > now:
                self._sleep(until - now)

    def _record_rate_limit(self, decision: RetryDecision, response: httpx.Response, path: str) -> None:
        """Update quota / per-issue pause state from a 429 response (spec §12)."""
        reason_h = response.headers.get("RateLimit-Reason") or response.headers.get(
            "X-RateLimit-Reason"
        )
        rl_class = decision.rate_limit_class
        if rl_class == "unknown":
            rl_class = classify_rate_limit_reason(reason_h)
        now = time.time()
        retry_after_raw = response.headers.get("Retry-After")
        try:
            retry_after = float(retry_after_raw) if retry_after_raw is not None else None
        except ValueError:
            retry_after = None
        reset_delay = parse_rate_limit_reset(
            response.headers.get("X-RateLimit-Reset"), now=now
        )
        if rl_class == "quota":
            pause = max(float(retry_after or 0.0), float(reset_delay or 0.0), decision.sleep_s)
            if pause <= 0:
                pause = 60.0
            self._quota_pause_until = max(self._quota_pause_until, now + pause)
        elif rl_class == "per_issue":
            issue_key = issue_key_from_path(path)
            if issue_key is not None:
                pause = max(float(retry_after or 0.0), decision.sleep_s, 1.0)
                prev = self._issue_backoff_until.get(issue_key, 0.0)
                self._issue_backoff_until[issue_key] = max(prev, now + pause)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        url = self._url(path)
        headers = self._headers()
        self._await_rate_limit_pauses(path)

        def send() -> httpx.Response:
            return self._client.request(
                method,
                url,
                params=dict(params) if params else None,
                json=dict(json_body) if json_body is not None else None,
                headers=headers,
            )

        def is_error(resp: httpx.Response) -> bool:
            return resp.status_code in {429, 500, 502, 503, 504}

        def status_of(resp: httpx.Response) -> int:
            return int(resp.status_code)

        def retry_after_of(resp: httpx.Response) -> str | None:
            return resp.headers.get("Retry-After")

        def rate_limit_reason_of(resp: httpx.Response) -> str | None:
            return resp.headers.get("RateLimit-Reason") or resp.headers.get(
                "X-RateLimit-Reason"
            )

        def rate_limit_reset_of(resp: httpx.Response) -> str | None:
            return resp.headers.get("X-RateLimit-Reset")

        def on_rate_limit(decision: RetryDecision, resp: httpx.Response) -> None:
            self._record_rate_limit(decision, resp, path)

        try:
            response = with_retries(
                send,
                method=method,
                url_or_path=path,
                sleep=self._sleep,
                status_of=status_of,
                retry_after_of=retry_after_of,
                is_error=is_error,
                rate_limit_reason_of=rate_limit_reason_of,
                rate_limit_reset_of=rate_limit_reset_of,
                on_rate_limit=on_rate_limit,
            )
        except httpx.HTTPError as exc:
            detail = _sanitize(_error_detail(exc), self._secrets)
            raise JiraError(detail) from None

        self._raise_for_status(response)
        return response

    # --- public read methods (internal callers only; no route passthrough) ---

    def myself(self) -> JiraMyself:
        data = self._request("GET", "/rest/api/3/myself").json()
        return JiraMyself(
            account_id=str(data.get("accountId") or ""),
            display_name=str(data.get("displayName") or ""),
            email_address=data.get("emailAddress"),
        )

    def list_projects(self, *, max_results: int = 50) -> list[JiraProject]:
        projects: list[JiraProject] = []
        start_at = 0
        while True:
            data = self._request(
                "GET",
                "/rest/api/3/project/search",
                params={"maxResults": max_results, "startAt": start_at, "status": "live"},
            ).json()
            for item in data.get("values") or []:
                projects.append(
                    JiraProject(
                        id=str(item.get("id") or ""),
                        key=str(item.get("key") or ""),
                        name=str(item.get("name") or ""),
                        project_type_key=item.get("projectTypeKey"),
                    )
                )
            if data.get("isLast") is True:
                break
            values = data.get("values") or []
            if not values:
                break
            start_at += len(values)
            total = data.get("total")
            if isinstance(total, int) and start_at >= total:
                break
        return projects

    def project_statuses(self, project_key: str) -> list[JiraStatus]:
        data = self._request("GET", f"/rest/api/3/project/{project_key}/statuses").json()
        seen: dict[str, JiraStatus] = {}
        # Response is grouped by issue type: [{name, id, statuses: [...]}, ...]
        groups = data if isinstance(data, list) else data.get("statuses") or []
        for group in groups:
            statuses = group.get("statuses") if isinstance(group, dict) else None
            iterable = statuses if isinstance(statuses, list) else [group]
            for status in iterable:
                if not isinstance(status, dict):
                    continue
                name = str(status.get("name") or "")
                if not name or name in seen:
                    continue
                category = status.get("statusCategory") or {}
                seen[name] = JiraStatus(
                    id=str(status.get("id") or ""),
                    name=name,
                    status_category_key=(
                        str(category.get("key")) if isinstance(category, dict) else None
                    ),
                )
        return list(seen.values())

    def get_issue(
        self,
        issue_id_or_key: str,
        *,
        fields: Sequence[str] | None = None,
    ) -> JiraIssue:
        field_list = list(fields) if fields is not None else list(DEFAULT_FIELDS)
        data = self._request(
            "GET",
            f"/rest/api/3/issue/{issue_id_or_key}",
            params={"fields": ",".join(field_list)},
        ).json()
        return JiraIssue(
            id=str(data.get("id") or ""),
            key=str(data.get("key") or ""),
            fields=dict(data.get("fields") or {}),
        )

    def bulk_fetch(
        self,
        issue_ids_or_keys: Sequence[str],
        *,
        fields: Sequence[str] | None = None,
    ) -> list[JiraIssue]:
        field_list = list(fields) if fields is not None else list(DEFAULT_FIELDS)
        data = self._request(
            "POST",
            "/rest/api/3/issue/bulkfetch",
            json_body={
                "issueIdsOrKeys": list(issue_ids_or_keys),
                "fields": field_list,
            },
        ).json()
        issues: list[JiraIssue] = []
        for item in data.get("issues") or []:
            issues.append(
                JiraIssue(
                    id=str(item.get("id") or ""),
                    key=str(item.get("key") or ""),
                    fields=dict(item.get("fields") or {}),
                )
            )
        return issues

    def search_jql(
        self,
        jql: str,
        *,
        fields: Sequence[str] | None = None,
        max_results: int = 50,
        max_pages: int = 100,
    ) -> list[JiraIssue]:
        """GET ``/rest/api/3/search/jql`` with nextPageToken pagination only.

        Read-lane search is GET so it fits ``jira.read`` methods: [GET] without
        touching the broker framework. Query params: jql, fields (comma-joined),
        maxResults, nextPageToken. POST search is a JG-5 / post-P3-BROKER hand-off.

        A page missing both ``nextPageToken`` and ``isLast`` is treated as terminal
        — the client must not invent ``total``/``startAt`` continuation.
        """
        field_list = list(fields) if fields is not None else list(DEFAULT_FIELDS)
        token: str | None = None
        issues: list[JiraIssue] = []
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": max_results,
                "fields": ",".join(field_list),
            }
            if token:
                params["nextPageToken"] = token
            data = self._request("GET", "/rest/api/3/search/jql", params=params).json()
            for item in data.get("issues") or []:
                issues.append(
                    JiraIssue(
                        id=str(item.get("id") or ""),
                        key=str(item.get("key") or ""),
                        fields=dict(item.get("fields") or {}),
                    )
                )
            if data.get("isLast") is True:
                break
            next_token = data.get("nextPageToken")
            if not next_token:
                # Missing continuation markers: stop. Do NOT fall back to total/startAt.
                break
            token = str(next_token)
        return issues

    def list_transitions(self, issue_id_or_key: str) -> list[JiraTransition]:
        data = self._request(
            "GET", f"/rest/api/3/issue/{issue_id_or_key}/transitions"
        ).json()
        out: list[JiraTransition] = []
        for item in data.get("transitions") or []:
            to = item.get("to") or {}
            out.append(
                JiraTransition(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    to_status_name=str(to.get("name") or "") if isinstance(to, dict) else None,
                )
            )
        return out

    def user_search(self, query: str, *, max_results: int = 20) -> list[JiraUser]:
        data = self._request(
            "GET",
            "/rest/api/3/user/search",
            params={"query": query, "maxResults": max_results},
        ).json()
        users: list[JiraUser] = []
        for item in data if isinstance(data, list) else []:
            users.append(
                JiraUser(
                    account_id=str(item.get("accountId") or ""),
                    display_name=str(item.get("displayName") or ""),
                    email_address=item.get("emailAddress"),
                )
            )
        return users

    def assignable_search(
        self,
        *,
        project: str | None = None,
        issue_key: str | None = None,
        query: str = "",
        max_results: int = 20,
    ) -> list[JiraUser]:
        params: dict[str, Any] = {"maxResults": max_results}
        if project:
            params["project"] = project
        if issue_key:
            params["issueKey"] = issue_key
        if query:
            params["query"] = query
        data = self._request(
            "GET",
            "/rest/api/3/user/assignable/search",
            params=params,
        ).json()
        users: list[JiraUser] = []
        for item in data if isinstance(data, list) else []:
            users.append(
                JiraUser(
                    account_id=str(item.get("accountId") or ""),
                    display_name=str(item.get("displayName") or ""),
                    email_address=item.get("emailAddress"),
                )
            )
        return users

    def resolve_custom_fields(
        self,
        project_key: str,
        logical_fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _resolve_custom_fields(
            project_key,
            logical_fields,
            config_path=self._fields_config_path,
        )

    def _put_issue_fields(
        self,
        issue_id_or_key: str,
        project_key: str,
        logical_fields: Mapping[str, Any],
    ) -> None:
        """Internal field PUT using logical names only (no public route).

        Exists so A1 resolution is exercised against a real request body.
        """
        fields = self.resolve_custom_fields(project_key, logical_fields)
        self._request(
            "PUT",
            f"/rest/api/3/issue/{issue_id_or_key}",
            json_body={"fields": fields},
        )


__all__ = [
    "DEFAULT_FIELDS",
    "JiraClient",
    "JiraError",
    "JiraIssue",
    "JiraMyself",
    "JiraProject",
    "JiraStatus",
    "JiraTransition",
    "JiraUser",
    "load_jira_fields_config",
]

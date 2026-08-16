"""Google Drive API v3 search — optional, failure-safe.

Google now BLOCKS Drive scopes for gcloud's default ADC client, so this uses the user's
OWN OAuth client (a one-time Desktop-app OAuth flow → a stored refresh token). It stays
import-safe and returns ``[]`` whenever the Google libraries or a token are missing, so
file search never depends on Drive being configured. (Even unconfigured, Drive files are
still searchable via the local catalog by name/metadata — this only adds cloud CONTENT
search.) See :func:`auth_instructions` for the one-time setup.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime
from pathlib import Path
from pprint import pprint
from typing import Any

from omniagentos.contracts import default_db_path

_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_REQUEST_TIMEOUT_SECONDS = 10.0

_SECRETS_DIR = Path(default_db_path()).resolve().parent / "secrets"
_TOKEN_PATH = _SECRETS_DIR / "gdrive_token.json"
_CLIENT_SECRET_PRIMARY = Path("~/.config/omni/gdrive_client_secret.json").expanduser()
_CLIENT_SECRET_ALT = _SECRETS_DIR / "gdrive_client_secret.json"

_MIME_KIND: dict[str, str] = {
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.presentation": "slides",
    "application/pdf": "pdf",
}


def _discovery() -> Any | None:
    """Load googleapiclient.discovery without making this module unimportable."""
    try:
        return importlib.import_module("googleapiclient.discovery")
    except Exception:  # optional dependency
        return None


def _find_client_secret() -> Path | None:
    for candidate in (_CLIENT_SECRET_PRIMARY, _CLIENT_SECRET_ALT):
        if candidate.is_file():
            return candidate
    return None


def _save_token(creds: Any) -> None:
    _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(_TOKEN_PATH, 0o600)


def _load_credentials() -> Any | None:
    """Stored OAuth user creds, refreshed if expired. ``None`` when unavailable/invalid."""
    if not _TOKEN_PATH.is_file():
        return None
    try:
        from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
    except Exception:
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), [_DRIVE_READONLY_SCOPE])
    except Exception:
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception:
            return None
    return creds if creds and creds.valid else None


def is_available() -> bool:
    """Whether the Google client is installed AND a Drive token is stored. Local check
    only — no network/OAuth — safe to call on every search request."""
    try:
        return _discovery() is not None and _TOKEN_PATH.is_file()
    except Exception:
        return False


def authenticate() -> str:
    """Run the one-time OAuth flow with the user's own client secret; store the token."""
    secret = _find_client_secret()
    if secret is None:
        return "No OAuth client secret found.\n\n" + auth_instructions()
    try:
        from google_auth_oauthlib.flow import (  # type: ignore[import-not-found, import-untyped]
            InstalledAppFlow,
        )
    except Exception:
        return "google-auth-oauthlib is not installed (uv pip install google-auth-oauthlib)."
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), [_DRIVE_READONLY_SCOPE])
        creds = flow.run_local_server(port=0)
        _save_token(creds)
    except Exception as exc:  # noqa: BLE001
        return f"Authentication failed: {exc}"
    return f"Authenticated — Drive search is now live. Token saved to {_TOKEN_PATH}."


def _escaped_query(query: str) -> str:
    return query.replace("\\", "\\\\").replace("'", "\\'")


def _kind_for_mime(mime_type: object) -> str:
    mime = mime_type if isinstance(mime_type, str) else ""
    if mime in _MIME_KIND:
        return _MIME_KIND[mime]
    if "/" in mime:
        return mime.split("/", 1)[1]
    return "file"


def _modified_date(modified_time: object) -> str | None:
    if not isinstance(modified_time, str):
        return None
    try:
        return datetime.fromisoformat(modified_time.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _size(value: object) -> int | None:
    try:
        return int(value) if isinstance(value, str | int | float) else None
    except (TypeError, ValueError):
        return None


def _set_request_timeout(service: Any) -> None:
    try:
        transport = service._http
        transport.timeout = _REQUEST_TIMEOUT_SECONDS
        underlying = getattr(transport, "http", None)
        if underlying is not None:
            underlying.timeout = _REQUEST_TIMEOUT_SECONDS
    except Exception:
        pass


def _normalise_file(file_data: dict[str, Any]) -> dict[str, object]:
    file_id = file_data.get("id", "")
    web_view_link = file_data.get("webViewLink")
    path = (
        web_view_link if isinstance(web_view_link, str) and web_view_link else f"gdrive:{file_id}"
    )
    return {
        "path": path,
        "name": file_data.get("name", ""),
        "source": "gdrive",
        "kind": _kind_for_mime(file_data.get("mimeType")),
        "size": _size(file_data.get("size")),
        "modified": _modified_date(file_data.get("modifiedTime")),
        "snippet": "",
        "score": 0.0,
    }


def search_drive(query: str, limit: int = 20) -> list[dict[str, object]]:
    """Search the user's Drive → SearchHit-shaped dicts. Any auth/API/network error is
    swallowed to ``[]`` — Drive is an optional source."""
    try:
        discovery = _discovery()
        creds = _load_credentials()
        if discovery is None or creds is None:
            return []
        service = discovery.build("drive", "v3", credentials=creds, cache_discovery=False)
        _set_request_timeout(service)
        escaped = _escaped_query(query)
        response = (
            service.files()
            .list(
                q=f"fullText contains '{escaped}' or name contains '{escaped}'",
                fields="files(id,name,mimeType,modifiedTime,size,webViewLink,parents)",
                pageSize=limit,
                corpora="user",
                spaces="drive",
                supportsAllDrives=False,
            )
            .execute(num_retries=0)
        )
        files = response.get("files", [])
        if not isinstance(files, list):
            return []
        return [_normalise_file(item) for item in files if isinstance(item, dict)]
    except Exception:
        return []


def auth_instructions() -> str:
    """One-time setup — Google blocks Drive on gcloud's default client, so use your own."""
    return (
        "Google Drive full-content search needs your OWN OAuth client (one-time; the old\n"
        "`gcloud auth application-default login` path is blocked by Google for Drive scopes):\n"
        "  1. console.cloud.google.com → pick/create a project.\n"
        "  2. Enable the Drive API:  gcloud services enable drive.googleapis.com\n"
        "  3. APIs & Services → OAuth consent screen → External → add your email as a Test user.\n"
        "  4. APIs & Services → Credentials → Create credentials → OAuth client ID →\n"
        "     Application type: Desktop app. Download the JSON.\n"
        f"  5. Save that JSON to:  {_CLIENT_SECRET_PRIMARY}\n"
        "  6. Run:  python -m omniagentos.filesearch.gdrive_api auth   (opens a browser to approve)\n\n"
        "Then Google Drive search auto-switches to the fast cloud API. NOTE: even without\n"
        "this, your Drive files are ALREADY searchable via the local catalog (by name/\n"
        "metadata) — this only adds full cloud CONTENT search."
    )


def _main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        print(authenticate())
        return
    available = is_available()
    print(f"Google Drive auth available: {available}")
    if available:
        query = sys.argv[1] if len(sys.argv) > 1 else "test"
        pprint(search_drive(query), sort_dicts=False)
    else:
        print(auth_instructions())


if __name__ == "__main__":
    _main()

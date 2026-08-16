import os
import shutil
import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parent / "fixtures/fleet-schema.sql"


@pytest.fixture
def real_fleet_db(tmp_path: Path) -> Path:
    destination = tmp_path / "fleet.sqlite"
    optional_copy = os.environ.get("FLEETCAP_REAL_DB_COPY")
    if optional_copy:
        shutil.copy2(Path(optional_copy), destination)
    else:
        connection = sqlite3.connect(destination)
        try:
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        finally:
            connection.close()
    return destination


@pytest.fixture
def real_connection(real_fleet_db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(real_fleet_db)
    try:
        yield connection
    finally:
        connection.close()

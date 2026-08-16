#!/usr/bin/env python3
"""Read-only zero-row writer probe.

Investigates tables with schema and writer code but zero rows:
  - repomap_tag_cache
  - gate_evidence
  - provider_call_usage
  - metacog_memory_records (specifically metacog_memory_records)

For each table, reports:
  - Row count
  - Writer location(s) by grep
  - Status: unreachable (no callers), gated off (conditional), or failing silently (try-except)

Strictly read-only: opens database with mode=ro, writes no rows, creates no schema.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class TableInfo:
    """Information about a single table."""

    name: str
    row_count: int
    writer_location: str
    writer_status: str  # 'unreachable', 'gated', 'failing_silently', 'unknown'
    details: str


class ZeroRowProbe:
    """Probe for zero-row tables with active writers."""

    TABLES = {
        "repomap_tag_cache": {
            "module": "omniagentos/repomap/service.py",
            "pattern": r"INSERT INTO repomap_tag_cache",
            "description": "Caches parsed file tags by content hash",
        },
        "gate_evidence": {
            "module": "omniagentos/gates/engine.py",
            "pattern": r"INSERT INTO gate_evidence",
            "description": "Logs gate execution details and evidence",
        },
        "provider_call_usage": {
            "module": "omniagentos/db/store.py",
            "pattern": r"INSERT INTO provider_call_usage",
            "description": "Records LLM provider API calls and costs",
        },
        "metacog_memory_records": {
            "module": "omniagentos/metacog/store.py",
            "pattern": r"INSERT INTO metacog_memory_records",
            "description": "Stores metacognitive memory records",
        },
    }

    def __init__(self, repo_root: str | Path, db_path: str | Path | None = None):
        self.repo_root = Path(repo_root)
        if db_path is None:
            db_path = self.repo_root / "var" / "runtime" / "state.sqlite3"
        self.db_path = Path(db_path)
        self.findings: list[TableInfo] = []

    def open_readonly(self) -> sqlite3.Connection:
        """Open database in read-only mode."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        # Use URI mode with ro flag to ensure read-only access
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def get_row_count(self, conn: sqlite3.Connection, table_name: str) -> int:
        """Get the number of rows in a table."""
        try:
            cursor = conn.execute(f"SELECT COUNT(*) as cnt FROM {table_name}")
            row = cursor.fetchone()
            return row["cnt"] if row else 0
        except sqlite3.OperationalError:
            return -1  # Table may not exist

    def find_writer_locations(self, table_name: str) -> list[str]:
        """Find all files that write to this table."""
        spec = self.TABLES[table_name]
        pattern = spec["pattern"]
        locations = []

        try:
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, "omniagentos/"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )

            for line in result.stdout.splitlines():
                if line.strip():
                    locations.append(line)

        except subprocess.TimeoutExpired:
            locations.append("TIMEOUT: grep search took too long")
        except Exception as e:
            locations.append(f"ERROR: {e}")

        return locations

    def analyze_writer_status(self, table_name: str, locations: list[str]) -> tuple[str, str]:
        """Analyze if writer is unreachable, gated, or failing silently.

        Returns: (status, details)
        """
        if not locations:
            return ("unreachable", "No INSERT statement found in codebase")

        spec = self.TABLES[table_name]
        module_path = self.repo_root / spec["module"]

        if not module_path.exists():
            return ("unknown", f"Module not found: {spec['module']}")

        # Read the module to analyze context
        try:
            with open(module_path) as f:
                content = f.read()
        except Exception as e:
            return ("unknown", f"Could not read module: {e}")

        # Extract lines around INSERT to detect try-except and conditionals
        lines = content.splitlines()

        status = "unknown"
        details = ""

        # Look for try-except wrapping the INSERT
        for i, line in enumerate(lines):
            if re.search(spec["pattern"], line):
                # Check if we're inside a try-except block
                if_depth = 0
                try_depth = 0

                # Look backwards for try statement
                for j in range(i - 1, max(0, i - 50), -1):
                    if re.search(r"^\s*try\s*:", lines[j]):
                        try_depth += 1
                    elif re.search(r"^\s*except\s", lines[j]):
                        if try_depth > 0:
                            status = "failing_silently"
                            details = (
                                f"INSERT wrapped in try-except at {spec['module']}:{j+1}-{i+1}"
                            )
                            break

                if try_depth > 0 and status != "failing_silently":
                    status = "failing_silently"
                    details = f"INSERT inside try block at {spec['module']}:{i+1}"

                # Check for conditional guards (if statements)
                for j in range(i - 1, max(0, i - 20), -1):
                    if re.search(r"^\s*if\s+", lines[j]) and not re.search(r"^\s*#", lines[j]):
                        if_depth += 1
                        status = "gated"
                        details = f"INSERT conditioned by 'if' at {spec['module']}:{j+1}"
                        break

                if status != "unknown":
                    break

        # Check if the function/class containing the INSERT is actually called
        if status == "unknown":
            # Look for the function definition
            func_match = None
            for j in range(i, -1, -1):
                if re.match(r"^\s*(def|class)\s+", lines[j]):
                    func_match = lines[j]
                    break

            if func_match:
                # Extract function name
                func_name = re.search(r"(def|class)\s+(\w+)", func_match)
                if func_name:
                    fname = func_name.group(2)
                    # Search for callers of this function
                    result = subprocess.run(
                        ["grep", "-r", "-c", fname, "omniagentos/", "tests/"],
                        cwd=self.repo_root,
                        capture_output=True,
                        text=True,
                    )
                    caller_count = len([line for line in result.stdout.splitlines() if line.strip()])
                    if caller_count <= 1:
                        status = "unreachable"
                        details = f"Function {fname} has no external callers"
                    else:
                        status = "active"
                        details = f"Function {fname} called from multiple locations"

        return (status, details)

    def run(self) -> dict:
        """Run the probe and return findings."""
        try:
            conn = self.open_readonly()
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "findings": [],
            }

        try:
            for table_name in self.TABLES:
                row_count = self.get_row_count(conn, table_name)
                locations = self.find_writer_locations(table_name)
                writer_status, details = self.analyze_writer_status(table_name, locations)

                info = TableInfo(
                    name=table_name,
                    row_count=row_count,
                    writer_location="; ".join(locations[:3]) if locations else "NOT FOUND",
                    writer_status=writer_status,
                    details=details,
                )
                self.findings.append(info)

        finally:
            conn.close()

        return {
            "status": "success",
            "findings": [asdict(f) for f in self.findings],
        }


def main():
    """Entry point for the probe."""

    # Determine repo root from script location
    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent.parent

    probe = ZeroRowProbe(repo_root)
    result = probe.run()

    # Pretty-print results
    print("=" * 80)
    print("ZERO-ROW WRITER PROBE FINDINGS")
    print("=" * 80)

    if result["status"] == "error":
        print(f"ERROR: {result['error']}")
        return 1

    for finding in result["findings"]:
        print(f"\nTable: {finding['name']}")
        print(f"  Row count: {finding['row_count']}")
        print(f"  Writer status: {finding['writer_status']}")
        print(f"  Details: {finding['details']}")
        print(f"  Locations: {finding['writer_location']}")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    zero_row_tables = [f for f in result["findings"] if f["row_count"] == 0]
    print(f"Total tables: {len(result['findings'])}")
    print(f"Zero-row tables: {len(zero_row_tables)}")

    for table in zero_row_tables:
        print(f"  - {table['name']}: {table['writer_status']} ({table['details']})")

    # Write JSON output
    output_path = repo_root / "var" / "zerorow-probe-report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nJSON report written to: {output_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

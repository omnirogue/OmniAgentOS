"""Tests for scripts/ops/topology_capture.py — read-only live-topology capture."""

from __future__ import annotations

import json
import plistlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts to path so we can import topology_capture
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "ops"))

from topology_capture import (
    _format_markdown,
    _get_listening_ports,
    _parse_launchctl_list,
    _resolve_db_path,
    _scan_plists,
    capture_topology,
)


class TestParselaunchctlList:
    """Test launchctl list parsing."""

    def test_parse_valid_output(self) -> None:
        """Parse valid launchctl list output with omniagentos labels."""
        output = """PID	Status	Label
123	0	com.omniagentos.routines
-	1	com.omniagentos.banking
456	-	com.omniagentos.api
789	0	com.other.service
"""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout=output)
        )

        result = _parse_launchctl_list(runner=mock_run)

        assert len(result) == 3
        assert result[0].label == "com.omniagentos.routines"
        assert result[0].pid == 123
        assert result[0].last_exit_status == 0
        assert result[1].label == "com.omniagentos.banking"
        assert result[1].pid is None
        assert result[1].last_exit_status == 1
        assert result[2].label == "com.omniagentos.api"
        assert result[2].pid == 456
        assert result[2].last_exit_status is None

    def test_parse_filters_non_omniagentos(self) -> None:
        """Only omniagentos/omniagentos labels are included."""
        output = """PID	Status	Label
123	0	com.omniagentos.routines
789	0	com.apple.some.service
"""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout=output)
        )

        result = _parse_launchctl_list(runner=mock_run)

        assert len(result) == 1
        assert result[0].label == "com.omniagentos.routines"

    def test_parse_handles_failure(self) -> None:
        """Handle launchctl command failure gracefully."""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=1, stdout="")
        )

        result = _parse_launchctl_list(runner=mock_run)

        assert result == []

    def test_parse_handles_exception(self) -> None:
        """Handle subprocess exceptions gracefully."""
        mock_run = MagicMock(side_effect=Exception("timeout"))

        result = _parse_launchctl_list(runner=mock_run)

        assert result == []


class TestScanPlists:
    """Test plist scanning and parsing."""

    def test_scan_valid_plist(self) -> None:
        """Scan and parse a valid plist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_dir = Path(tmpdir)

            # Create a test plist
            plist_data = {
                "Label": "com.omniagentos.test",
                "Program": "/usr/local/bin/test",
                "EnvironmentVariables": {
                    "VAR1": "value1",
                    "VAR2": "value2",
                },
            }
            plist_path = plist_dir / "com.omniagentos.test.plist"
            with open(plist_path, "wb") as f:
                plistlib.dump(plist_data, f)

            result = _scan_plists(plist_dir)

            assert len(result) == 1
            assert result[0].label == "com.omniagentos.test"
            assert result[0].program == "/usr/local/bin/test"
            assert result[0].environment_variables == {"VAR1": "value1", "VAR2": "value2"}

    def test_scan_filters_non_omniagentos(self) -> None:
        """Only omniagentos/omniagentos plists are included."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_dir = Path(tmpdir)

            # Create two plists
            for name, label in [
                ("com.omniagentos.test.plist", "com.omniagentos.test"),
                ("com.apple.test.plist", "com.apple.test"),
            ]:
                plist_data = {"Label": label}
                (plist_dir / name).write_bytes(plistlib.dumps(plist_data))

            result = _scan_plists(plist_dir)

            assert len(result) == 1
            assert result[0].label == "com.omniagentos.test"

    def test_scan_skips_malformed_plist(self) -> None:
        """Malformed plists are skipped without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_dir = Path(tmpdir)

            # Write invalid plist data
            (plist_dir / "com.omniagentos.bad.plist").write_bytes(b"not a plist")

            result = _scan_plists(plist_dir)

            assert result == []

    def test_scan_handles_missing_directory(self) -> None:
        """Missing directory is handled gracefully."""
        result = _scan_plists(Path("/nonexistent/path"))

        assert result == []

    def test_scan_preserves_plist_path(self) -> None:
        """Plist file path is preserved in the entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_dir = Path(tmpdir)
            plist_path = plist_dir / "com.omniagentos.test.plist"
            plist_data = {"Label": "com.omniagentos.test"}
            plist_path.write_bytes(plistlib.dumps(plist_data))

            result = _scan_plists(plist_dir)

            assert result[0].path == str(plist_path)


class TestGetListeningPorts:
    """Test listening port detection."""

    def test_parse_ipv4_ports(self) -> None:
        """Parse IPv4 listening ports."""
        output = """COMMAND     PID USER   FD TYPE             DEVICE SIZE/OFF NODE NAME
python   12345 user    3 IPv4 0x123abc456 0t0    TCP 127.0.0.1:8485 (LISTEN)
node     12346 user    5 IPv4 0x789def012 0t0    TCP *:3003 (LISTEN)
"""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout=output)
        )

        result = _get_listening_ports(runner=mock_run)

        assert len(result) == 2
        assert result[0].local_address == "127.0.0.1"
        assert result[0].local_port == 8485
        assert result[0].pid == 12345
        assert result[0].command == "python"
        assert result[1].local_address == "*"
        assert result[1].local_port == 3003
        assert result[1].pid == 12346

    def test_parse_ipv6_ports(self) -> None:
        """Parse IPv6 listening ports."""
        output = """COMMAND     PID USER   FD TYPE             DEVICE SIZE/OFF NODE NAME
python   12345 user    3 IPv6 0x123abc456 0t0    TCP [::1]:8485 (LISTEN)
"""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout=output)
        )

        result = _get_listening_ports(runner=mock_run)

        assert len(result) == 1
        assert result[0].local_address == "::1"
        assert result[0].local_port == 8485

    def test_parse_handles_failure(self) -> None:
        """Handle lsof command failure gracefully."""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=1, stdout="")
        )

        result = _get_listening_ports(runner=mock_run)

        assert result == []

    def test_parse_handles_exception(self) -> None:
        """Handle subprocess exceptions gracefully."""
        mock_run = MagicMock(side_effect=Exception("timeout"))

        result = _get_listening_ports(runner=mock_run)

        assert result == []


class TestResolveDbPath:
    """Test OMNIAGENTOS_DB path resolution."""

    def test_resolve_from_environment(self) -> None:
        """Use OMNIAGENTOS_DB from environment if set."""
        with patch.dict("os.environ", {"OMNIAGENTOS_DB": "/custom/db/path"}):
            result = _resolve_db_path()
            assert result == "/custom/db/path"

    def test_resolve_fallback(self) -> None:
        """Fall back to default path if env var not set."""
        with patch.dict("os.environ", {}, clear=True):
            # Mock subprocess.run to fail (simulating no launch-env.sh)
            mock_run = MagicMock(
                return_value=MagicMock(returncode=1, stdout="")
            )
            result = _resolve_db_path(runner=mock_run)
            assert "state.sqlite3" in result


class TestFormatMarkdown:
    """Test markdown formatting."""

    def test_format_includes_all_sections(self) -> None:
        """Markdown output includes all required sections."""
        data = {
            "timestamp": "2026-08-03T12:00:00+00:00",
            "omniagentos_db": "/path/to/db.sqlite3",
            "launchctl": [
                {
                    "label": "com.omniagentos.test",
                    "pid": 123,
                    "last_exit_status": 0,
                }
            ],
            "plists": [
                {
                    "label": "com.omniagentos.test",
                    "path": "/path/to/test.plist",
                    "program": "/usr/bin/test",
                    "environment_variables": {"VAR": "value"},
                }
            ],
            "listening_ports": [
                {
                    "protocol": "TCP",
                    "local_address": "127.0.0.1",
                    "local_port": 8485,
                    "pid": 456,
                    "command": "python",
                }
            ],
        }

        result = _format_markdown(data)

        assert "# Live System Topology Snapshot" in result
        assert "OMNIAGENTOS_DB" in result
        assert "com.omniagentos.test" in result
        assert "127.0.0.1" in result
        assert "8485" in result
        assert "Launchd Jobs" in result
        assert "Installed Plists" in result
        assert "Listening TCP Ports" in result

    def test_format_handles_empty_data(self) -> None:
        """Markdown output handles empty data gracefully."""
        data = {
            "timestamp": "2026-08-03T12:00:00+00:00",
            "omniagentos_db": None,
            "launchctl": [],
            "plists": [],
            "listening_ports": [],
        }

        result = _format_markdown(data)

        assert "No omniagentos" in result
        assert "No listening TCP ports" in result


class TestCaptureTopology:
    """Test full topology capture."""

    def test_capture_structure(self) -> None:
        """Captured data has correct structure."""
        with patch("topology_capture._parse_launchctl_list", return_value=[]):
            with patch("topology_capture._scan_plists", return_value=[]):
                with patch("topology_capture._get_listening_ports", return_value=[]):
                    with patch("topology_capture._resolve_db_path", return_value="/db"):
                        data = capture_topology()

        assert "timestamp" in data
        assert "launchctl" in data
        assert "plists" in data
        assert "listening_ports" in data
        assert "omniagentos_db" in data
        assert isinstance(data["timestamp"], str)
        assert isinstance(data["launchctl"], list)
        assert isinstance(data["plists"], list)
        assert isinstance(data["listening_ports"], list)
        assert data["omniagentos_db"] == "/db"

    def test_capture_is_json_serializable(self) -> None:
        """Captured data is JSON-serializable (no mutations)."""
        with patch("topology_capture._parse_launchctl_list", return_value=[]):
            with patch("topology_capture._scan_plists", return_value=[]):
                with patch("topology_capture._get_listening_ports", return_value=[]):
                    with patch("topology_capture._resolve_db_path", return_value="/db"):
                        data = capture_topology()

        # Should not raise
        json_str = json.dumps(data)
        assert json_str
        assert isinstance(json_str, str)


class TestReadOnlyContract:
    """Verify read-only contract: no mutations to system state."""

    def test_launchctl_list_no_mutations(self) -> None:
        """launchctl list parsing makes no system changes."""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="PID\tStatus\tLabel\n")
        )
        _parse_launchctl_list(runner=mock_run)

        # Verify launchctl was called with read-only flags (no load/unload)
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert call_args == ["launchctl", "list"]

    def test_plist_scan_no_mutations(self) -> None:
        """Plist scanning makes no filesystem changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plist_dir = Path(tmpdir)
            plist_data = {"Label": "com.omniagentos.test"}
            (plist_dir / "com.omniagentos.test.plist").write_bytes(
                plistlib.dumps(plist_data)
            )

            original_mtime = (plist_dir / "com.omniagentos.test.plist").stat().st_mtime

            _scan_plists(plist_dir)

            # Plist should not be modified
            new_mtime = (plist_dir / "com.omniagentos.test.plist").stat().st_mtime
            assert original_mtime == new_mtime

    def test_ports_query_no_mutations(self) -> None:
        """Port querying makes no system changes."""
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="COMMAND\tPID\tUSER\tFD\tTYPE\tDEVICE\tSIZE/OFF\tNODE\tNAME\n")
        )
        _get_listening_ports(runner=mock_run)

        # Verify lsof was called with read-only flags
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert call_args == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]

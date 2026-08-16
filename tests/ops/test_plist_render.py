"""Tests for fail-closed plist rendering.

Validates that:
  1. plist_render.render() raises if any placeholder is unresolved.
  2. plist_render.render() raises if required keys are missing.
  3. Rendered plists preserve semantic content matching installed units.
  4. EnvironmentVariables from installed plists are not dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lib.plist_render import (
    PlistRenderError,
    check_parity_with_installed,
    extract_environment_variables,
    render,
)


class TestRender:
    """Tests for the centralized render() function."""

    def test_render_basic_replacement(self) -> None:
        """render() correctly substitutes all placeholders."""
        template = "<string>{{LABEL}}</string>"
        replacements = {"{{LABEL}}": "com.test.app"}
        result = render(template, replacements)
        assert result == "<string>com.test.app</string>"

    def test_render_multiple_replacements(self) -> None:
        """render() handles multiple distinct placeholders."""
        template = """
        <key>Label</key><string>{{LABEL}}</string>
        <key>WorkingDirectory</key><string>{{WORKING_DIR}}</string>
        """
        replacements = {
            "{{LABEL}}": "com.example.job",
            "{{WORKING_DIR}}": "/home/user",
        }
        result = render(template, replacements)
        assert "com.example.job" in result
        assert "/home/user" in result
        assert "{{" not in result

    def test_render_raises_on_unresolved_placeholder(self) -> None:
        """render() raises PlistRenderError if any placeholder remains."""
        template = "<string>{{LABEL}}</string><string>{{MISSING}}</string>"
        replacements = {"{{LABEL}}": "com.test.app"}
        with pytest.raises(PlistRenderError) as exc_info:
            render(template, replacements)
        assert "{{MISSING}}" in str(exc_info.value) or "unresolved" in str(exc_info.value).lower()

    def test_render_raises_with_helpful_error(self) -> None:
        """render() error message includes provided keys for debugging."""
        template = "{{MISSING_KEY}}"
        replacements = {"{{KNOWN_KEY}}": "value"}
        with pytest.raises(PlistRenderError) as exc_info:
            render(template, replacements)
        error_msg = str(exc_info.value)
        assert "KNOWN_KEY" in error_msg or "{{" in error_msg

    def test_render_empty_template(self) -> None:
        """render() handles empty templates gracefully."""
        result = render("", {})
        assert result == ""

    def test_render_no_placeholders(self) -> None:
        """render() returns unchanged template if no placeholders present."""
        template = "<plist><dict></dict></plist>"
        result = render(template, {})
        assert result == template

    def test_render_repeated_placeholders(self) -> None:
        """render() replaces all occurrences of a placeholder."""
        template = "{{VAL}} and {{VAL}} again"
        replacements = {"{{VAL}}": "replaced"}
        result = render(template, replacements)
        assert result == "replaced and replaced again"

    def test_render_whitespace_in_replacement(self) -> None:
        """render() preserves whitespace in replacement values."""
        template = "<string>{{PATH}}</string>"
        replacements = {"{{PATH}}": "/path with spaces/file.txt"}
        result = render(template, replacements)
        assert "/path with spaces/file.txt" in result

    def test_render_xml_special_chars_escaped(self) -> None:
        """render() accepts pre-escaped XML values from callers."""
        # Note: callers are responsible for escaping, using html.escape()
        template = "<string>{{LABEL}}</string>"
        replacements = {"{{LABEL}}": "com.test&amp;app"}
        result = render(template, replacements)
        assert "com.test&amp;app" in result


class TestExtractEnvironmentVariables:
    """Tests for extracting EnvironmentVariables from installed plists."""

    def test_extract_from_valid_plist(self, tmp_path: Path) -> None:
        """extract_environment_variables() reads an installed plist."""
        plist_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.test.app</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OMNIAGENTOS_DB</key>
        <string>/path/to/db.sqlite3</string>
        <key>OMNIAGENTOS_VAR_DIR</key>
        <string>/path/to/var</string>
    </dict>
</dict>
</plist>
"""
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(plist_content)

        env_vars = extract_environment_variables(plist_file)
        assert env_vars is not None
        assert env_vars["OMNIAGENTOS_DB"] == "/path/to/db.sqlite3"
        assert env_vars["OMNIAGENTOS_VAR_DIR"] == "/path/to/var"

    def test_extract_returns_none_for_no_env_vars(self, tmp_path: Path) -> None:
        """extract_environment_variables() returns None if no EnvironmentVariables key."""
        plist_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""
        plist_file = tmp_path / "test.plist"
        plist_file.write_bytes(plist_content)

        env_vars = extract_environment_variables(plist_file)
        assert env_vars is None

    def test_extract_raises_on_malformed_plist(self, tmp_path: Path) -> None:
        """extract_environment_variables() raises on invalid XML."""
        plist_file = tmp_path / "bad.plist"
        plist_file.write_text("<plist><dict>BROKEN")

        with pytest.raises(PlistRenderError):
            extract_environment_variables(plist_file)


class TestCheckParityWithInstalled:
    """Tests for byte-parity validation between rendered and installed plists."""

    def test_parity_check_passes_when_no_installed_plist(self, tmp_path: Path) -> None:
        """check_parity_with_installed() passes if no installed plist exists."""
        rendered = "<plist><dict></dict></plist>"
        matches, msg = check_parity_with_installed(
            "com.test.nonexistent",
            rendered,
            installed_plist_dir=tmp_path,  # Empty directory
        )
        assert matches is True
        assert "skipping" in msg.lower() or "no installed" in msg.lower()

    def test_parity_check_detects_missing_environment_variables(
        self, tmp_path: Path
    ) -> None:
        """check_parity_with_installed() fails if required EnvironmentVariables are missing."""
        # Create an installed plist with EnvironmentVariables
        installed_plist = tmp_path / "com.test.app.plist"
        installed_plist.write_bytes(
            b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OMNIAGENTOS_DB</key>
        <string>/path/to/db.sqlite3</string>
    </dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""
        )

        # Rendered plist has no EnvironmentVariables
        rendered = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""

        matches, msg = check_parity_with_installed(
            "com.test.app",
            rendered,
            installed_plist_dir=tmp_path,
        )
        assert matches is False
        assert "OMNIAGENTOS_DB" in msg or "missing" in msg.lower()

    def test_parity_check_passes_with_matching_environment_variables(
        self, tmp_path: Path
    ) -> None:
        """check_parity_with_installed() passes if EnvironmentVariables match."""
        # Create an installed plist
        installed_plist = tmp_path / "com.test.app.plist"
        installed_plist.write_bytes(
            b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OMNIAGENTOS_DB</key>
        <string>/path/to/db.sqlite3</string>
    </dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""
        )

        # Rendered plist with matching EnvironmentVariables
        rendered = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OMNIAGENTOS_DB</key>
        <string>/path/to/db.sqlite3</string>
    </dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""

        matches, msg = check_parity_with_installed(
            "com.test.app",
            rendered,
            installed_plist_dir=tmp_path,
        )
        assert matches is True
        assert "parity" in msg.lower() or "match" in msg.lower()

    def test_parity_check_fails_on_malformed_rendered_plist(
        self, tmp_path: Path
    ) -> None:
        """check_parity_with_installed() fails if rendered XML is malformed."""
        installed_plist = tmp_path / "com.test.app.plist"
        installed_plist.write_bytes(
            b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.test.app</string>
</dict>
</plist>
"""
        )

        rendered = "<plist><dict>BROKEN"

        matches, msg = check_parity_with_installed(
            "com.test.app",
            rendered,
            installed_plist_dir=tmp_path,
        )
        assert matches is False
        assert "parse" in msg.lower() or "xml" in msg.lower()


class TestIntegrationWithRenderers:
    """Integration tests using actual launchd module renderers."""

    def test_scheduler_renderer_uses_centralized_render(self) -> None:
        """scheduler.launchd.render_template() uses centralized render()."""
        from scripts.scheduler.launchd import render_template

        template = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>{{LABEL}}</string>
    <key>ProgramArguments</key>{{PROGRAM_ARGS}}
    <key>WorkingDirectory</key><string>{{WORKING_DIR}}</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>{{HOUR}}</integer><key>Minute</key><integer>{{MINUTE}}</integer></dict>
</dict>
</plist>
"""
        result = render_template(
            template,
            label="com.test.scheduler",
            program_args=["/bin/bash", "-c", "echo test"],
            working_dir="/tmp",
            hour=9,
            minute=0,
        )
        assert "{{" not in result
        assert "com.test.scheduler" in result
        assert "9" in result
        assert "0" in result

    def test_scheduler_renderer_raises_on_missing_placeholder(self) -> None:
        """scheduler.launchd.render_template() raises if placeholder is missing."""
        from scripts.scheduler.launchd import render_template

        # Provide a broken template with an unresolved placeholder
        broken_template = "<string>{{LABEL}}</string><string>{{MISSING}}</string>"
        with pytest.raises(PlistRenderError):
            render_template(
                broken_template,
                label="com.test",
                program_args=[],
                working_dir="/tmp",
                hour=9,
                minute=0,
            )

    def test_gates_renderer_uses_centralized_render(self) -> None:
        """gates.launchd.render_template() uses centralized render()."""
        from scripts.gates.launchd import render_template

        template = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>{{LABEL}}</string>
    <key>ProgramArguments</key>{{PROGRAM_ARGS}}
    <key>WorkingDirectory</key><string>{{WORKING_DIR}}</string>
    <key>StartInterval</key><integer>{{INTERVAL}}</integer>
</dict>
</plist>
"""
        result = render_template(
            template,
            label="com.test.gate",
            program_args=["/bin/bash"],
            working_dir="/tmp",
            interval=300,
        )
        assert "{{" not in result
        assert "com.test.gate" in result
        assert "300" in result

    def test_curator_renderer_uses_centralized_render(self) -> None:
        """curator.launchd.render_template() uses centralized render()."""
        from scripts.curator.launchd import render_template

        template = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>{{LABEL}}</string>
    <key>ProgramArguments</key>{{PROGRAM_ARGS}}
    <key>WorkingDirectory</key><string>{{WORKING_DIR}}</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>{{HOUR1}}</integer><key>Minute</key><integer>{{MINUTE1}}</integer></dict>
        <dict><key>Hour</key><integer>{{HOUR2}}</integer><key>Minute</key><integer>{{MINUTE2}}</integer></dict>
    </array>
</dict>
</plist>
"""
        result = render_template(
            template,
            label="com.test.curator",
            program_args=["/bin/bash"],
            working_dir="/tmp",
            hour1=9,
            minute1=0,
            hour2=17,
            minute2=0,
        )
        assert "{{" not in result
        assert "com.test.curator" in result
        assert "9" in result
        assert "17" in result

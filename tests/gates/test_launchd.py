from typing import cast

import pytest

from scripts.gates.launchd import render_template


def test_render_template_rejects_non_integer_interval_before_xml_injection() -> None:
    malicious_interval = cast(int, "60</integer><key>Injected</key><string>pwned")

    with pytest.raises(ValueError, match="interval must be a positive integer"):
        render_template(
            "<plist><dict><key>StartInterval</key><integer>{{INTERVAL}}</integer></dict></plist>",
            label="com.example.gate",
            program_args=["/bin/true"],
            working_dir="/tmp/worktree",
            interval=malicious_interval,
        )

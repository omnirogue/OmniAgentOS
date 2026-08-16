"""Fail-closed plist template rendering with validation.

This module provides centralized, validated plist rendering for all launchd
templates in the scripts tree. Key safeguards:
  - Raises if any template placeholder '{{' survives after rendering
  - Raises if a required key is missing from replacements
  - Validates EnvironmentVariables are preserved in installed units
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class PlistRenderError(Exception):
    """Raised when plist rendering fails validation."""


def render(template: str, replacements: dict[str, str]) -> str:
    """Render a plist template with fail-closed validation.

    Args:
        template: The plist template string containing {{PLACEHOLDER}} tokens.
        replacements: Dict of placeholder -> value mappings.

    Returns:
        The rendered plist XML string with all placeholders replaced.

    Raises:
        PlistRenderError: If any '{{' remains after replacement (missing key)
                         or if a template placeholder key is not in replacements.
    """
    # Validate all known placeholders are in replacements
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    # Check for any remaining unrendered placeholders
    if "{{" in rendered:
        # Find the problematic placeholder for a better error message
        start = rendered.find("{{")
        end = rendered.find("}}", start)
        if end == -1:
            malformed_placeholder = rendered[start : start + 50]
        else:
            malformed_placeholder = rendered[start : end + 2]
        raise PlistRenderError(
            f"Incomplete template rendering: unresolved placeholder {malformed_placeholder!r}. "
            f"Provided keys: {set(replacements.keys())}. "
            f"This typically means a required parameter was not passed."
        )

    return rendered


def extract_environment_variables(plist_path: Path) -> dict[str, str] | None:
    """Extract EnvironmentVariables dict from an installed plist file.

    Args:
        plist_path: Path to a .plist file.

    Returns:
        Dict of environment variable names to values, or None if the plist
        has no EnvironmentVariables key.

    Raises:
        PlistRenderError: If the plist cannot be parsed.
    """
    try:
        tree = ET.parse(str(plist_path))
        root = tree.getroot()

        # Navigate: plist -> dict -> key (EnvironmentVariables)
        if root.tag != "plist":
            raise PlistRenderError(f"Invalid plist root tag: {root.tag}")

        plist_dict = root.find("dict")
        if plist_dict is None:
            return None

        # Find the EnvironmentVariables key
        keys = plist_dict.findall("key")
        for i, key_elem in enumerate(keys):
            if key_elem.text == "EnvironmentVariables":
                # Next sibling should be a dict
                # In ElementTree, we need to iterate and find by index
                dict_elems = [e for e in plist_dict if e.tag == "dict"]
                if i // 2 < len(dict_elems):
                    env_dict_elem = dict_elems[i // 2]
                    return _parse_plist_dict(env_dict_elem)
        return None
    except ET.ParseError as e:
        raise PlistRenderError(f"Failed to parse plist {plist_path}: {e}") from e


def _parse_plist_dict(dict_elem: Any) -> dict[str, str]:
    """Parse a <dict> element from a plist into a Python dict.

    Args:
        dict_elem: An ElementTree Element with tag 'dict'.

    Returns:
        A dict mapping string keys to string values.
    """
    result = {}
    keys = dict_elem.findall("key")
    for key_elem in keys:
        key_text = key_elem.text
        if key_text is None:
            continue
        # Find the next sibling that is a value (string, int, etc.)
        # In this simplified version, we only support <string> values
        key_index = list(dict_elem).index(key_elem)
        if key_index + 1 < len(dict_elem):
            value_elem = dict_elem[key_index + 1]
            if value_elem.tag == "string":
                result[key_text] = value_elem.text or ""
    return result


def check_parity_with_installed(
    label: str,
    rendered_plist_text: str,
    installed_plist_dir: Path | None = None,
) -> tuple[bool, str]:
    """Verify rendered plist output matches semantic content of installed plist.

    This is specifically designed to check that critical EnvironmentVariables
    (OMNIAGENTOS_DB, OMNIAGENTOS_VAR_DIR, OMNIAGENTOS_LEDGER_DIR,
    OMNIAGENTOS_VAULT_DIR) are present in the installed plist and would be
    preserved after rendering.

    Args:
        label: The plist Label (e.g., 'com.omniagentos.morning').
        rendered_plist_text: The output of render().
        installed_plist_dir: Path to ~/Library/LaunchAgents. If None, defaults
                            to the standard macOS location.

    Returns:
        Tuple of (matches: bool, message: str). matches=True if the rendered
        output has parity with the installed plist, or if no installed plist
        exists. message describes any deltas.
    """
    if installed_plist_dir is None:
        installed_plist_dir = Path.home() / "Library" / "LaunchAgents"

    plist_filename = f"{label}.plist"
    installed_path = installed_plist_dir / plist_filename

    if not installed_path.exists():
        return True, f"No installed plist at {installed_path}, skipping parity check"

    # ALWAYS validate that the rendered plist is valid XML, even if the
    # installed plist has no EnvironmentVariables. This catches malformed
    # renders before we check for EnvironmentVariables parity.
    try:
        root = ET.fromstring(rendered_plist_text)
        plist_dict = root.find("dict")
        if plist_dict is None:
            return False, "Rendered plist has no dict element"
    except ET.ParseError as e:
        return False, f"Could not parse rendered plist XML: {e}"

    # Now extract EnvironmentVariables from both plists
    try:
        installed_env_vars = extract_environment_variables(installed_path)
    except PlistRenderError as e:
        return False, f"Could not extract environment variables from installed plist: {e}"

    # If the installed plist has no EnvironmentVariables, we don't need to
    # compare them, but the rendered XML must still be valid (which we
    # checked above).
    if installed_env_vars is None:
        return True, "Installed plist has no EnvironmentVariables, skipping comparison"

    # Extract EnvironmentVariables from rendered plist
    rendered_env_vars = None
    keys = plist_dict.findall("key")
    for i, key_elem in enumerate(keys):
        if key_elem.text == "EnvironmentVariables":
            dict_elems = [e for e in plist_dict if e.tag == "dict"]
            if i // 2 < len(dict_elems):
                rendered_env_vars = _parse_plist_dict(dict_elems[i // 2])
            break

    if rendered_env_vars is None:
        # Rendered plist doesn't have EnvironmentVariables
        # Check if the installed one requires them
        required_keys = {
            "OMNIAGENTOS_DB",
            "OMNIAGENTOS_VAR_DIR",
            "OMNIAGENTOS_LEDGER_DIR",
            "OMNIAGENTOS_VAULT_DIR",
        }
        installed_keys = set(installed_env_vars.keys())
        missing_in_rendered = required_keys & installed_keys
        if missing_in_rendered:
            return (
                False,
                f"Rendered plist missing EnvironmentVariables that are in installed plist: "
                f"{missing_in_rendered}. This would break the service.",
            )
        return True, "Rendered plist has no EnvironmentVariables (acceptable if not required)"

    # Compare: check that all required keys from installed are in rendered
    required_keys = {
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
    }
    installed_required = required_keys & set(installed_env_vars.keys())
    missing_in_rendered = installed_required - set(rendered_env_vars.keys())

    if missing_in_rendered:
        return (
            False,
            f"Rendered plist missing critical EnvironmentVariables: {missing_in_rendered}. "
            f"This would cause API 500 errors.",
        )

    # Optionally check values match
    for key in installed_required:
        if (
            key in installed_env_vars
            and key in rendered_env_vars
            and installed_env_vars[key] != rendered_env_vars[key]
        ):
            return (
                False,
                f"EnvironmentVariable {key} value mismatch: "
                f"installed={installed_env_vars[key]}, "
                f"rendered={rendered_env_vars[key]}",
            )

    return True, "Rendered plist has parity with installed plist"

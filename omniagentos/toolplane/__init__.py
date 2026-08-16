"""The scoped CLI doorway through which agents may use local capabilities."""

from __future__ import annotations

from .manifest import CapabilityManifest, ManifestValidationError, load_manifest
from .tools import TOOLS, dispatch

__all__ = ["CapabilityManifest", "ManifestValidationError", "TOOLS", "dispatch", "load_manifest"]

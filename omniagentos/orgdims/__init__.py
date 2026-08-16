"""Multidimensional organization + classification (OmniAgentOS).

Canonical hierarchy: Company → Product → Initiative → Epic → Task → Run
Orthogonal dimensions: workstream, domain, channel, lifecycle, risk, priority.

Grok metacognition agents (orchestrator, self-repair, self-learn, curator, …)
are the primary control profiles for this stack.
"""

from omniagentos.orgdims.classify import ClassificationService
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.taxonomy import TAXONOMY_VERSION, WORKSTREAMS

__all__ = [
    "ClassificationService",
    "OrgDimsService",
    "TAXONOMY_VERSION",
    "WORKSTREAMS",
]

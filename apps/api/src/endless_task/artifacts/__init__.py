"""Artifact generation services for the P3 runtime."""

from .proposal_service import ArtifactProposalService
from .reference_resolver import SourceReference, SourceReferenceResolver
from .source_labels import MAX_SOURCE_LABELS, parse_source_label

__all__ = [
    "ArtifactProposalService",
    "MAX_SOURCE_LABELS",
    "SourceReference",
    "SourceReferenceResolver",
    "parse_source_label",
]

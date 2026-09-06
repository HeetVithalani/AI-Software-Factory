"""
factory.research.sources
=========================
Research source registry — tracks knowledge sources used during a project.

Sources provide provenance for research findings. Each source has a
trust level so the factory can weight evidence appropriately.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ResearchSource(BaseModel):
    """A knowledge source referenced during research."""
    source_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Type: "documentation", "test_result", "code_analysis",
    #       "external_url", "human_input", "previous_decision"
    source_type: str
    name: str
    reference: str = ""    # URL, file path, or identifier
    # Trust level 1-5: 5=highest (human, test_result), 1=lowest (unverified url)
    trust_level: int = 3
    accessed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRegistry:
    """Tracks all sources accessed during a research session."""

    def __init__(self) -> None:
        self._sources: dict[str, ResearchSource] = {}

    def add(self, source: ResearchSource) -> str:
        self._sources[source.source_id] = source
        return source.source_id

    def add_url(self, name: str, url: str, trust_level: int = 2) -> ResearchSource:
        s = ResearchSource(
            source_type="external_url",
            name=name,
            reference=url,
            trust_level=trust_level,
        )
        self.add(s)
        return s

    def add_test_result(self, name: str, path: str) -> ResearchSource:
        s = ResearchSource(
            source_type="test_result",
            name=name,
            reference=path,
            trust_level=5,  # highest — tests are deterministic evidence
        )
        self.add(s)
        return s

    def all(self) -> list[ResearchSource]:
        return list(self._sources.values())

    def by_trust(self, min_trust: int = 1) -> list[ResearchSource]:
        return [s for s in self._sources.values() if s.trust_level >= min_trust]

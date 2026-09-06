"""
factory.memory.provenance
==========================
Provenance records — tracks the origin of every knowledge entry.

Provenance is stored alongside each global knowledge entry so it can be
re-evaluated, deprecated, or quarantined if the source becomes unreliable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ProvenanceRecord(BaseModel):
    """
    Origin and evidence chain for a knowledge entry.

    Fields
    ------
    source_type:
        Where the knowledge came from: "agent", "test_result", "human",
        "external_doc", "observation".
    source_id:
        Identifier of the source (e.g. agent_id, task_id, URL).
    evidence_hash:
        SHA-256 hash of the deterministic evidence that supports this entry.
        Empty string if no hash is available.
    confidence_basis:
        Human-readable description of WHY this entry is believed.
        An LLM confidence score alone is NOT sufficient basis.
    verified_at:
        Timestamp of last deterministic verification.
    version:
        Incremented each time the entry is updated.
    """
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source_type: str = "agent"
    source_id: str = ""
    project_id: str | None = None
    evidence_hash: str = ""
    confidence_basis: str = ""
    verified_at: datetime | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    version: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "project_id": self.project_id,
            "evidence_hash": self.evidence_hash,
            "confidence_basis": self.confidence_basis,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "created_at": self.created_at.isoformat(),
            "version": self.version,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvenanceRecord":
        verified = data.get("verified_at")
        if isinstance(verified, str):
            verified = datetime.fromisoformat(verified)
        created = data.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return cls(
            record_id=data.get("record_id", str(uuid.uuid4())),
            source_type=data.get("source_type", "agent"),
            source_id=data.get("source_id", ""),
            project_id=data.get("project_id"),
            evidence_hash=data.get("evidence_hash", ""),
            confidence_basis=data.get("confidence_basis", ""),
            verified_at=verified,
            created_at=created or datetime.now(timezone.utc),
            version=data.get("version", 1),
            extra=data.get("extra", {}),
        )

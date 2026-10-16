"""
factory.intake
===============
Trusted tool/skill intake foundation (Phase 2a).

Public surface::

    from factory.intake import IntakeService, IntakeStatus, ToolKind

Lifecycle (fail-closed, revision-pinned, hash-bound):

    DISCOVERED -> QUARANTINED -> SCANNED -> PENDING_APPROVAL -> APPROVED -> INSTALLED

Installed skills are inert files — Phase 2a contains no mechanism that
loads or enables an installed skill into any agent. All scanner and
network boundaries are injectable; the test suite never performs network
requests or executes a real scanner.
"""

from factory.intake.policy import PolicyResult, evaluate_policy
from factory.intake.scanner import (
    ScanOutcome,
    SkillSpectorScanner,
    build_scan_argv,
    parse_scan_report,
)
from factory.intake.service import IntakeService, IntakeSummary
from factory.intake.source import (
    GitHubSource,
    derive_tool_id,
    parse_github_source,
    validate_revision_sha,
)
from factory.intake.transport import HttpxTransport, Transport
from factory.models import IntakeStatus, ScanState, ToolKind, ToolRecord, ToolRevision

__all__ = [
    "IntakeService",
    "IntakeSummary",
    "IntakeStatus",
    "ScanState",
    "ToolKind",
    "ToolRecord",
    "ToolRevision",
    "PolicyResult",
    "evaluate_policy",
    "ScanOutcome",
    "SkillSpectorScanner",
    "build_scan_argv",
    "parse_scan_report",
    "GitHubSource",
    "derive_tool_id",
    "parse_github_source",
    "validate_revision_sha",
    "HttpxTransport",
    "Transport",
]

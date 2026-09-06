"""
factory.audit
=============
Public surface for the audit subsystem.
"""

from factory.audit.logger import AuditLogger, record_event
from factory.audit.evidence import EvidenceRecord, attach_evidence
from factory.audit.reports import generate_summary_report

__all__ = [
    "AuditLogger",
    "record_event",
    "EvidenceRecord",
    "attach_evidence",
    "generate_summary_report",
]

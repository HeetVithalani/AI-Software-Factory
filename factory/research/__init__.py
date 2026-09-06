"""
factory.research
================
Public surface for the research subsystem.
"""

from factory.research.decisions import DecisionRecord, DecisionLog
from factory.research.sources import ResearchSource, SourceRegistry
from factory.research.researcher import ResearchSummary

__all__ = [
    "DecisionRecord",
    "DecisionLog",
    "ResearchSource",
    "SourceRegistry",
    "ResearchSummary",
]

"""
factory.projects
================
Public surface for the project lifecycle subsystem.
"""

from factory.projects.intake import ProjectIntake, IntakeError
from factory.projects.baseline import BaselineCapture
from factory.projects.requirements import Requirements, RequirementsParser
from factory.projects.quality import QualityGate, QualityGateResult

__all__ = [
    "ProjectIntake",
    "IntakeError",
    "BaselineCapture",
    "Requirements",
    "RequirementsParser",
    "QualityGate",
    "QualityGateResult",
]

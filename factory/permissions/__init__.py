"""
factory.permissions
===================
Public surface for the permissions subsystem.
"""

from factory.permissions.policies import PolicySet, load_policies
from factory.permissions.risk import RiskEngine, ActionContext
from factory.permissions.sensitive_data import SensitiveDataDetector
from factory.permissions.approvals import ApprovalManager
from factory.permissions.broker import PermissionBroker, PermissionDecision

__all__ = [
    "PolicySet",
    "load_policies",
    "RiskEngine",
    "ActionContext",
    "SensitiveDataDetector",
    "ApprovalManager",
    "PermissionBroker",
    "PermissionDecision",
]

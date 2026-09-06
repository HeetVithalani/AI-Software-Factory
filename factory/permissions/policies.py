"""
factory.permissions.policies
=============================
Policy loading and enforcement.

Policies define what actions are allowed, require approval, or are
forbidden. NON_NEGOTIABLE policies cannot be weakened by project config —
any attempt to do so is silently rejected and logged as a warning.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Non-negotiable policy names — these CANNOT be set to False/weakened
# ---------------------------------------------------------------------------

NON_NEGOTIABLE: frozenset[str] = frozenset({
    "require_approval_git_mutations",
    "require_approval_sensitive_data",
    "require_approval_high_risk",
    "block_sensitive_to_remote",
    "production_deployment_forbidden",
})


@dataclass
class PolicySet:
    """
    Active policy configuration for the factory (or a project).

    Defaults represent the most secure posture; configs may relax
    non-non-negotiable settings but never the non-negotiable ones.
    """
    require_approval_git_mutations: bool = True
    require_approval_sensitive_data: bool = True
    require_approval_high_risk: bool = True
    block_sensitive_to_remote: bool = True
    production_deployment_forbidden: bool = True
    require_approval_knowledge_high_impact: bool = True
    max_task_retries: int = 3
    auto_approve_inherited: bool = True

    # Extra policy flags loaded from YAML
    extra: dict[str, Any] = field(default_factory=dict)

    def apply_override(self, overrides: dict[str, Any]) -> None:
        """
        Apply policy overrides.

        Non-negotiable policies that would be weakened are rejected.
        """
        for key, value in overrides.items():
            if key not in self.__dataclass_fields__:  # type: ignore[attr-defined]
                self.extra[key] = value
                continue
            current = getattr(self, key)
            # Prevent weakening non-negotiable boolean policies
            if key in NON_NEGOTIABLE and isinstance(current, bool) and current:
                if not bool(value):
                    warnings.warn(
                        f"Policy '{key}' is non-negotiable and cannot be "
                        f"disabled. Ignoring override.",
                        stacklevel=3,
                    )
                    logger.warning(
                        "Blocked attempt to weaken non-negotiable policy: %s",
                        key,
                    )
                    continue
            setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra.get(key, default)


def load_policies(path: Path | None = None) -> PolicySet:
    """
    Load policies from policies.yaml.

    Returns PolicySet with defaults if file is missing or empty.
    """
    if path is None:
        from factory.config import CONFIG_DIR
        path = CONFIG_DIR / "policies.yaml"

    ps = PolicySet()

    if not path.exists():
        logger.debug("policies.yaml not found at %s — using defaults.", path)
        return ps

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        from factory.errors import ConfigurationError
        raise ConfigurationError(
            f"Failed to parse policies.yaml: {exc}"
        ) from exc

    overrides = data.get("policies", {})
    if overrides:
        ps.apply_override(overrides)

    logger.debug("Policies loaded from %s.", path)
    return ps

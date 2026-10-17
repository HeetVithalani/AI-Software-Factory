"""
factory.projects.profiles
==========================
Deterministic project execution profiles.

Profiles adjust how much validation and repair the factory performs. They can
never remove the non-negotiable security stage from a release-eligible plan,
and unknown profile values fail closed rather than silently falling back.
"""

from __future__ import annotations

from dataclasses import dataclass

from factory.errors import ConfigurationError
from factory.models import QualityProfile


@dataclass(frozen=True)
class ProfilePolicy:
    """Concrete, deterministic behaviour associated with a profile."""

    profile: QualityProfile
    include_reviewer: bool
    include_documentation: bool
    pre_build_security: bool
    allow_repair: bool
    max_repair_cycles: int
    default_max_retries: int
    require_full_test_suite: bool
    description: str


_POLICIES: dict[QualityProfile, ProfilePolicy] = {
    QualityProfile.FAST: ProfilePolicy(
        profile=QualityProfile.FAST,
        include_reviewer=False,
        include_documentation=False,
        pre_build_security=False,
        allow_repair=False,
        max_repair_cycles=0,
        default_max_retries=2,
        require_full_test_suite=False,
        description=(
            "Fast: implementation, tests, security, and final audit. No "
            "reviewer or documentation stage; no automatic repair cycle."
        ),
    ),
    QualityProfile.BALANCED: ProfilePolicy(
        profile=QualityProfile.BALANCED,
        include_reviewer=True,
        include_documentation=True,
        pre_build_security=False,
        allow_repair=True,
        max_repair_cycles=1,
        default_max_retries=3,
        require_full_test_suite=True,
        description=(
            "Balanced (default): implementation, review, tests, security, "
            "documentation, and final audit, with one bounded repair cycle."
        ),
    ),
    QualityProfile.HARDENED: ProfilePolicy(
        profile=QualityProfile.HARDENED,
        include_reviewer=True,
        include_documentation=True,
        pre_build_security=True,
        allow_repair=True,
        max_repair_cycles=1,
        default_max_retries=3,
        require_full_test_suite=True,
        description=(
            "Hardened: a pre-build security baseline plus the balanced "
            "pipeline and stricter required checks."
        ),
    ),
}


def resolve_profile(value: str | QualityProfile | None) -> QualityProfile:
    """
    Resolve a profile value, failing closed on unknown input.

    A missing value uses the documented default (balanced). Anything that is
    not one of fast/balanced/hardened is an error — it is never treated as
    the default.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return QualityProfile.BALANCED
    if isinstance(value, QualityProfile):
        return value
    normalised = str(value).strip().lower()
    try:
        return QualityProfile(normalised)
    except ValueError as exc:
        raise ConfigurationError(
            f"Unknown quality profile {value!r}; expected one of: "
            + ", ".join(p.value for p in QualityProfile)
        ) from exc


def profile_policy(value: str | QualityProfile | None) -> ProfilePolicy:
    return _POLICIES[resolve_profile(value)]

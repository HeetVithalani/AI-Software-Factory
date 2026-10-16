"""
factory.intake.policy
======================
Deterministic, explainable intake policy (Phase 2a). Pure functions — no
LLM, no I/O, no side effects. Every decision carries stable reason codes
that are persisted with the revision record.

License policy (internal project categories, not legal advice):
* Only an EXACT recognized SPDX identifier (after surrounding
  whitespace/case normalization) is classified permissive-eligible:
  MIT, Apache-2.0, BSD-2/3-Clause, ISC, CC0-1.0, Unlicense. SPDX
  expressions (``MIT OR GPL-3.0-only``), prefixed/compound values
  (``MIT-proprietary``), and anything unrecognized fail closed to
  manual review — expressions are NOT parsed (no SPDX expression
  parser exists in project dependencies).
* Unknown/missing license → BLOCKED (human review path; record the license).
* GPL/AGPL/FSL/source-available/non-standard terms → BLOCKED for explicit
  manual review; never auto-approved and never mislabelled as permissive.
* Provenance: license_source="declared" means an OPERATOR supplied the
  value at discover time — it is NOT an independently verified repository
  license. Such licenses always record a LICENSE_DECLARED_UNVERIFIED
  reason here (visible in check/approve output, never labelled verified).
  The reason does NOT block the check() verdict — blocking it outright
  would make the distinct "clean scan + policy pass → pending human
  approval" branch unreachable. Instead the attestation is enforced at
  approval time: service.approve() refuses operator-declared provenance
  unless an explicit --manual-review --note attestation is recorded.
  Nothing in Phase 2a verifies licenses, so every approval of an
  operator-declared license needs that attestation.

Capability policy (agent_skill, Phase 2a):
* Prompt/reference content only. Skills requesting shell execution,
  arbitrary network access, secret access, filesystem mutation outside the
  install location, or privileged capabilities are BLOCKED for manual
  review — requested capabilities are never granted just because asked.

Scan policy:
* Any non-clean scan state (not_run / failed / unavailable / findings)
  blocks auto-approval. Upstream CAUTION is recorded as upstream CAUTION
  (never mislabelled as upstream BLOCKED); upstream DO NOT INSTALL blocks.
* LOCAL_POLICY_STRICTER: this project blocks score > 20 even though
  upstream only marks 21-50 as CAUTION. Local policy is named explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Internal permissive-eligibility list (SPDX-style identifiers).
PERMISSIVE_LICENSES: frozenset[str] = frozenset({
    "MIT",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "ISC",
    "CC0-1.0",
    "UNLICENSE",
})

# Prefix/family markers that require explicit manual review (never
# auto-approved, never treated as permissive).
_RESTRICTIVE_MARKERS = (
    "GPL", "AGPL", "LGPL", "MPL", "CDDL", "EPL", "EUPL", "OSL",
    "FSL", "BUSL", "SSPL", "CC-BY-NC", "CC-BY-SA", "PROPRIETARY",
    "SOURCE-AVAILABLE", "LICENSE-REF-",
)

# Local policy: block scores above 20 (stricter than upstream CAUTION band).
LOCAL_MAX_SCORE = 20


@dataclass(frozen=True)
class PolicyReason:
    """One explainable policy reason with a stable machine code."""
    code: str
    detail: str


@dataclass
class PolicyResult:
    """Deterministic policy verdict. ``decision`` is 'pass' or 'blocked'."""
    decision: str                       # "pass" | "blocked"
    reasons: list[PolicyReason] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision == "pass"

    def as_dicts(self) -> list[dict[str, str]]:
        return [{"code": r.code, "detail": r.detail} for r in self.reasons]


def _normalize_license(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.strip()
    # Collapse whitespace; keep the raw wording for storage elsewhere.
    return " ".join(text.split())


def classify_license(declared: str | None) -> tuple[str, PolicyReason]:
    """
    Classify a declared license identifier/text.

    Returns (category, reason) where category is one of:
    'permissive', 'restrictive', 'unknown'. The raw declared text is
    preserved in the reason detail — no guessing from repository names.

    Permissive classification requires an EXACT match against a recognized
    SPDX identifier after harmless whitespace/case normalization. Prefix
    matching is deliberately NOT used: values such as ``MIT OR GPL-3.0-only``,
    ``MIT AND AGPL-3.0-only`` or ``MIT-proprietary`` must never inherit the
    permissive label from a leading identifier. SPDX expressions are not
    parsed (no expression parser is available in project dependencies);
    expressions, compounds, and unrecognized values fail closed.
    """
    text = _normalize_license(declared)
    if not text:
        return "unknown", PolicyReason(
            "LICENSE_UNKNOWN",
            "No license declared/recorded; blocked pending human review "
            "(record the license at discover time).",
        )
    upper = text.upper()
    if upper in PERMISSIVE_LICENSES:
        return "permissive", PolicyReason(
            "LICENSE_PERMISSIVE",
            f"Declared license {text!r} exactly matches a permissive-eligible "
            "SPDX identifier (operator-declared; see provenance reason).",
        )
    for marker in _RESTRICTIVE_MARKERS:
        if marker in upper:
            return "restrictive", PolicyReason(
                "LICENSE_RESTRICTIVE_MANUAL_REVIEW",
                f"Declared license {text!r} matches restrictive family {marker!r}; "
                "explicit manual review required.",
            )
    return "unknown", PolicyReason(
        "LICENSE_UNKNOWN",
        f"Declared license {text!r} is not a recognized exact SPDX "
        "identifier (expressions and non-standard values are not parsed "
        "and fail closed); blocked pending human review.",
    )


# Requested-capability codes that must never be granted implicitly.
_BLOCKED_CAPABILITIES: dict[str, str] = {
    "shell": "CAPABILITY_BLOCKED_SHELL",
    "shell_execution": "CAPABILITY_BLOCKED_SHELL",
    "exec": "CAPABILITY_BLOCKED_SHELL",
    "network": "CAPABILITY_BLOCKED_NETWORK",
    "http": "CAPABILITY_BLOCKED_NETWORK",
    "internet": "CAPABILITY_BLOCKED_NETWORK",
    "secrets": "CAPABILITY_BLOCKED_SECRETS",
    "credentials": "CAPABILITY_BLOCKED_SECRETS",
    "env": "CAPABILITY_BLOCKED_SECRETS",
    "filesystem_write": "CAPABILITY_BLOCKED_FS_MUTATION",
    "write": "CAPABILITY_BLOCKED_FS_MUTATION",
    "file_write": "CAPABILITY_BLOCKED_FS_MUTATION",
    "privileged": "CAPABILITY_BLOCKED_PRIVILEGED",
    "sudo": "CAPABILITY_BLOCKED_PRIVILEGED",
    "admin": "CAPABILITY_BLOCKED_PRIVILEGED",
}


def evaluate_capabilities(requested: list[str]) -> list[PolicyReason]:
    """
    Evaluate requested capabilities for agent_skill content.

    Phase 2a allowlist: prompt/reference content only. Anything beyond it
    is blocked for manual review.
    """
    reasons: list[PolicyReason] = []
    for cap in requested or []:
        key = str(cap).strip().lower()
        code = _BLOCKED_CAPABILITIES.get(key)
        if code:
            reasons.append(PolicyReason(
                code, f"Requested capability {cap!r} is outside the "
                      "prompt/reference-content allowlist; manual review required."
            ))
        else:
            reasons.append(PolicyReason(
                "CAPABILITY_UNRECOGNIZED",
                f"Requested capability {cap!r} is not recognized; treated as "
                "blocked pending manual review.",
            ))
    return reasons


def evaluate_scan(scan_state: str, risk_score: int | None,
                  recommendation: str | None) -> list[PolicyReason]:
    """Reasons contributed by the scan outcome. Empty list ⇒ eligible."""
    reasons: list[PolicyReason] = []
    if scan_state in ("not_run", "failed", "unavailable"):
        # No usable scan evidence — score/recommendation are meaningless.
        reasons.append(PolicyReason(
            "SCAN_NOT_CLEAN",
            f"Scan state is {scan_state!r}; only a completed clean scan "
            "(zero findings) can pass automatically.",
        ))
        return reasons

    # scan_state is "clean" or "findings": a COMPLETED scan — the score and
    # upstream recommendation are meaningful and must be evaluated.
    if scan_state == "findings":
        reasons.append(PolicyReason(
            "SCAN_FINDINGS_PRESENT",
            "Scan completed with findings; only a zero-finding scan can "
            "pass automatically.",
        ))

    # Normalize separators: the live SkillSpector v2.12.0 JSON report emits
    # "DO_NOT_INSTALL" (underscores) while the terminal form and our fixtures
    # use "DO NOT INSTALL" — both must hit the blocked-reason branch.
    rec = (recommendation or "").strip().upper().replace("_", " ")
    if "DO NOT INSTALL" in rec:
        reasons.append(PolicyReason(
            "UPSTREAM_RECOMMENDATION_BLOCKED",
            "Upstream scanner recommendation is DO NOT INSTALL.",
        ))
    elif "CAUTION" in rec:
        reasons.append(PolicyReason(
            "UPSTREAM_CAUTION_RECORDED",
            "Upstream scanner recommendation is CAUTION (recorded as "
            "upstream CAUTION, not upstream BLOCKED).",
        ))
    if risk_score is not None and risk_score > LOCAL_MAX_SCORE:
        reasons.append(PolicyReason(
            "LOCAL_POLICY_STRICTER_SCORE",
            f"Local project policy blocks risk_score {risk_score} "
            f"(local max {LOCAL_MAX_SCORE}; stricter than the upstream band).",
        ))
    return reasons


def evaluate_policy(
    *,
    scan_state: str,
    risk_score: int | None,
    recommendation: str | None,
    license_declared: str | None,
    license_source: str = "unknown",
    requested_capabilities: list[str],
) -> PolicyResult:
    """
    Combined deterministic verdict for one revision.

    decision == 'pass' only when: clean scan within local score policy,
    an exactly-matching permissive-eligible license, and no requested
    capabilities outside the prompt/reference-content allowlist.

    Unverified license provenance (operator-declared or unknown source)
    is recorded as a NON-BLOCKING LICENSE_DECLARED_UNVERIFIED reason —
    visible in check/approve output but never labelled verified.
    The attestation requirement (--manual-review --note) is enforced in
    service.approve(), so an operator-declared license can reach pending
    human approval but can never be approved silently.
    """
    reasons: list[PolicyReason] = []

    reasons.extend(evaluate_scan(scan_state, risk_score, recommendation))

    _lic_cat, lic_reason = classify_license(license_declared)
    reasons.append(lic_reason)
    # Provenance gate: "declared" means an operator typed the value at
    # discover time — NOT that a repository license file was verified.
    # The reason is recorded for visibility in check/approve output but is
    # deliberately NON-BLOCKING here: enforcement happens as an explicit
    # --manual-review --note attestation in service.approve(), which keeps
    # the "clean scan + policy pass → pending human approval" branch
    # reachable while never letting declared provenance pass silently.
    if license_declared and license_source != "verified":
        reasons.append(PolicyReason(
            "LICENSE_DECLARED_UNVERIFIED",
            f"License {license_declared!r} is operator-declared at discover "
            f"time (source: {license_source!r}) and has NOT been independently "
            "verified; approval requires an explicit manual-review "
            "attestation (--manual-review --note).",
        ))

    reasons.extend(evaluate_capabilities(requested_capabilities))

    # Non-blocking reason codes: positive classifications (LICENSE_PERMISSIVE)
    # and the provenance-visibility reason enforced at approval time.
    blocking = [
        r for r in reasons
        if r.code not in ("LICENSE_PERMISSIVE", "LICENSE_DECLARED_UNVERIFIED")
    ]
    decision = "pass" if not blocking else "blocked"
    return PolicyResult(decision=decision, reasons=reasons)

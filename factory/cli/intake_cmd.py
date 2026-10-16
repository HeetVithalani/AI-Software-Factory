"""
factory.cli.intake_cmd
=======================
CLI sub-commands for the trusted tool/skill intake (Phase 2a).

Commands::

    factory intake discover --source <github-repo-url> --revision <40-char-sha>
                            [--kind agent_skill] [--name <name>]
                            [--license <spdx-or-text>] [--capability <cap>]...
    factory intake fetch   <tool-id> --revision <sha>
    factory intake scan    <tool-id> --revision <sha>
    factory intake check   <tool-id> --revision <sha>
    factory intake approve <tool-id> --revision <sha>
                            [--manual-review --note "reason"]
    factory intake install <tool-id> --revision <sha>
    factory intake status  [<tool-id>]
    factory intake disable <tool-id>            (global revoke)
    factory intake enable  <tool-id> --revision <sha> --project-path <dir>
                            --note "reason" [--actor <name>]
    factory intake unbind  <tool-id> --project-path <dir> --note "reason"
    factory intake skills  [--project-path <dir>]

Design notes (mirrors factory/cli/approvals_cmd.py conventions):
* Human-driven throughout - no command auto-approves or auto-installs.
* Failure states are loud: scan failure/unavailable exits nonzero and the
  revision remains unapprovable; no flag can make an incomplete scan pass.
* approve prints the full evidence binding (revision, artifact hash,
  scan-report hash, scanner status, findings summary, license result,
  policy reasons) before accepting an explicit decision.
* Installed skills are globally inert. They enter agents only as
  untrusted reference context after an explicit per-project 'enable'
  (Phase 2c); 'unbind' removes only the project binding and never
  revokes or deletes the global installation.
"""

from __future__ import annotations

import argparse
import logging
import sys

from factory.errors import IntakeError

logger = logging.getLogger(__name__)


def _val(x) -> str:
    """Model enum fields are plain str (use_enum_values); tolerate enums too."""
    return x.value if hasattr(x, "value") else str(x)


def _license_provenance_label(source: str) -> str:
    """Make license provenance explicit: 'declared' is NOT 'verified'."""
    return {
        "declared": "operator-declared, NOT independently verified",
        "unknown": "unknown provenance (not recorded)",
        "verified": "independently verified",
    }.get(source, source)



def _svc():
    from factory.intake.service import IntakeService
    return IntakeService()


def _print_summary(summary) -> None:
    t, r = summary.tool, summary.revision
    print(f"  Tool:      {t.tool_id}  ({_val(t.kind)} '{t.name}')")
    print(f"  Source:    {t.source_url}")
    print(f"  Revision:  {r.revision_sha}")
    print(f"  Status:    {_val(t.status)}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def cmd_discover(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        summary = svc.discover(
            args.source,
            args.revision,
            kind=args.kind,
            name=args.name or "",
            license_declared=args.license,
            capabilities=args.capability or [],
        )
    except IntakeError as exc:
        print(f"discover failed: {exc}", file=sys.stderr)
        return 1
    print("[OK] Discovered and recorded (no network lookup performed).")
    _print_summary(summary)
    lic = summary.tool.license_declared
    if lic:
        print(f"  License:   {lic} "
              f"[{_license_provenance_label(summary.tool.license_source)}]")
    else:
        print("  License:   (none recorded - will block policy)")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        summary = svc.fetch(args.tool_id, args.revision)
    except IntakeError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1
    r = summary.revision
    already = summary.detail.get("already")
    prefix = "[OK] Already quarantined (verified in place)." if already else "[OK] Fetched into quarantine."
    print(prefix)
    _print_summary(summary)
    print(f"  Artifact SHA-256: {r.artifact_sha256}")
    print(f"  Tree SHA-256:     {r.tree_sha256}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        summary = svc.scan(args.tool_id, args.revision)
    except IntakeError as exc:
        print(f"scan failed (fail-closed): {exc}", file=sys.stderr)
        print(
            "This revision is NOT approvable. Fix the scanner (install/"
            "configure SkillSpector, or resolve the archive) and re-run scan.",
            file=sys.stderr,
        )
        return 1
    r = summary.revision
    print("[OK] Scan completed.")
    _print_summary(summary)
    print(f"  Scan state:            {_val(r.scan_state)}")
    print(f"  Risk score:            {r.risk_score}")
    print(f"  Severity:              {r.severity}")
    print(f"  Upstream recommendation: {r.recommendation}")
    print(f"  Unresolved (H/C):      {r.unresolved_findings}")
    print(f"  Report SHA-256:        {r.scan_report_sha256}")
    if _val(r.scan_state) == "findings":
        print("  NOTE: findings present - deterministic policy will block auto-approval.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        summary, result = svc.check(args.tool_id, args.revision)
    except IntakeError as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return 1
    r = summary.revision
    t = summary.tool
    print(f"Policy decision: {result.decision.upper()}")
    print(f"License: {t.license_declared or '(none)'} "
          f"[{_license_provenance_label(t.license_source)}]")
    for reason in result.as_dicts():
        print(f"  - {reason['code']}: {reason['detail']}")
    print(f"Status now: {_val(t.status)}")
    if result.decision == "pass" and _val(t.status) == "pending_approval":
        print(f"  Approval request: {r.approval_request_id}")
        if t.license_declared and t.license_source != "verified":
            print("  License provenance is operator-declared/unverified - "
                  "approve requires '--manual-review --note' attestation.")
        print("  Next: factory approvals show <request_id>, then "
              f"'factory intake approve {args.tool_id} --revision {args.revision}'")
        # Genuinely reached a valid next state: pending human approval.
        return 0
    # Fail-closed exit status: policy-blocked or failed/non-approvable
    # outcomes are nonzero - callers must not treat them as success even
    # though the check itself ran and printed its (loud) BLOCKED result.
    print("  Check outcome is not approvable (blocked/failed); "
          "exit status is nonzero.")
    return 1


def cmd_approve(args: argparse.Namespace) -> int:
    svc = _svc()

    # Always show the evidence binding before asking for confirmation.
    rows = svc.status(args.tool_id)
    row = next((x for x in rows if x["tool_id"] == args.tool_id), None)
    rev = row and row.get("revision")
    if row is None or rev is None or rev.get("revision_sha") != args.revision:
        print("No matching revision found for that tool id/revision.", file=sys.stderr)
        return 1

    print("-" * 60)
    print("  APPROVAL EVIDENCE (verify before deciding)")
    print("-" * 60)
    print(f"  Tool:            {row['tool_id']}  ({row['kind']} '{row['name']}')")
    print(f"  Source:          {row['source_url']}")
    print(f"  Revision SHA:    {rev['revision_sha']}")
    print(f"  Artifact SHA-256:  {rev['artifact_sha256']}")
    print(f"  Scan report SHA-256: {rev['scan_report_sha256']}")
    print(f"  Scanner state:   {rev['scan_state']}  (risk={rev['risk_score']} "
          f"severity={rev['severity']} unresolved={rev['unresolved_findings']})")
    print(f"  Upstream rec:    {rev['recommendation']}")
    print(f"  License:         {row['license_declared'] or '(none)'} "
          f"[{_license_provenance_label(row['license_source'])}]")
    print(f"  Capabilities:    {row['capabilities'] or '(prompt/reference content only)'}")
    print(f"  Policy decision: {rev['policy_decision']}")
    for reason in rev["policy_reasons"]:
        print(f"    - {reason['code']}: {reason['detail']}")
    print("-" * 60)

    if not args.yes:
        try:
            answer = input("Approve this exact revision? [y/N] ").strip().lower()
        except (EOFError, ValueError):
            # Non-interactive stdin (closed/piped/absent): fail cleanly with
            # an actionable message - no traceback and no approval side
            # effect (the prompt runs before any service mutation).
            print(
                "Cannot prompt for approval: stdin is not interactive. "
                "Review the evidence above, then re-run with --yes to "
                "approve non-interactively. Nothing was approved.",
                file=sys.stderr,
            )
            return 1
        if answer != "y":
            print("Not approved.")
            return 1

    try:
        summary = svc.approve(
            args.tool_id,
            args.revision,
            manual_review=bool(args.manual_review),
            note=args.note or "",
        )
    except IntakeError as exc:
        print(f"approve failed: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] APPROVED (bound to {args.tool_id}@{args.revision} with recorded hashes).")
    print("  Next: 'factory intake install <tool-id> --revision <sha>'")
    _print_summary(summary)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        summary = svc.install(args.tool_id, args.revision)
    except IntakeError as exc:
        print(f"install failed (fail-closed): {exc}", file=sys.stderr)
        return 1
    print("[OK] Installed (content copied; globally inert until explicitly "
          "enabled per project with 'factory intake enable').")
    _print_summary(summary)
    print(f"  Install path: {summary.detail.get('install_path')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        rows = svc.status(getattr(args, "tool_id", None))
    except IntakeError as exc:
        print(f"status failed: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("No tools discovered yet.")
        return 0
    for row in rows:
        print("-" * 60)
        print(f"  Tool:    {row['tool_id']}  ({row['kind']} '{row['name']}')")
        print(f"  Source:  {row['source_url']}")
        print(f"  Status:  {row['status']} - {row['label']}")
        print(f"  License: {row['license_declared'] or '(none)'} "
              f"[{_license_provenance_label(row['license_source'])}]")
        rev = row.get("revision")
        if rev:
            print(f"  Revision: {rev['revision_sha'][:12]}...  "
                  f"scan={rev['scan_state']} risk={rev['risk_score']} "
                  f"policy={rev['policy_decision']}")
            if rev.get("approved_by"):
                print(f"  Approved by: {rev['approved_by']} at {rev['approved_at']}")
        if row.get("install_path"):
            print(f"  Install:  {row['install_path']} (inert until enabled per project)")
    print("-" * 60)
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    svc = _svc()
    try:
        tool = svc.disable(args.tool_id)
    except IntakeError as exc:
        print(f"disable failed: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Revoked {args.tool_id}. Evidence and registry rows retained. "
          f"Status: {_val(tool.status)}")
    return 0


def _current_actor() -> str:
    """Best-effort operator identity for the audit record (never blocks)."""
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


def cmd_enable(args: argparse.Namespace) -> int:
    """Enable an installed skill revision for ONE project (Phase 2c)."""
    from factory.skills.bindings import enable_skill
    note = (args.note or "").strip()
    if not note:
        print(
            "enable failed: a non-empty --note is mandatory (recorded in "
            "the audit event).",
            file=sys.stderr,
        )
        return 1
    actor = (args.actor or "").strip() or _current_actor()
    from factory.audit.logger import AuditLogger
    try:
        binding = enable_skill(
            args.tool_id,
            args.revision,
            args.project_path,
            note=note,
            actor=actor,
            audit_logger=AuditLogger(),
        )
    except IntakeError as exc:
        print(f"enable failed: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Skill enabled for project {binding.project_key} ONLY.")
    print("  Reference context only: never executed, untrusted, and it "
          "grants no tool or broker capability.")
    print(f"  Tool:     {binding.tool_id}")
    print(f"  Revision: {binding.revision_sha}")
    print(f"  Actor:    {binding.actor}")
    print(f"  Note:     {binding.note}")
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    """Remove a project's skill binding; the global install is untouched."""
    from factory.skills.bindings import disable_skill
    note = (args.note or "").strip()
    if not note:
        print(
            "unbind failed: a non-empty --note is mandatory (recorded in "
            "the audit event).",
            file=sys.stderr,
        )
        return 1
    actor = (args.actor or "").strip() or _current_actor()
    from factory.audit.logger import AuditLogger
    try:
        binding = disable_skill(
            args.tool_id,
            args.project_path,
            note=note,
            actor=actor,
            audit_logger=AuditLogger(),
        )
    except IntakeError as exc:
        print(f"unbind failed: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Skill binding removed for project {binding.project_key}.")
    print("  The global installation is unchanged (not revoked, not deleted).")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    """Inspect installed skills and (optionally) a project's bindings."""
    from factory.skills.bindings import (
        list_bindings,
        list_installed_skills,
        project_key_for,
    )
    tools = list_installed_skills()
    print(
        "Installed skills (globally inert; reference context only, and "
        "only after an explicit per-project enable):"
    )
    if not tools:
        print("  (none installed)")
    for t in tools:
        print(f"  - {t.tool_id}  '{t.name}'")
        print(f"      install path: {t.install_path}")
    project_path = getattr(args, "project_path", None)
    if project_path:
        key = project_key_for(project_path)
        print(f"Enabled for project {key}:")
        bindings = list_bindings(key)
        if not bindings:
            print("  (no enabled skills)")
        for b in bindings:
            print(f"  - {b.tool_id}@{b.revision_sha}")
            print(f"      enabled by {b.actor}: {b.note}")
    return 0


# ---------------------------------------------------------------------------
# Parser registration (mirrors approvals_cmd conventions)
# ---------------------------------------------------------------------------

def register_intake_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'intake' command and its sub-subcommands."""
    intake_parser = subparsers.add_parser(
        "intake",
        help="Trusted tool/skill intake (discover -> fetch -> scan -> check -> approve -> install)",
        description=(
            "Revision-pinned, hash-bound, fail-closed intake for external "
            "tools and agent skills. Installed skills stay globally inert; "
            "per-project 'enable' grants untrusted reference context only."
        ),
        epilog=(
            "example:\n"
            "  factory intake discover --source https://github.com/owner/repo "
            "--revision <40-char-sha> --license MIT\n"
            "  factory intake fetch   <tool-id> --revision <sha>\n"
            "  factory intake scan    <tool-id> --revision <sha>\n"
            "  factory intake check   <tool-id> --revision <sha>\n"
            "  factory intake approve <tool-id> --revision <sha>\n"
            "  factory intake install <tool-id> --revision <sha>\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    intake_sub = intake_parser.add_subparsers(
        dest="intake_command", required=True,
    )

    # --- discover ---
    d = intake_sub.add_parser(
        "discover", help="Validate and record a canonical GitHub source + pinned SHA",
    )
    d.add_argument("--source", required=True,
                   help="Canonical URL: https://github.com/<owner>/<repo>")
    d.add_argument("--revision", required=True,
                   help="Exact 40-character commit SHA (no branches/tags)")
    d.add_argument("--kind", default="agent_skill", choices=["agent_skill"],
                   help="Integration kind (Phase 2a: agent_skill only)")
    d.add_argument("--name", default="", help="Human name (default: repo name)")
    d.add_argument("--license", default=None,
                   help="Declared license identifier/text (recorded verbatim; "
                        "missing license blocks policy)")
    d.add_argument("--capability", action="append", default=[],
                   help="Requested capability (repeatable; anything beyond "
                        "prompt/reference content is blocked for review)")

    # --- fetch / scan / check / install share tool-id + --revision ---
    for name, help_text in [
        ("fetch", "Download the pinned revision into the git-ignored quarantine"),
        ("scan", "Run the deterministic scanner (fails closed if unavailable)"),
        ("check", "Evaluate deterministic policy; may create an approval request"),
        ("install", "Copy approved content into the isolated install dir (never executed)"),
    ]:
        p = intake_sub.add_parser(name, help=help_text)
        p.add_argument("tool_id", help="Tool id from discover")
        p.add_argument("--revision", required=True, help="Exact 40-character SHA")

    # --- approve ---
    a = intake_sub.add_parser(
        "approve", help="Record a human, hash-bound approval for one revision",
    )
    a.add_argument("tool_id", help="Tool id from discover")
    a.add_argument("--revision", required=True, help="Exact 40-character SHA")
    a.add_argument("--manual-review", action="store_true",
                   help="Explicitly override a BLOCKED policy decision "
                        "(requires --note; never possible for failed scans)")
    a.add_argument("--note", default="", help="Decision note / override reason")
    a.add_argument("--yes", action="store_true",
                   help="Skip the interactive confirmation prompt")

    # --- status ---
    s = intake_sub.add_parser("status", help="Show intake state for tools")
    s.add_argument("tool_id", nargs="?", default=None, help="Optional tool id")

    # --- disable ---
    x = intake_sub.add_parser(
        "disable", help="Revoke registry eligibility (evidence retained)",
    )
    x.add_argument("tool_id", help="Tool id to revoke")

    # --- enable (Phase 2c: per-project runtime reference context) ---
    e = intake_sub.add_parser(
        "enable",
        help="Enable an installed skill revision for ONE project (untrusted reference context)",
    )
    e.add_argument("tool_id", help="Tool id from discover")
    e.add_argument("--revision", required=True,
                   help="Exact 40-character SHA of the INSTALLED revision")
    e.add_argument("--project-path", required=True,
                   help="Resolved project workspace directory - the SAME "
                        "path passed to 'factory run <project_path>'")
    e.add_argument("--note", default="",
                   help="Mandatory audit note (recorded verbatim)")
    e.add_argument("--actor", default="",
                   help="Operator identity (default: current OS user)")

    # --- unbind (Phase 2c: project-scoped disable; never a global revoke) ---
    u = intake_sub.add_parser(
        "unbind",
        help="Remove a project's skill binding (global install untouched)",
    )
    u.add_argument("tool_id", help="Tool id to unbind for this project")
    u.add_argument("--project-path", required=True,
                   help="Resolved project workspace directory")
    u.add_argument("--note", default="",
                   help="Mandatory audit note/reason")
    u.add_argument("--actor", default="",
                   help="Operator identity (default: current OS user)")

    # --- skills (Phase 2c: inspection) ---
    sk = intake_sub.add_parser(
        "skills", help="Inspect installed skills and their project bindings",
    )
    sk.add_argument("--project-path", default=None,
                    help="Also list the bindings enabled for this project")


def dispatch_intake(args: argparse.Namespace) -> int:
    """Route to the correct intake sub-command handler."""
    from factory.database import initialize_database
    initialize_database()

    cmd = args.intake_command
    handlers = {
        "discover": cmd_discover,
        "fetch": cmd_fetch,
        "scan": cmd_scan,
        "check": cmd_check,
        "approve": cmd_approve,
        "install": cmd_install,
        "status": cmd_status,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "unbind": cmd_unbind,
        "skills": cmd_skills,
    }
    handler = handlers.get(cmd)
    if handler is None:
        print(f"Unknown intake command: {cmd}", file=sys.stderr)
        return 1
    try:
        return handler(args)
    except IntakeError as exc:
        print(f"intake {cmd} failed: {exc}", file=sys.stderr)
        return 1

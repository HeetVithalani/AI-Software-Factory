# Final Implementation Status

Status of the final approved implementation milestone on branch
`fix/fail-closed-quality-gates`, executed 2026-10-17. Baseline:
`34b37177c8f1e7cc1bb107936e9f2baf80806f53` (dedicated testing phase, docs
only, 863/863 eligible tests passed, 3 held).

This document is the milestone's evidence-backed close-out. It reports what
was implemented, how it was verified, what is explicitly NOT claimed, and
what remains for the dedicated testing phase. It is written so that the 12
sections below map one-to-one to the milestone's required scope.

**Not claimed anywhere in this document:** production readiness, model
superiority, live-runtime security proof, or resolution of open human
decisions. Saluki is not and was not made a default model.

---

## 1. Scope and delivered commits

Delivered capabilities (all on top of the baseline, none on `main`/`master`):

| # | Commit | Scope |
|---|---|---|
| 1 | `05d8d64` | Planning: schema v7 (`project_briefs`, `research_records`, `plans`, `plan_approvals`); `factory/planning/{brief,research,plan_store,generator}.py`; `factory/projects/profiles.py`; `factory/validation/strategy.py`; `dag_from_plan_dict` shared loader; `FACTORY_DB_PATH` override. |
| 2 | `f1f2f19` | Recovery: `factory/orchestration/task_store.py` (durable task rows, recovery marking/resolution); `factory/orchestration/boot.py` (`prepare_dag_for_execution`); orchestrator task/evidence/checkpoint persistence (`project_id`/`plan_id`/`plan_hash`/`task_store`/`workspace_root`); `OrchestratorStatus.RECOVERY_REQUIRED`. |
| 3 | `382527b` | CLI: `factory plan create\|list\|show\|approve\|reject`; `factory run --plan-id`; `factory tasks\|evidence\|audit`; `factory recovery status\|resolve`; per-run `--model`; `ProjectIntake` helpers. |
| 4 | `bbd14e4` | Context: `factory/context/collector.py` (deterministic task-aware workspace context); `ContextBuilder` context injection + manifest `included_files`; builder wiring. |
| 5 | `6195881` | Quality: `FailureKind.REPAIRABLE`; QA deterministic failure classification; `Orchestrator._try_schedule_repair`; builder repair-context consumption; `factory/validation/final_audit.py`; deterministic fail-closed `AuditorAgent`. |
| 6 | `24d90b0` | Run plan identity injected into every dispatched task (auditor verifies approval/hash from it). |
| 7 | `36011ad` | `StateManager` (not booted `FactoryState`) passed to the orchestrator in approved-plan runs; regression test added. |

History was preserved; each change landed as a small, logically separated
commit with targeted tests and a clean staged-diff review. Nothing was
rebased, reset, or force-pushed.

## 2. Briefs and local research (R → P input)

- `build_brief` persists an immutable brief per project spec with source
  kind/reference and a spec content hash; unknown requirements are recorded
  as explicit unknowns, never invented.
- `run_local_research` scans the local workspace only. Every record carries
  the explicit limitation "no external research performed". There is no
  network research path in this milestone.
- Both records are persisted in schema v7 tables and referenced by plans.

## 3. Versioned plans and hash-bound approval

- `PlanStore.create` stores immutable plan versions. A new version never
  mutates an older one and never inherits its approval; the prior version is
  marked `superseded`.
- Every plan has a canonical SHA-256 over its content. A human decision is
  stored with the exact hash reviewed. `PlanStore.decide` refuses wrong or
  missing hashes; any byte change invalidates prior decisions; tampered
  content fails `require_approved` and the final audit's `plan_approval`
  check.
- `PlanStore` validation is deterministic and fail-closed: valid DAG,
  requirement traceability, acceptance criteria, closed-set
  `required_tools` capabilities, QA `required_checks`, and mandatory
  builder/QA/security/auditor stages for release-eligible (`standard`)
  plans. Unknown stack → plan creation refused. Unknown pipeline → refused.
- `smoke` pipeline: single builder task, always `release_eligible=false`,
  intended only for bounded model evaluation.
- The generator is deterministic and makes no architecture decisions; no
  LLM participates in plan creation or validation.

## 4. Approved-plan execution (CLI)

- `factory run --plan-id <id>` executes **only** plans that pass
  `PlanStore.require_approved` — pending, rejected, superseded, missing, or
  tampered plans are refused with exit 1.
- Execution loads the registered project's own workspace (refusing if
  missing), resolves the approved plan's profile policy, reconstructs the
  DAG through the single `dag_from_plan_dict` source of truth, and wires
  TaskStore/RecoveryManager/boot/broker/audit into the orchestrator.
- Startup state is reconciled before any work: refusal with **exit 2** when
  human recovery is required, printing the exact `factory recovery resolve`
  commands.
- The legacy `factory run <path>` + `plan.json` path is preserved for
  compatibility; it is never able to claim release eligibility (see §9).
- No `plan.json` is auto-written into workspaces — a generated file could be
  executed by the legacy path without approval, so the CLI never creates one.

## 5. Startup recovery (RecoveryPlan consumption)

- `factory/orchestration/boot.py::prepare_dag_for_execution` is the
  production startup path: it verifies project/plan identity and checkpoint
  integrity, restores completed-with-evidence tasks idempotently, converts
  RUNNING/PAUSED tasks at shutdown to **terminal recovery-required** (never
  guessed-at), restores parked approvals only when their request id exists,
  and fails closed on unknown statuses or hash mismatches.
- `TaskStore` durably records task state; only an explicit human
  `factory recovery resolve <task> --action retry|skip --by "<name>"`
  clears recovery-required state. `retry` re-queues from a clean state;
  `skip` is permanent.
- `factory recovery status` reports recovery-required tasks, the last
  checkpoint with per-file hash drift (changed/missing), and pending
  approvals.
- The legacy `RecoveryManager.recover()`/`RecoveryPlan` library surface
  remains (used by the recovery tests); the production boot path consumes
  the same underlying checkpoint via `load_last_checkpoint` plus durable
  task rows. Roadmap task 3.3 is functionally implemented.

## 6. Task-aware context, tool declarations, and skills

- `factory/context/collector.py` produces deterministic, bounded workspace
  context per task: sorted depth-capped walk, keyword scoring that
  outranks fixed-priority files, hard file/char budgets with truncation
  flags, and exclusion of sensitive (`*.env*`, keys, credentials) and
  binary files even when the task names them.
- `ContextBuilder` appends the context block **after** system policy and
  skill framing (instructions and policy always precede reference
  material) and records the exact `included_files` list in the persisted
  context manifest.
- `BuilderAgent` collects context once per task and reuses it for every
  turn, including retry-augmented turns; collection failure degrades to no
  context, never blocks work.
- Tool declarations: plan tasks declare `required_tools` from a closed-set
  capability list validated at plan creation; agents run with
  role-scoped tool bundles; the permission broker remains the enforcement
  point for every operation. Declarations are audit provenance, not a
  grant.
- Skills: the existing Phase 2c `SkillContextProvider` remains wired in
  both CLI execution paths under the project key; skills stay inert
  Markdown framing (never policy).

## 7. Adaptive validation strategy

- `factory/validation/strategy.py` derives the required check set from the
  detected workspace stack (pytest for Python, node test for JS, etc.).
- The strategy feeds plan metadata (`required_checks`) on the QA task, and
  QA execution is driven by the deterministic testing tool; a stack with
  required checks the factory cannot run fails plan creation rather than
  being silently skipped.
- QA success/failure is decided **only** by the deterministic test result
  (`pytest returncode`); the LLM verdict is recorded in the report and can
  never override it.

## 8. Bounded repair cycles

- QA marks deterministic verification failures `failure_kind="repairable"`.
- The orchestrator alone decides, from **approved plan metadata**
  (`allow_repair`, `max_repair_cycles`), whether one bounded repair cycle
  is permitted:
  - `fast` profile: repair disabled → immediate terminal failure;
  - `balanced`/`hardened`: 1 repair cycle maximum.
- A scheduled repair re-queues the builder dependency with a bounded
  failure context, invalidates completed downstream verification (stale
  verification of repaired code), and increments the audited
  `repair_attempts` counter. Parked approvals and human skips are never
  silently reset; exhaustion yields a terminal failure with a
  `task.repair_exhausted` audit event.
- Transient failures still use the ordinary bounded retry budget;
  `repairable` never silently becomes a retry.

## 9. Deterministic final audit and release eligibility

- `factory/validation/final_audit.py` re-checks persisted facts only:
  approved plan status + canonical hash (and that the run's hash matches),
  release pipeline (`smoke` can never be eligible), completion of all
  non-auditor tasks, absence of uncertain/recovery-required state,
  evidence presence per completed task, and **latest** evidence file hash
  integrity against the workspace (latest per path — evidence is history;
  only the most recent recorded fact about a file must still be true).
- `AuditorAgent` is fail-closed: any failed check fails the task
  (`failure_kind="permanent"`); LLM output is not consulted for the
  decision at all. The audit report is still written for the human.
- Two distinct outcomes: `passed` decides the task; `release_eligible`
  additionally requires zero limitations. Legacy/unpersisted runs record
  limitations and can therefore never claim release eligibility.

## 10. Per-run model selection and live CLI evaluation

- `--model` on both run paths flows through the single model-resolution
  point (`ContextBuilder.default_model` → configured default). The
  configured default is never mutated; every call is audited in
  `llm_calls.model`.
- Live evaluation executed and recorded in
  [`docs/LOCAL_MODEL_CLI_EVALUATION.md`](LOCAL_MODEL_CLI_EVALUATION.md):
  - `qwen2.5-coder:7b-agent`: direct CLI tests A/B/C passed; full
    `plan create → approve → run --plan-id` smoke run passed (exit 0, 2/2
    audited LLM calls successful, `greet.py` written, evidence hash
    re-verified byte-for-byte).
  - `saluki-27b-iq2:latest`: blocked at model load — reproduces the
    Windows llama-server CUDA crash (`exit status 0xc0000409`, "shared
    object initialization failed"). One bounded attempt, no retries, no
    driver/Ollama changes; no integration run attempted; no quality
    conclusion drawn.
- The first live factory run exposed a real defect (`StateManager` wiring,
  §1 commit 7) that all deterministic tests had missed; it was fixed and
  covered by a regression test before the successful run.

## 11. Verification, invariants, and carried limitations

**Verification performed** (targeted per subsystem, per commit — the full
broad testing program was intentionally not re-run):

- Planning/migration/loader/projects suites: all green at commit 1.
- Startup-recovery/orchestrator/recovery-scenarios plus
  agents/pipeline/smoke suites: all green at commit 2.
- CLI lifecycle (isolated `FACTORY_DB_PATH` DB): create/show/approve/reject,
  refusal of pending/rejected/unknown/uncertain state, recovery resolution
  with actor, inspection commands; plus context-builder, agents,
  orchestrator, planning, e2e smoke, and pipeline integration regressions:
  all green at commits 3–4.
- Repair and audit suites (16 tests: allowed/disabled/bounded/invalidation/
  skip-protection/transient-separation, tamper, missing evidence, changed
  files, uncertain state, smoke, legacy limitations, auditor fail-closed)
  plus orchestrator/agents/startup-recovery/pipeline/e2e regressions: all
  green at commits 5–7.
- `python -m compileall -q factory tests` and `git diff --check`: clean
  before every commit. No test was weakened, no assertion removed, no
  `xfail` added, no held test touched.
- Live smoke evidence from §10 (isolated eval DB, repository DB untouched).

**Determinism and fail-closed invariants preserved:**

- Authorization, filesystem, retries, test outcomes, security, and release
  eligibility remain deterministic; model plans/explanations are proposals,
  never proof. The permission broker remains the mandatory OS boundary.
- Secret redaction, project-memory isolation, and fail-safe handling of
  unknown profile values are unchanged; security policies override
  profiles; `standard` plans always include non-negotiable security.
- No LLM can override a deterministic test failure, security rejection,
  denied approval, or exhausted budget; non-idempotent operations are never
  blindly retried; uncertain side effects stay recovery-required until a
  human resolves them.

**Known carried limitations (documented, unchanged):**

- Browser redirect validation retains the DNS-rebinding TOCTOU window.
- "connection refused" is deliberately non-retryable.
- Skill framing neutralization does not defeat Unicode homoglyph visual
  approximation.
- Legacy `factory run <path>` runs cannot claim release eligibility
  (limitations recorded by the auditor).
- Context injection is currently wired into the builder stage; other
  agents continue to receive structured artifacts by design.

## 12. Outstanding items reserved for the dedicated testing phase

1. The three held Ollama/live tests remain unauthorized and unexecuted:
   `test_real_ollama_minimal_inference`,
   `test_builder_hello_api_vertical_slice`,
   `test_prevent_secret_exfiltration`.
2. Full `standard` pipeline execution with a live model (QA/security/
   auditor/repair loop end-to-end) — only the bounded smoke path was
   evaluated here.
3. Saluki follow-up: blocked on this host at model load; requires a human
   environment decision (GPU/driver/backend compatibility). No automatic
   driver or Ollama changes; model files untouched.
4. Phase 2b human decisions still unresolved: pilot skill selection with
   license verification, final license allowlist/policy, Trivy pin/scanner
   approval.
5. Cross-platform (non-Windows) execution of the new CLI commands has not
   been tested on this host.
6. A live, adversarial model-driven security exercise (the held
   exfiltration test) remains the outstanding live-boundary proof.

# Dedicated Testing Report

Dedicated comprehensive testing phase for AI Software Factory.
Date of execution: 2026-10-17.

## 1. Repository state tested

- Branch: `fix/fail-closed-quality-gates`
- Commit: `28c81438eefc43a2fb39f2d848a050ce98822983` ("docs: reference architecture comparison and combined tranche status (Workstream C)")
- Working tree: clean before and after execution; `git diff --check` clean; local == origin.
- Preflight verified before any execution: branch name, HEAD SHA, clean status, diff-check.

Historical context: the implementation tranche reported 772 unit / 83
affected integration+e2e / 855 approved-subset passes. This phase ran the
COMPLETE eligible program, adding the 8 tests in
`tests/integration/test_skill_runtime.py` that had never been executed in
any prior phase. All historical results remain valid; this phase
supersedes them with full non-Ollama coverage.

## 2. Test inventory

`pyproject.toml` contains no pytest configuration (default settings, no
custom markers beyond the `integration` marker registered by
`tests/integration/conftest.py`). No root conftest; unit and integration
conftests only provide mock-LLM fixtures and markers — neither starts a
process, server, or service.

| Group | Files | Collected |
|---|---|---|
| Unit (`tests/unit/`) | 25 files (tools, permissions, orchestrator, agents, skills provider/bindings, intake x7, llm provider/sanitizer/opencode, sensitive data, recovery, projects, plan loader, context builder, structured output, tool proposals) | 772 |
| Integration (`tests/integration/`) | test_pipeline, test_intake_lifecycle, test_recovery_scenarios, test_skill_runtime, test_real_ollama (held), test_vertical_slice (held), test_adversarial_boundary (held) | 88 |
| E2E (`tests/e2e/`) | test_smoke | 6 |
| **Total** | | **866** |

External-process/network/model dependency audit (performed by source
inspection BEFORE execution):

- `tests/unit/conftest.py`: all agent tests use `_MockLLMRouter` — no live model.
- `tests/unit/test_tools.py` browser sections: headers state "no real
  network"; localhost/127.0.0.1/scheme/hostless URLs are blocked in
  validation before any I/O; the two "valid public URL" tests are blocked
  at the permission broker before fetch; all redirect tests patch
  `httpx.Client` with `_FakeClient`.
- `tests/unit/test_llm_provider.py`, `test_llm_sanitizer.py`: request
  construction and redaction only; no transport calls.
- `tests/unit/test_opencode_provider.py`: `manage_server=False`,
  `complete()` fully mocked; `socket` use is a local ephemeral bind to
  test port-detection logic (loopback only, no outbound connection).
- `tests/integration/test_recovery_scenarios.py::test_network_timeout_recovery`:
  constructs a real `OllamaProvider` object but the first
  `httpx.Client.send` raises a mocked `ReadTimeout`, so no live request
  is ever made (the provider does not retry internally).
- `tests/e2e/test_smoke.py`: `LLMRouter.complete` is patched with a stub
  response for every router invocation; orchestrator executors are
  registered fakes.
- `tests/integration/test_skill_runtime.py`: header states "no network,
  no LLM calls, no Ollama"; all DBs/workspaces under `tmp_path`.

Conclusion: only the three held tests require a live model/server. All
other tests are safe to execute with Ollama stopped and were executed.

## 3. Exact commands executed

```
python -m pytest tests\unit --collect-only -q                        # 772 collected
python -m pytest tests\integration --collect-only -q                 # 88 collected
python -m pytest tests\e2e --collect-only -q                         # 6 collected
python -m pytest tests\unit -q                                       # Phase B.1
python -m pytest tests\integration\test_pipeline.py tests\integration\test_intake_lifecycle.py tests\integration\test_recovery_scenarios.py tests\integration\test_skill_runtime.py -q   # Phase B.2
python -m pytest tests\e2e\test_smoke.py -q                          # Phase B.3
python -m compileall -q factory tests                                # Phase E
git diff --check                                                     # Phase E
```

All runs used `$env:TEMP/$env:TMP/$env:TMPDIR =
C:\Users\Heet Vithalani\AppData\Local\Temp\opencode\pytest-tmp` (required
writable temp directory for this Windows environment).

## 4. Test counts and results

| Suite | Executed | Passed | Failed | Skipped by condition | Excluded (held) | Blocked |
|---|---|---|---|---|---|---|
| Unit | 772 | 772 | 0 | 0 | 0 | 0 |
| Integration (eligible) | 85 | 85 | 0 | 0 | 3 | 0 |
| E2E | 6 | 6 | 0 | 0 | 0 | 0 |
| **Total** | **863** | **863** | **0** | **0** | **3** | **0** |

- Runtime: unit 24.83s, integration 21.03s, e2e 3.12s.
- `compileall`: OK. `git diff --check`: OK. Working tree: clean.
- No test printed a skip; the 11 pre-existing `pytest.skip` guards are
  all platform-conditional (Windows-vs-POSIX paths, symlink privileges)
  and none triggered on this Windows host (e.g. symlink creation
  succeeded, so `test_symlinked_skill_md_rejected` genuinely executed).
- No `xfail` markers exist anywhere in the test tree.

## 5. Failures, causes, and resolutions

**None.** Zero failures occurred across all 863 executed tests, so no
defect triage (Phase C) was required and no product, test, or
environmental defects were found or fixed. No test was modified,
weakened, skipped, or marked during this phase. The only file created is
this report.

## 6. Security and regression coverage

Coverage of the Phase D checklist, by verification level:

| Boundary | Level | Where executed |
|---|---|---|
| Shell injection / unsupported operators (SEC-002) | deterministic unit | `test_tools.py::TestShellTool`, `TestShellNonShellExecution` (quote-aware parser, operator refusal, argv passthrough) |
| Exact-operation approval binding (SEC-006) | deterministic unit + mocked integration | `test_permissions.py` (fingerprint inheritance), `test_orchestrator.py` (NULL/tampered fingerprint never resumes), `test_pipeline.py` approvals CLI |
| Sensitive-data redaction / logging boundaries (SEC-008) | deterministic unit | `test_llm_sanitizer.py` (32 tests), `test_sensitive_data.py` |
| Filesystem sandbox / symlink / junction (SEC-001, SEC-005) | deterministic unit (Windows primitives executed live) | `test_tools.py::TestBaseSandbox` (junction/symlink escapes), `test_intake_archive.py` |
| Browser redirects / prohibited destinations (SEC-003) | deterministic unit (mocked transport) | `TestBrowserToolValidation` (loopback/scheme blocks), `TestBrowserRedirects` (3-hop budget, cross-host revalidation, private-target refusal) |
| Git workspace / argument boundaries (SEC-004) | deterministic unit | `TestGitToolBoundaryAndArgs`, `TestGitToolMutations` |
| Fail-closed quality gates (COR-001/COR-002/COR-007) | deterministic unit | `test_tools.py::test_run_tests_no_tests_collected` (rc=5 is failure), `test_agents.py` QA/security verdict-vs-scan tests |
| Bounded retry / exhaustion (COR-004) | deterministic unit | `test_orchestrator.py::TestBoundedRetries` (transient/permanent/unknown, budget, backoff, escalate/skip) |
| Approval pause / grant / denial / re-execution (COR-003) | deterministic unit | `test_orchestrator.py::TestAwaitingApprovalPauseResume`, `TestApprovalPropagation`, `test_agents.py::TestBuilderApprovalPause` |
| Project isolation / skill-context integrity | deterministic unit + mocked integration | `test_skill_provider.py` (42), `test_skill_bindings.py` (32), `test_skill_runtime.py` (8, end-to-end CLI wiring) |
| Evidence completeness / audit consistency | deterministic unit + mocked integration | orchestrator audit-payload assertions, agent evidence tests, intake audit tests, `test_recovery_scenarios.py` |

Explicit limitation: this coverage is source-level, deterministic-unit,
and **mocked**-integration/stubbed-e2e evidence. It does NOT prove live
runtime security (no live model, no live sandbox escape attempt against a
real inference loop). That proof depends on the three held tests in
section 9.

## 7. Skipped, held, excluded, or blocked tests

Held by explicit user instruction (3 — never executed, never collected
into a run):

1. `tests/integration/test_real_ollama.py::test_real_ollama_minimal_inference`
   — requires a running Ollama with `qwen2.5-coder:7b`.
2. `tests/integration/test_vertical_slice.py::test_builder_hello_api_vertical_slice`
   — spawns `python -m factory run` end-to-end against a live model. The
   file contains ONLY this test.
3. `tests/integration/test_adversarial_boundary.py::test_prevent_secret_exfiltration`
   — spawns `python -m factory run` instructing an outbound-sandbox
   write; the file contains ONLY this test.

Excluded: none beyond the three held tests. Blocked by environment: none.
Skipped by existing conditions: none triggered.

## 8. Remaining defects and risk assessment

No defects were identified in this phase. Known, pre-existing,
documented limitations carried forward (not defects introduced or
ignored):

- Roadmap task 3.3 (RecoveryPlan consumption on boot) — deliberately
  deferred, out of scope by instruction.
- Browser redirect validation: DNS-rebinding TOCTOU window between
  hostname validation and connection (documented in SEC-005 tranche).
- "connection refused" is classified non-retryable by design.
- Skill framing neutralization is byte-exact and case-insensitive but
  does not defeat Unicode homoglyph visual approximation (machine
  boundaries themselves cannot be forged).

Risk assessment: the deterministic and mocked layers are fully green and
cover every enforcement boundary. The residual risk is concentrated in
live-runtime behavior (real model driving real tools) which remains
unverified pending the held tests.

## 9. Future plan for the three held Ollama tests

Run only after (a) Saluki testing is complete, (b) the user explicitly
authorizes, and (c) an Ollama server with `qwen2.5-coder:7b` is confirmed
running. Suggested order and purpose:

1. `test_real_ollama_minimal_inference` — cheapest; validates the
   provider transport and structured-output path against the live model.
2. `test_builder_hello_api_vertical_slice` — full CLI-driven builder loop;
   validates agent/tool/permission wiring under a real model (180s
   timeout).
3. `test_prevent_secret_exfiltration` — highest security value; asserts
   a real model cannot write outside the sandbox (file byte-identity
   before/after) and the run terminates cleanly (90s timeout).

Each should be run individually with output captured before running the
next, since live-model behavior is non-deterministic; a failure should be
triaged as model-behavior vs product-defect before any code change.

## 10. Readiness recommendation

**Ready for further evaluation.** 863/863 eligible tests pass with zero
skips, zero xfails, and zero failures; every Phase D security boundary
has deterministic or mocked-integration coverage; compilation and diff
checks are clean. This is NOT a production-readiness claim: live-model
runtime behavior, the three held tests, the deferred RecoveryPlan boot
work, and the documented TOCTOU/homoglyph limitations remain open. The
held tests require explicit user authorization and are not auto-authorized
by this report.

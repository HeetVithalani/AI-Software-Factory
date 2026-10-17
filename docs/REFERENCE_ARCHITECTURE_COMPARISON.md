# Reference Architecture Comparison

Research artifact for Workstream C of the combined implementation tranche
(branch `fix/fail-closed-quality-gates`, base `d16f885`).

Date of research: 2026-10-17.

## Purpose and method

This document compares four open-source coding-agent projects against the
AI-Software-Factory architecture to extract adoptable ideas and to identify
where our deliberately different principles must be defended. It is research
only: no code was copied from any reference repository, and nothing in this
document commits the project to an implementation.

Method:

- Each project was pinned to the exact default-branch HEAD commit at research
  time (table below). Commits and dates were read from the GitHub API.
- Factual claims marked "(verified)" were taken from the repository README,
  the repository's own documentation, or the project's live documentation
  pages at/near the pinned commit.
- Claims marked "(background)" are established general knowledge about the
  project that was not re-verified against the pinned commit; treat them as
  directional, not citable.
- License and org/URL facts were read from the GitHub API repository record.

## Pinned repositories

| Project | Repository (at pin) | License | Primary language | Pinned commit (default branch) | Commit date | Status notes |
|---|---|---|---|---|---|---|
| OpenHands | `OpenHands/OpenHands` (moved from `All-Hands-AI/OpenHands`) | MIT | TypeScript | `045290739f0af2ae4cc89d2a209c6bd930bb81ee` (`main`) | 2026-10-16 | Self-described beta |
| SWE-agent | `SWE-agent/SWE-agent` (moved from `princeton-nlp/SWE-agent`) | MIT | Python | `3ea751c087f32b16e039a2233dd6eefecef325d5` (`main`) | 2026-07-16 | Superseded by `mini-swe-agent` per its own README |
| OpenCode | `anomalyco/opencode` (moved from `sst/opencode`) | MIT | TypeScript | `22e771f5893e255ea78fc10557ed3fb98d3a1072` (`dev`) | 2026-10-17 | Active; this is also the harness running this research |
| Continue | `continuedev/continue` | Apache-2.0 | TypeScript | `5522c6f44ca0ac3528b37244818fbfa39b5af470` (`main`) | 2026-07-21 | Read-only; no longer actively maintained; final 2.0.0 shipped |

## 1. OpenHands

### Identity (verified at pin)

"Agent Canvas", described in its README as a self-hosted developer control
center for coding agents and automations. It runs the OpenHands agent and
other agents (Claude Code, Codex, Gemini, Pi, OpenCode, or any
ACP-compatible agent) across local, remote, and cloud backends. Beta status;
shipped as the `@openhands/agent-canvas` npm package plus desktop apps.

### Execution model (verified, live docs at research time)

The frontend connects to interchangeable backends. A backend provides an
Agent Server (runs conversations and tools inside a workspace: a folder,
mounted project directory, container, or cloud sandbox) and, when
automations are enabled, an Automation Server (schedules, events, run
lifecycle). Recommended setups: default local (a local backend is created
automatically), self-hosted VM/container, cloud/enterprise, and managed
provider sandboxes (e.g. DigitalOcean, which pause when idle). Backend
settings, LLM configuration, MCP servers, and automations are scoped to the
active backend.

Historically (background): a Python monolith with a Docker-based runtime,
an event-stream architecture between frontend/server/agent, and
"microagents" (markdown instruction files loaded by trigger).

### Permission and safety model (background)

Confirmation/approval settings exist for command execution, and
sandbox/container isolation is the primary containment mechanism. There is
no persisted, re-verified approval ledger equivalent to an
exact-operation fingerprint; approvals are session-configuration-driven.

### Relevance to AI-Software-Factory

- The backend abstraction (local folder vs container vs cloud sandbox behind
  one control plane) is the cleanest reference for a future "workspace
  backend" interface. Any Phase 5 remote-execution expansion is explicitly
  out of scope for the current tranche; this is recorded as a reference only.
- Running multiple third-party agents through one control plane validates
  our agent-executor registration model (the Orchestrator dispatches by
  `agent_type` and does not care what the executor is).
- Its per-session, config-driven approvals contrast with our deterministic,
  DB-persisted, fingerprint-re-verified approvals. We keep ours.

## 2. SWE-agent

### Identity (verified at pin)

Research-oriented Python agent that takes a GitHub issue and attempts to
fix it with a chosen language model; built around the SWE-benchmark
ecosystem (arXiv 2405.15793, NeurIPS 2024). Its README at the pinned commit
warns that most development effort has moved to `mini-swe-agent`, which it
says matches SWE-agent's performance while being much simpler, and
recommends mini-swe-agent for most uses.

### Execution model (background)

Runs inside per-task containers or VMs; configuration is YAML-driven
(model, environment, agent settings); ships an evaluation harness for
benchmarks.

### Design idea: the Agent-Computer Interface (background)

SWE-agent's core contribution is the Agent-Computer Interface (ACI):
tool surfaces are deliberately designed and iterated as if they were a
user interface - a bash tool, a file viewer that presents syntax/lint
context, and edit commands that receive lint feedback. The companion
mini-swe-agent reduces this to a bash-only tool surface of roughly a
hundred lines of core logic.

### Relevance to AI-Software-Factory

- The ACI idea supports our minimal, deliberate tool surface (shell, git,
  browser, filesystem, testing, security) rather than "expose the whole
  OS" toolkits. mini-swe-agent's bash-only minimalism is a useful
  counterweight whenever tool-count pressure builds.
- Benchmark-first evaluation culture maps to our held integration tests and
  the dedicated testing phase; it does not change our current test plan.

## 3. OpenCode

### Identity (verified at pin)

"The open source AI coding agent" - a CLI/TUI agent with a desktop app,
MIT-licensed, TypeScript, very large community. Built-in agents: `build`
(full access), `plan` (read-only; denies edits, asks before bash), plus an
internal `general` subagent invocable with `@general`.

### Permission model (verified from opencode.ai/docs/permissions, last
updated 2026-10-16)

- Every tool action resolves to `allow`, `ask`, or `deny` via a
  `permission` config in `opencode.json`.
- Granular object syntax: per-tool wildcard patterns (`*`, `?`), last
  matching rule wins; bash rules match parsed commands (e.g. `git status
  --porcelain`), not raw strings.
- Distinct permission keys per tool: read, edit, glob, grep, bash, task,
  skill, lsp, question, webfetch, websearch, plus two safety guards:
  `external_directory` (tool touches paths outside the working directory)
  and `doom_loop` (same tool call repeated 3x with identical input).
- Defaults are permissive (`allow`), with `doom_loop` and
  `external_directory` defaulting to `ask`, and `.env` files denied for
  read by default.
- "Ask" offers three outcomes: `once`, `always` (session-scoped pattern
  whitelist suggested by the tool, e.g. `git status*`), `reject`.
- `--auto` auto-approves everything not explicitly denied; explicit `deny`
  still applies.
- Permissions can be overridden per agent (config or markdown agent files).

### Relevance to AI-Software-Factory

OpenCode is the closest operational neighbor to our harness, and several
mechanisms deserve explicit comparison:

- Parsed-command permission patterns vs our shell quote-aware parser: we
  already tokenize shell commands (SEC-002); pattern rules like `git *`
  are a natural config layer on top of the broker and were noted as a
  candidate follow-up, not adopted in this tranche.
- The `doom_loop` guard is a deterministic repeat-detection circuit
  breaker. Our equivalent levers are bounded retries (COR-004), turn
  limits, and the DAG terminal-failure markers; a repeat-identical-action
  guard is a candidate future hardening item.
- The `always` grant semantics (session-scoped, tool-suggested patterns)
  resemble our scope inheritance (SEC-006) - except ours is
  DB-persisted and bound to a re-verified operation fingerprint rather
  than session memory.
- `external_directory` mirrors our workspace-sandbox boundary (SEC-004 git
  boundary, SEC-005 path checks); our boundary enforcement is fail-closed
  in the tool broker rather than per-rule configuration.
- `--auto` (auto-approve everything not denied) conflicts with our
  fail-closed stance and is deliberately not adopted.

## 4. Continue

### Identity (verified at pin)

The pinned README states the repository is no longer actively maintained
and is read-only for all users; a final 2.0.0 shipped for the VS Code
extension, CLI, and JetBrains plugin, including removal of anonymous
telemetry and of authentication. Apache-2.0, TypeScript.

### Relevance to AI-Software-Factory

Continue is a historical reference rather than an adoption source:

- Its configuration model (YAML/JSON blocks, rules, MCP support, many
  providers including local models) parallels our config-driven providers
  and skills-as-markdown stance, one generation earlier.
- The removal of telemetry/authentication in the final release matches the
  project's local-first, no-phone-home principles.
- No adoption candidates beyond reinforcement of existing direction.

## Cross-cutting comparison

| Dimension | OpenHands | SWE-agent | OpenCode | Continue | AI-Software-Factory |
|---|---|---|---|---|---|
| Primary setting | Self-hosted control plane, multi-backend | Benchmark/research harness | Interactive CLI/TUI + desktop | IDE extension + CLI | Local-first CLI factory pipeline |
| Execution isolation | Container / mounted folder / cloud sandbox (verified) | Container/VM per task (background) | Host workspace + permission gates | IDE workspace | Workspace sandbox checks + broker; no container requirement |
| Approval model | Session config confirmation settings (background) | Mostly autonomous in-sandbox | `allow/ask/deny` rules, session `always`, `--auto` (verified) | Optional local approval prompts (background) | Deterministic broker; DB-persisted decisions; exact-operation fingerprint re-verified on resume (SEC-006, COR-003) |
| Fail-closed defaults | Sandbox-first | Sandbox-first | Permissive defaults + guards | Permissive | Fail-closed: unknown failure kinds non-retryable; unverified approvals never resume |
| Quality gates | LLM-driven with tests | Benchmark harness externally | None built in | None built in | Deterministic scans/tests decide; LLM verdicts informational (COR-002/COR-007) |
| Extension model | Plugins, ACP agents, microagents (background) | YAML config | Plugins, skills, MCP, ACP, custom tools | Blocks, rules, MCP | Skills are inert Markdown by principle; tools brokered |
| Provider stance | Multi-provider, cloud-first backends | Any LM (incl. local via mini) | Multi-provider incl. local | Multi-provider incl. local | Ollama-first, local-first, provider fallback chain |
| Retry/loop safety | Run lifecycle management | Harness-bounded | `doom_loop` guard (verified) | n/a | Bounded deterministic retries (COR-004), declared budgets, terminal markers (COR-008) |

## Adoptable ideas (candidates only - none adopted by this tranche)

1. Parsed-command permission patterns (`git *`, `rm *`) as a config layer
   over the existing shell broker checks.
2. Repeat-identical-action detection ("doom_loop" analogue) as a
   deterministic guard next to bounded retries.
3. A workspace-backend interface (folder vs sandbox behind one executor
   contract) if and only if Phase 5 remote execution is ever approved -
   explicitly out of scope now.
4. ACP (Agent Client Protocol) as a possible future interop surface for
   running third-party agents under our orchestrator - reference only.

## Explicit non-adoptions

- Auto-approve modes (`--auto` and equivalent): conflicts with fail-closed
  gating; approvals stay human- and DB-driven.
- Cloud/managed sandbox dependence: conflicts with local-first,
  Ollama-compatible operation.
- Continue: read-only upstream; treated as a historical reference.
- No wholesale copying: all future adoption must be re-derived against our
  own NON_NEGOTIABLE principles and tested independently.

## Verification limitations

- Live documentation pages (OpenCode permissions, OpenHands backends) are
  not commit-pinned; they were current as of 2026-10-16/17 and could drift
  from the pinned commits.
- Claims marked (background) were not re-verified at the pinned commits.
- Repository renames (All-Hands-AI -> OpenHands, princeton-nlp ->
  SWE-agent, sst -> anomalyco) mean old URLs redirect; the pinned table
  lists the canonical locations at research time.

# AI Software Factory Codebase Structure Summary

## Top-Level Directory Structure

The project at `D:\AI-Software-Factory` has the following top-level directories and files:

```
.git/              # Git repository
.gitignore         # Git ignore patterns
.pyet_cache/       # Python cache
.venv/             # Virtual environment
artifacts/         # Generated artifacts
backups/           # Backup files
cache/             # Cache directory
configs/           # Configuration files
engine/            # Engine module
factory-backup-before-git-policy/
factory.db         # SQLite database
knowledge/         Knowledge base
logs/              # Log files
Modelfile.qwen25-agent
Modelfile.qwen3-agent
models/            # Model files
opencode.json
opencode.qwen3-backup.json
plan.json          # Generated project plan
projects/          # Project directories
pyproject.toml     # Project configuration
tests/             # Test directory
temp/              # Temporary directory
```

## factory/ Package Structure

### Core Data Models (`factory/models.py`)
- **Enumerations**: `TaskStatus`, `ProjectStatus`, `RiskLevel`, `ApprovalStatus`, `QualityProfile`, `ModelProvider`, `RecoveryStrategy`
- **ProjectRecord**: Persistent project state with UUID, name, description, status, quality profile, metadata
- **TaskRecord**: Persistent task state with UUID, project_id, name, description, agent_type, status, risk_level, dependencies, retry configuration
- **ApprovalRequest/Decision**: Human approval workflow models
- **CheckpointRecord**: State snapshots before/after risky operations
- **ModelRecord/Capability**: Model registry with capability profiles
- **AgentResult**: Structured output from any agent invocation

### Agents (`factory/agents/`)

#### `factory/agents/base.py` - BaseAgent
- Abstract base class for all factory agents
- Defines the `run(task: TaskRecord) -> AgentResult` interface
- Provides helper methods: `_make_filesystem_tool()`, `_make_shell_tool()`, `_make_git_tool()`, `_make_testing_tool()`, `_make_security_tool()`
- Convenience methods: `_ok()`, `_fail()`, `_artifact_path()`, `_emit()`
- Agents are stateless between runs, communicating through artifact files

#### `factory/agents/builder.py` - BuilderAgent
- Uses LLM to write source files to the workspace
- Runs an LLM loop proposing tool calls (shell, filesystem)
- Maximum 50 turns, typically limited to 15
- Proposals must match `BuilderProposal` schema
- Supports filesystem.write, filesystem.read, shell.run actions
- On completion, writes summary with written/failed files count

#### `factory/agents/planner.py` - PlannerAgent
- Decomposes project description into task DAG
- Reads project metadata from TaskRecord
- Writes `plan.json` artifact to workspace
- Default deterministic pipeline with keyword-based task augmentation
- Keyword triggers: "api" → API Integration Test, "database" → Database Migration Check
- Output format: `plan.json` with project_id, description, tasks array
- Each task has: task_id, name, agent_type, depends_on, description, metadata

### LLM System (`factory/llm/`)

#### `factory/llm/provider.py` - LLM Abstraction
- **LLMRequest**: Request payload (model, system_prompt, messages, output_schema, metadata)
- **LLMResponse**: Response returned from provider (content, telemetry, success/failure, structured_data)
- **LLMProvider**: Abstract base class with `complete()` method

#### `factory/llm/router.py` - LLMRouter
- Routes LLM requests to appropriate provider
- Enforces single inference at a time via semaphore (VRAM protection)
- Routing rules by model name prefix:
  - "ollama:..." → OllamaProvider
  - "opencode:..." → OpenCodeProvider (if enabled)
  - anything else → OllamaProvider (fallback)
- Sanitizes requests before execution
- Returns immediate failure if OpenCode is disabled

#### `factory/llm/ollama.py` - OllamaProvider
- Implements LLMProvider using Ollama's HTTP API
- Uses httpx for synchronous requests to `/api/chat`
- Supports output_schema via `format` parameter
- Handles timeout, HTTP errors, and JSON parsing
- Extracts structured_data from response if output_schema provided

#### `factory/llm/structured_output.py` - Structured Output
- **extract_json_from_text()**: Finds first valid JSON from text (handles markdown fences, prose, trailing commas)
- **_try_parse_json()**: Attempts JSON parse, tries trailing comma removal
- **_extract_balanced()**: Finds outermost balanced braces/brackets
- **parse_structured_response()**: Extracts JSON and validates against Pydantic model
- **parse_structured_data()**: Validates pre-parsed dict against Pydantic model
- **StructuredOutputError**: Exception raised on parse/validation failure

#### `factory/llm/schemas/` - Pydantic Schemas
- `__init__.py`: Package init (no content)
- **builder.py**: `BuilderProposal` - summary, files_changed, proposed_actions, blockers, remaining_work, completed
- **planner.py**: `PlannerTask`, `PlannerProposal` - summary, decisions, assumptions, tasks list
- **tool_call.py**: `ToolCallProposal` - action_type, tool, arguments, reason, expected_effect; `to_action_context()` method
- **auditor.py**: `AuditorProposal` - summary, requirements_met, evidence, approved
- **reviewer.py**: `ReviewDefect`, `ReviewerProposal` - summary, defects, approved, risks
- **security.py**: Empty (no schema defined)
- **documentation.py**: Empty (no schema defined)
- **qa.py**: Empty (no schema defined)

### Tools (`factory/tools/`)

#### `factory/tools/base.py` - BaseTool & ToolResult
- **ToolResult**: Structured result (success, output, error, tool_name, command, duration_ms, returncode, evidence_hash, metadata, produced_at)
- **BaseTool**: Abstract base for all tools
  - `_sandbox_path()`: Path validation within workspace
  - `_check_permission()`: Broker permission checks
  - `_audit_emit()`: Audit event emission
  - `_ok/_fail()`: Convenience result builders

#### `factory/tools/filesystem.py` - FilesystemTool
- Sandboxed filesystem I/O operations
- **read()**: Reads file with sensitive data gate and size guard
- **write()**: Writes file with risk classification and approval gating
- **delete()**: Deletes file (always HIGH risk, requires approval)
- **list_dir()**: Directory listing (SAFE operation)
- **exists()**: Path existence check
- **read_json()/write_json()**: JSON I/O helpers

#### `factory/tools/shell.py` - ShellTool
- Sandboxed subprocess execution
- Risk classification via RiskEngine
- Permission broker approval for non-SAFE/LOW risk commands
- Timeout protection (default 60s)
- `run_safe()`: Skip broker check for known-safe commands
- Critical patterns blocked: `rm -rf`, `format`, `dd`, `> /dev/`, sudo, chmod 777, etc.
- Safe prefixes: `python`, `pytest`, `pip`, `npm`, etc.

#### `factory/tools/git.py` - GitTool
- Git operations with mutation gating
- **Read-only** (always safe): status, log, diff, show, branch, tag, etc.
- **Mutation** (requires approval): commit, push, merge, rebase, reset, etc.
- **Critical** (fresh approval, no scope inheritance): push --force, reset --hard, clean -f
- **Add** (MEDIUM risk, requires approval)
- Scope-key based approval tracking

#### `factory/tools/testing.py` - TestingTool
- Runs pytest within workspace sandbox
- Parses summary output into structured TestResult
- Supports test_path restriction and extra_args
- `run_single_test()`: Run individual test by node ID

#### `factory/tools/browser.py` - BrowserTool
- Read-only HTTP GET fetcher
- Blocks private/reserved IP ranges (loopback, RFC1918, link-local)
- Blocks metadata endpoints (169.254.0.0/16)
- Always requires broker approval (HIGH risk)
- Response capped at 1 MB
- User-Agent: `AI-Software-Factory/0.1 (read-only research)`

### Permissions (`factory/permissions/`)

#### `factory/permissions/broker.py` - PermissionBroker
- Single entry-point for all action authorization
- Integrates 4 subsystems: PolicySet, RiskEngine, SensitiveDataDetector, ApprovalManager
- **PermissionOutcome**: APPROVED, APPROVAL_REQUIRED, DENIED
- **PermissionDecision**: Value object with outcome, risk_level, approval_request, reason, inherited_from, metadata
- Full permission pipeline:
  1. Risk classification via RiskEngine
  2. Sensitive data escalation to CRITICAL
  3. Non-negotiable policy blocks
  4. Approval requirement check
  5. Scope inheritance check
  6. New approval request if needed

#### `factory/permissions/policies.py` - PolicySet
- **NON_NEGOTIABLE policies**: cannot be weakened (require_approval_git_mutations, require_approval_sensitive_data, block_sensitive_to_remote, production_deployment_forbidden)
- **PolicySet dataclass**: configurable policy flags
- `apply_override()`: Apply overrides, blocks non-negotiable weakening
- `load_policies()`: Load from policies.yaml

#### `factory/permissions/risk.py` - RiskEngine
- Deterministic risk classification (no LLM)
- **RiskLevel**: SAFE, LOW, MEDIUM, HIGH, CRITICAL
- Git subcommand classification: read-only (SAFE), high-risk (HIGH), critical (CRITICAL)
- Shell command pattern matching: critical patterns (rm -rf, format, sudo, etc.), high-risk (npm publish, AWS calls, curl|bash, etc.)
- File write classification: sensitive → CRITICAL, safe extensions → LOW, others → MEDIUM
- File read: sensitive path → CRITICAL, otherwise SAFE
- File delete: always HIGH
- Network: always HIGH
- Knowledge with high impact: HIGH, otherwise LOW

#### `factory/permissions/sensitive_data.py` - SensitiveDataDetector
- Deterministic pattern-based detection (no LLM)
- **SensitivityReport**: is_sensitive, reasons, severity
- **check_path()**: File path analysis (filename patterns, path segments)
- **check_content()**: Content regex matching (private keys, cloud credentials, database URLs, etc.)
- **check_file()**: Combined path + content check
- **is_sensitive_path()**: Quick boolean check

#### `factory/permissions/approvals.py` - ApprovalManager
- Scoped, persistent, auditable approval lifecycle
- **_make_scope_key()**: Deterministic scope key from category + target
- **_ScopeEntry**: Tracks approved scope for inheritance lookup
- **request_approval()**: Create pending ApprovalRequest
- **record_decision()**: Persist human decision (APPROVED/DENIED)
- **find_inherited()**: Check for valid inherited approval
- **Scope inheritance**: Only valid when action_category, scope_key match AND risk ≤ approved ceiling
- Persistence in SQLite database

### Orchestration (`factory/orchestration/`)

#### `factory/orchestration/state.py` - OrchestratorStatus
- Status enum: IDLE, RUNNING, PAUSED, STOPPED, EMERGENCY_STOP, RECOVERING, AWAITING_APPROVAL

#### `factory/orchestration/dag.py` - TaskDAG
- TaskNode: Single DAG node with task_id, name, agent_type, depends_on, status, metadata
- TaskDAG: Directed acyclic graph with:
  - add/remove nodes
  - update_status()
  - is_complete(), is_terminal()
  - has_unrecoverable_failure()
  - ready_tasks() (PENDING with all deps completed)
  - validate() (cycle detection via DFS, dependency existence)
  - topological_order() (Kahn's algorithm)

#### `factory/orchestration/scheduler.py` - Scheduler
- Resource-aware task scheduler
- **ResourceBudget**: Tracks RAM (16GB total, 10GB inference) and VRAM (6GB total, 5.5GB inference)
- **can_allocate()**: Check if requested resources fit within headroom
- **allocate/release**: Manage resource reservations
- **can_schedule()**: Check concurrency limit + resource budget
- **max_concurrent**: Default 2 (prevents VRAM exhaustion)
- **ScheduledTask**: Tracks allocated resources per task

#### `factory/orchestration/orchestrator.py` - Orchestrator
- Main execution controller (the "kernel loop")
- Holds: DAG, Scheduler, StateManager, RecoveryManager, PermissionBroker, AuditLogger
- **AgentExecutor** type: `Callable[[TaskRecord], AgentResult]`
- **OrchestratorConfig**: max_concurrent, poll_interval, checkpoint_every_n
- **register_executor(agent_type, fn)**: Register agent executors
- **emergency_stop()**: Immediate halt, blocks new dispatch
- **pause/resume/clear_emergency_stop()**: State management
- **_check_stop_signals()**: File sentinel + internal flag
- **_dispatch_task()**: Dispatch to registered executor
- **step()**: One scheduling step - dispatch ready tasks up to concurrency limit
- **run()**: Run loop until completion, stop, or max_steps (default 10,000)
- **snapshot()**: JSON-safe state snapshot

#### `factory/orchestration/recovery.py` - RecoveryManager
- Checkpoint/recovery foundation
- **Checkpoint**: In-memory snapshot (id, project_id, task_id, created_at, state, hashes)
- **RecoveryPlan**: Recovery decision data (safe_to_continue, completed/uncertain/tainted task lists, issues)
- **save_checkpoint()**: Persist snapshot to DB with optional file hashes
- **load_last_checkpoint()**: Load most recent valid checkpoint
- **invalidate_checkpoint()**: Mark checkpoint as untrusted
- **recover()**: Inspect DB + workspace, produce recovery plan
- On crash recovery: re-reads task DB records, cross-checks workspace hashes, determines which tasks are safe to continue

### LLM Schemas (summary)
The `factory/llm/schemas/` directory contains Pydantic models for structured LLM output:
- **BuilderProposal**: Controls builder agent actions
- **PlannerProposal**: Controls planner agent task decomposition
- **ToolCallProposal**: General tool call proposals (git commit, shell commands, etc.)
- **AuditorProposal**: Final approval decision
- **ReviewerProposal**: Code review decisions
- **AuditorProposal**, **ReviewerProposal**, **security.py**, **documentation.py**, **qa.py**: Some schemas are empty (placeholders or not yet implemented)

## Key Design Principles

1. **Stateless agents**: Read inputs from artifacts, write outputs to artifacts
2. **Permission-first**: Every action gated through RiskEngine + PermissionBroker
3. **Deterministic risk**: Risk engine uses pattern matching, not LLM judgments
4. **Structured output**: LLM responses validated against Pydantic schemas
5. **Approval workflow**: Human-in-the-loop for risky operations
6. **Resource awareness**: Scheduler limits concurrent tasks and VRAM usage
7. **Crash recovery**: Checkpoint-based recovery never blindly trusts state
8. **Sandboxed I/O**: All filesystem paths validated within workspace_root
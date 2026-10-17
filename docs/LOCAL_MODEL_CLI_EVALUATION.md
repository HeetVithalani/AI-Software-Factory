# Local Model CLI Evaluation

Bounded local-model evaluation executed 2026-10-17 as part of the final
implementation milestone on branch `fix/fail-closed-quality-gates`.

**Purpose.** Compare the two locally installed candidate models —
`saluki-27b-iq2:latest` and `qwen2.5-coder:7b-agent` — through direct Ollama
CLI prompts and through real `factory` CLI runs on disposable projects, and
record exactly what happened, including failures.

**What this is not.** This is not a model-quality benchmark (three short
prompts, one run each, non-deterministic models), not a production-readiness
claim, and not a basis for changing the factory's default model. No default
was changed; Saluki is not a default; no model files, manifests, Ollama
settings, or drivers were modified.

## 1. Environment

| Item | Value |
|---|---|
| Host | Windows (win32), PowerShell |
| Python | 3.12 (system) |
| Ollama server | default `http://localhost:11434` (already running) |
| Models installed | `saluki-27b-iq2:latest` (7.9 GB), `qwen2.5-coder:7b` (4.7 GB), `qwen2.5-coder:7b-agent` (4.7 GB) |
| Evaluation commit | `36011ad` (factory working tree) |
| Sandbox | `%TEMP%\opencode\model-eval\` (disposable; never committed) |

All model usage was strictly sequential (one model resident at a time), as
required.

## 2. Direct Ollama CLI tests

Definition of the three bounded tests:

- **Test A — minimal inference:** `Reply with exactly one word: READY`.
- **Test B — structured-ready output:** return only a JSON object
  `{"ok": true, "tool": "filesystem.read", "path": "main.py"}`.
- **Test C — small code generation:** the `add(a, b)` Python function.

Exact command shape (per test, one prompt, output captured, wall time
recorded):

```
ollama run <model> "<prompt>"
```

### 2.1 `qwen2.5-coder:7b-agent` — passed all three

| Test | Wall time | Result |
|---|---|---|
| A | 11 s (cold load) | Printed exactly `READY`. Exit 0. |
| B | 2 s | Valid JSON object **wrapped in a ```json fence** despite "no code fences". Exit 0. |
| C | 1 s | Correct `def add(a, b): return a + b`, wrapped in a ```python fence. Exit 0. |

Observation: raw CLI output tends to be fenced. This did **not** affect the
factory integration — see section 3.3.

### 2.2 `saluki-27b-iq2:latest` — blocked at model load (crash)

Single deliberate attempt (no retries by instruction). Test A, 28 s, exit
code 1, **no model output produced**. The Ollama CLI reported:

```
Error: 500 Internal Server Error: llama-server process has terminated:
exit status 0xc0000409: The system detected an overrun of a stack-based
buffer in this application. ... : CUDA error: shared object initialization
failed
```

- `0xc0000409` is `STATUS_STACK_BUFFER_OVERRUN`; the crash occurs in the
  `llama-server` backend during CUDA "shared object initialization" — i.e.
  while the model backend loads, before any prompt is processed.
- This reproduces the previously recorded Saluki CUDA-related crash on this
  Windows host. No driver, Ollama installation, model file, or manifest was
  modified, and no further Saluki attempts were made.
- Tests B and C were not attempted, and **no factory CLI run was attempted
  with Saluki**: the same backend loads the model for the HTTP API, so a
  factory run would fail identically at the first inference.
- Post-crash sanity: the Ollama daemon survived; `ollama list` responded and
  `qwen2.5-coder:7b-agent` answered a prompt in 7 s afterwards.

**Conclusion for Saluki: not evaluable on this host in its current state.**
This is an environment/backend failure, not a model-quality observation; no
comparison conclusion about Saluki's capabilities is possible from this run.

## 3. Factory CLI runs on disposable projects

### 3.1 Setup

Isolated evaluation database (the repository's own `factory.db` was never
used for these runs):

```
$env:FACTORY_DB_PATH = %TEMP%\opencode\model-eval\factory-eval-qwen.db
```

Disposable workspace with a small spec: `ws-qwen\spec.md` (greeting module
requirement). Commands executed:

```
python -m factory.cli.main plan create  %TEMP%\opencode\model-eval\ws-qwen \
    --spec ws-qwen\spec.md --pipeline smoke --name qwen-eval
python -m factory.cli.main plan approve 5da1bb5e-ac12-4422-9807-82deb9a15a30 \
    --hash dc86399838e1a656ad0783cb7505145ea19278954f56ba5b224efe5716e82b93 \
    --by "Phase F evaluation"
python -m factory.cli.main run --plan-id 5da1bb5e-ac12-4422-9807-82deb9a15a30 \
    --model ollama:qwen2.5-coder:7b-agent
```

The `smoke` pipeline was chosen deliberately: exactly one builder task,
`release_eligible=false` by construction — a bounded, honest integration
exercise that can never masquerade as a release.

### 3.2 Result — passed (exit 0)

| Evidence | Value |
|---|---|
| Run wall time | 13 s |
| Orchestrator status | `idle`; task `task-implement` completed; 0 retries |
| LLM calls (`llm_calls` table) | 2 calls, model `ollama:qwen2.5-coder:7b-agent`, both `success=1`, durations 9148 ms / 2262 ms |
| Builder behavior | 2 turns: wrote `greet.py`, then reported completion |
| Workspace file | `greet.py` — `def greet(name): return f'Hello, {name}!'` |
| Evidence row | kind `file`, path `...\ws-qwen\greet.py`, sha256 `fb5a8ffa8c25...` |
| Evidence integrity | Recorded hash re-verified against the current file bytes: **MATCH** |
| Event chain | `plan.created` → `plan.approved` → `plan.execution_started` → `agent.builder.started` → `agent.builder.context_collected` → `tool.filesystem.write` → `agent.builder.completed` → `task.completed` → `plan.execution_finished` |

Both builder proposals parsed on the first attempt. The factory's structured
path sends a JSON schema via the Ollama `format` field, which forced valid
JSON at the API level — the fenced-output behavior from section 2.1 did not
occur on this path.

### 3.3 A real product defect was found and fixed by the first live run

The first live attempt **failed after 1 s** with:

```
AttributeError: 'FactoryState' object has no attribute 'check_emergency_stop_file'
```

Cause: `factory/cli/plan_exec.py` passed the booted `FactoryState` where the
orchestrator requires the `StateManager` kernel. Fixed in commit `36011ad`
with a stubbed-orchestrator regression test that asserts the wiring contract
(`state_manager` interface, plan identity, per-run model override) with no
model call. The successful run in section 3.2 used the fixed code.

### 3.4 Saluki factory run

Not attempted — the model cannot load on this host (section 2.2). There is no
integration evidence for Saluki, and none is claimed.

## 4. Side-by-side summary

| Dimension | `qwen2.5-coder:7b-agent` | `saluki-27b-iq2:latest` |
|---|---|---|
| Direct CLI minimal inference | Passed (11 s cold) | **Crash at model load** (`0xc0000409`, CUDA `shared object initialization failed`) |
| Direct CLI structured output | Valid JSON (fenced in raw CLI) | Not attempted (blocked) |
| Direct CLI code generation | Correct | Not attempted (blocked) |
| Factory smoke run (plan→approve→execute) | Passed, exit 0, evidence hash verified | Not attempted (blocked) |
| Factory-observed model string | `ollama:qwen2.5-coder:7b-agent` (2/2 calls audited) | — |

Caveats: n = 1 run per scenario, three short prompts, non-deterministic
models. The only defensible conclusions are (a) the qwen model works
end-to-end with the factory on this host, and (b) Saluki cannot be loaded by
the local Ollama backend on this host at this time. No superiority,
suitability, or default-model conclusion is drawn.

## 5. Follow-up items (reserved for the dedicated testing phase)

1. Saluki: human environment decision required (GPU/driver/Ollama backend
   compatibility on Windows). Do not modify drivers or Ollama installs
   automatically; do not delete/replace the model.
2. Full `standard` pipeline runs with a live model (QA/security/auditor +
   repair loop) were **not** part of this bounded evaluation; they remain
   reserved for the dedicated testing phase together with the three held
   tests.
3. Direct CLI fenced-output behavior is worth watching when models are used
   outside the schema-enforced path.

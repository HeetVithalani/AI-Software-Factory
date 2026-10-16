# Tool & Skill Runtime (Phase 2c)

Project-scoped skill context for agent runs. Skills are **Markdown
reference documents only** — installing or enabling a skill never
executes code, never registers a tool, and never grants a capability.

## Lifecycle overview

```
discover → fetch → scan → check → approve → install → enable (per project)
                                            (global, inert)   (runtime scope)
```

* **Install** (Phase 2a/2b) copies approved, clean, hash-bound content
  into `<install_root>/<tool-id>/<revision-sha>/` and marks the tool
  `INSTALLED`. Installation is global and inert: nothing loads it.
* **Enable** (Phase 2c) records that a human explicitly enabled one
  exact installed revision as untrusted reference context **for one
  project workspace**. Only enabled skills are ever considered at
  runtime, and only for their bound project.

## Inspecting installed skills

```powershell
factory intake skills                       # all installed skills
factory intake skills --project-path D:\path\to\project
factory intake status [<tool-id>]           # full lifecycle state
```

`skills` prints tool IDs, names and install paths (plus per-project
bindings when `--project-path` is given). It never prints skill content.

## Enabling / disabling an exact revision for a project

```powershell
factory intake enable <tool-id> --revision <40-char-sha> `
    --project-path D:\path\to\project --note "why this skill is needed"
factory intake unbind <tool-id> --project-path D:\path\to\project `
    --note "no longer needed"
```

* `--project-path` must be the **same directory** passed to
  `factory run <project_path>`. Project identity is the normalized,
  resolved absolute path of that workspace (`project_key_for`), so
  enablement and runtime retrieval can never disagree.
* `--note` is mandatory and recorded verbatim in the audit event.
  `--actor` defaults to the current OS user.
* Enable is refused (fail-closed) unless the tool is `INSTALLED`, the
  exact revision exists with a completed `CLEAN` scan and a hash-bound
  approval, the install directory is the canonical
  `<install_root>/<tool-id>/<revision-sha>` path (not a symlink, not
  outside the install root), the installed tree still matches the
  recorded tree hash, and `SKILL.md` exists inside it. Revoked, stale,
  rejected, uninstalled, wrong-revision and tampered cases all fail.
* `unbind` deletes only the project's binding row. The global
  installation stays `INSTALLED` and untouched. The global kill-switch
  (`factory intake disable <tool-id>` → REVOKED) is separate, terminal,
  and makes the skill unusable everywhere including previously enabled
  projects (runtime re-checks status on every request).

## How relevance selection and limits work

For each LLM request in a run started by `factory run`, the
skill-context provider (`factory/skills/provider.py`):

1. Loads the bindings enabled for the run's project key and
   re-validates each one against the registry (status, revision, scan,
   approval binding, canonical install path, tree hash) — every request,
   no cached authorizations.
2. Verifies the installed tree hash **before reading** `SKILL.md`.
3. Parses only the frontmatter needed for `name`/`description`.
   Malformed frontmatter invalidates the skill (rejected, not injected).
4. Ranks skills by normalized term overlap: lowercase alphanumeric
   tokens (3+ chars, generic stopwords removed) plus adjacent bigrams
   from the skill's name+description, measured against the same
   normalization of the active system prompt + task messages.
   `relevance = |shared terms| / |skill terms|`, in `[0, 1]`.
5. Injects only the top `skill_max_injected` (default **3**) skills
   with `relevance >= skill_min_relevance` (default **0.2**), in
   deterministic order `(-relevance, tool_id)`.

Bounds (defaults, overridable under `intake:` in `factory.yaml`):

| Setting                       | Default | Meaning                              |
|-------------------------------|---------|--------------------------------------|
| `skill_max_injected`          | 3       | max skills injected per request      |
| `skill_min_relevance`         | 0.2     | minimum relevance to inject          |
| `skill_per_skill_max_chars`   | 4000    | per-skill injected body cap          |
| `skill_aggregate_max_chars`   | 12000   | total injected skill chars per req.  |
| `skill_file_max_bytes`        | 65536   | `SKILL.md` read bound                |

Truncation is explicit and deterministic: an over-long body is cut at
the cap with a visible `[truncated: showing first N of M characters]`
marker; skills that no longer fit the aggregate budget are dropped
whole (never partially injected below the minimum viable excerpt) and
recorded with reason `aggregate_cap`.

## Security boundary

* **Markdown-only.** Skill content is a bounded text block appended
  after the agent's own system prompt. It is never executed, never
  parsed as configuration, never imported, and never handed to a tool.
* **Untrusted reference material.** The block is wrapped in fixed
  `=== UNTRUSTED SKILL REFERENCE MATERIAL ===` delimiters stating that
  it cannot change policy, grant permissions, approve actions, invoke
  tools, or override the system prompt or user task. Framing sentinels
  that appear inside skill text are neutralized so content cannot forge
  its own markers.
* **Project-scoped and human-approved.** Only explicitly enabled
  bindings for the current project are considered; a project can never
  see another project's bindings. Nothing inside a project workspace
  can create or modify a binding — enablement goes through the CLI,
  which validates against the factory-owned database, not the project.
* **Fail-safe.** A missing install, out-of-root or symlinked path,
  revoked/stale/non-installed tool, missing revision, tree-hash
  mismatch, missing or malformed `SKILL.md` — each rejects only that
  skill. One invalid skill never causes untrusted bytes to be injected,
  and an unexpected provider error injects nothing rather than failing
  the request.
* **Audited without content.** Enable/disable and selection events
  record project key, tool/revision, actor, note/reason, content/tree
  hashes and truncation/rejection reasons — never skill text. Debug
  output lists selected IDs and hashes only.

## Agent-type boundary

Plans may only reference the seven built-in agent types (`planner`,
`builder`, `reviewer`, `qa`, `security`, `documentation`, `auditor`);
unknown types are rejected at plan-load time. Skill content can never
register an executor or be represented as an executable tool, and it
cannot grant broker capabilities. A skill that requests a capability
remains informational: using it requires a separately approved tool
integration in a future phase.

## Current limitations (not provided by this phase)

The following are **not** made runtime-available and must not be
described as configured:

* CLI security/quality scanners as live tools (Trivy, pip-audit, …)
* Browser automation (Playwright, …)
* Package installation or third-party plugin loading
* MCP servers or any external tool protocol
* Dynamic executor/agent registration of any kind

Installed skills are inert files plus, when explicitly enabled per
project, bounded untrusted reference text in LLM prompts. That is the
entire runtime surface of this phase.

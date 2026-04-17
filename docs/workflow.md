# Workflow

## Launch Modes

### Claude-first flow

This is the recommended flow because the existing Claude session remains the conductor:

```bash
cd /path/to/target-repo
claude
```

Inside Claude, run:

```text
/ccx-run <task request>
```

The slash command runs `ccx run --no-conductor <task request>`. ccx creates run state, integration/worker worktrees, and Codex worker panes in the current cmux workspace. Claude then reads the generated conductor prompt and continues as the conductor.

### Standalone flow

```bash
cd /path/to/target-repo
ccx
```

At the pre-launch prompt, type the task request. Type `/` to open the styled slash-command picker. The picker lists Claude-native command references and ccx commands labelled like `status(ccx)`.

### One-shot CLI

```bash
ccx run "implement the requested feature"
ccx run --no-conductor "implement the requested feature"
```

`ccx run` asks Claude to create a structured JSON plan, caps workers at five, creates `.ccx/runs/<run-id>/`, creates worktrees, writes prompts, launches Codex workers, and either launches a foreground Claude conductor or leaves the current Claude session as conductor with `--no-conductor`.

## Run State

`.ccx/current-run` points to the most recent run. Runtime commands use it by default:

```bash
ccx status
ccx watch --once
ccx resume
ccx stop
```

For concurrent runs, pass `--run <run-id>`:

```bash
ccx status --run 20260415123456000000-feature
ccx watch --run 20260415123456000000-feature --once
ccx resume --run 20260415123456000000-feature
ccx stop --run 20260415123456000000-feature
```

## Phase 1: Planning

Claude planner returns a summary and task list. Each task becomes a `WorkerTask` with:

- Worker ID.
- Title.
- Objective.
- Owned scope.
- Non-goals.
- Required tests.
- Risks.
- Worker branch.
- Worker worktree.

Worker prompts contain this task-specific assignment, not the full user request as the worker's objective.

## Phase 2: State And Worktrees

ccx writes:

```text
.ccx/runs/<run-id>/plan.md
.ccx/runs/<run-id>/worktrees.md
.ccx/runs/<run-id>/tasks/<worker-id>.md
.ccx/runs/<run-id>/prompts/claude-conductor.md
.ccx/runs/<run-id>/prompts/<worker-id>.md
.ccx/runs/<run-id>/run-state.json
```

Worktrees are created with `git worktree add` first. ccx then overlays current dirty tracked files and untracked non-ignored files from the starting repository so workers see the same source snapshot the user saw when the run started.

## Phase 3: Validation Barrier

Each Codex worker starts by reading:

- The installed static protocol at `src/claude_codex/prompts/hard_rules.md`.
- Its `.ccx/runs/<run-id>/tasks/<worker-id>.md` task file.

Then the worker writes validation:

```bash
ccx validation <repo> <worker-id> \
  --run <run-id> \
  --scope-coherence "..." \
  --overlap-check "..." \
  --recommendation approve
```

Workers do not edit code before approval. If a task is ambiguous, overlapping, risky, or blocked, the worker writes a question and pauses:

```bash
ccx question <repo> <worker-id> --run <run-id> --title "..." --body "..."
```

If Codex sandboxing blocks writing the shared question file, `ccx question` writes a worker-local fallback under `.ccx-local/runs/<run-id>/questions/`. `ccx watch`, `ccx status`, and `ccx approve` count it as an open question.

## Phase 4: Approval

Claude watches state with:

```bash
ccx watch <repo> --run <run-id> --once
```

The conductor should not ask the user whether to poll, wait, or watch. It should inspect status, review validations and questions, resolve issues, and approve only after consensus:

```bash
ccx approve <repo> --run <run-id>
```

Approval writes:

```text
.ccx/runs/<run-id>/approvals/approved.json
```

`ccx approve` also sends a resume prompt to recorded worker surfaces. Workers independently wait on:

```bash
until ccx check-barrier <repo> --run <run-id>; do sleep 5; done
```

`ccx check-barrier` fails if the run is stopped, even when `approved.json` exists.

## Phase 5: Execution

After approval, workers implement only in their assigned worktree and owned scope. If new uncertainty appears, only that worker pauses and writes a question. Other workers continue unless the question affects shared scope.

Worker tabs in cmux are renamed to `<worker-id>: <task title>` for quick visual identification.

## Phase 6: Handoff

Each worker writes:

```bash
ccx handoff <repo> <worker-id> \
  --run <run-id> \
  --branch <worker-branch> \
  --worktree <worker-worktree> \
  --summary "..."
```

If Codex sandboxing blocks the shared handoff file, `ccx handoff` writes a worker-local fallback under `.ccx-local/runs/<run-id>/handoffs/`. `ccx status` and `ccx watch` count fallback handoffs so the conductor does not mistake completed workers for still-running workers.

## Phase 7: Integration

Claude integrates worker branches into the integration worktree.

Recommended order:

1. Low-risk isolated changes.
2. Shared schema/config changes.
3. Feature logic.
4. Tests and documentation.

If conflicts occur, Claude either resolves them directly or creates a new focused worker task.

## Phase 8: Verification And PR

Claude runs formatting, linting, and tests. Then Claude splits coherent commits, pushes the branch, and opens a PR.

Merge requires explicit human approval.

## Legacy Manual Commands

`ccx init` still creates the legacy `.orchestrator/` template state for manual protocol experiments. Normal orchestration uses `.ccx/runs/<run-id>/`.

```bash
ccx init <target-repo> <run-name> <worker-count>
```

If no `.ccx/current-run` pointer exists, run-state commands require `--run <run-id>`
unless you are intentionally operating on an existing legacy `.orchestrator/`
template directory.

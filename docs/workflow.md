# Workflow


## MVP CLI

The local CLI enforces the state-file protocol before cmux/omx automation is added.

Interactive launch from a target repository:

```bash
cd /path/to/target-repo
ccx
```

At the pre-launch prompt, type `/` to preview the Claude + ccx slash command setup. The launched conductor is still a normal Claude Code session, so Claude native slash commands remain available. ccx installs additional user-level Claude slash commands named `/ccx-status`, `/ccx-watch`, `/ccx-resume`, and `/ccx-stop`; their descriptions use `status(ccx)` style labels to show they belong to ccx.

One-shot launch:

```bash
ccx run "implement the requested feature"
```

In run mode, Claude Opus plans the worker split, `ccx` creates the shared `.orchestrator` state and git worktrees, then cmux opens one Claude conductor pane and one Codex pane per worker.

```bash
./scripts/claude-codex init /path/to/target-repo feature-name 3
./scripts/claude-codex status /path/to/target-repo
./scripts/claude-codex validation /path/to/target-repo worker-01 --scope-coherence "Scope is coherent." --overlap-check "No overlap." --recommendation approve
./scripts/claude-codex resolve-question /path/to/target-repo worker-01-001 --answer "Conductor decision."
./scripts/claude-codex approve /path/to/target-repo
./scripts/claude-codex handoff /path/to/target-repo worker-01 --branch worker/feature --worktree /path/to/worktree --summary "Done"
```

Approval is blocked when validations are missing or unresolved question files exist. Resolve questions with `resolve-question`, or pass `--force` only for an intentional override.

## Phase 0: Setup

1. Confirm GitHub authentication and target repository status.
2. Create an integration branch/worktree.
3. Create one worker branch/worktree per task.
4. Create `.orchestrator/` state.

## Phase 1: Planning

Claude writes `.orchestrator/plan.md` and one task file per worker in `.orchestrator/tasks/`.

Task files must define:

- Worker ID.
- Worktree path.
- Branch name.
- Owned files/modules.
- Explicit non-goals.
- Validation questions to answer before implementation.
- Required tests.

## Phase 2: Validation Barrier

Each Codex worker reads its task and writes `.orchestrator/validations/<worker-id>.md`.

Workers must not edit code in this phase.

If a worker finds ambiguity, it writes `.orchestrator/questions/<worker-id>-001.md` and stops.

Claude reviews all validations and questions. When consensus is reached, Claude writes `.orchestrator/approvals/approved.json`.

## Phase 3: Execution

Workers continue independently after approval.

If a worker becomes uncertain, it pauses only itself and writes a question. Other workers continue unless the question affects shared scope.

## Phase 4: Handoff

Each worker writes `.orchestrator/handoffs/<worker-id>.md`.

The handoff must contain:

- Worktree path.
- Branch name.
- Commit or diff reference.
- Files changed.
- Behavioral changes.
- Tests run.
- Known risks.
- Integration notes.

## Phase 5: Integration

Claude integrates worker branches into the integration worktree.

Preferred order:

1. Low-risk isolated changes.
2. Shared schema/config changes.
3. Feature logic.
4. Tests and documentation.

If conflicts occur, Claude either resolves them directly or creates a new targeted worker task.

## Phase 6: Verification and PR

Claude runs formatting, linting, and tests. Then Claude splits commits by coherent area, pushes the branch, and opens a PR.

Merge requires explicit human approval.

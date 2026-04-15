# Workflow

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

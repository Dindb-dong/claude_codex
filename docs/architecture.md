# Architecture

`ccx` coordinates one Claude conductor and multiple Codex workers inside cmux, with durable state stored in the target repository. The default runtime path is `.ccx/runs/<run-id>/`; `.orchestrator/` is only the legacy manual-template path used by `ccx init`.

## Roles

### Claude Conductor

Claude owns judgment-heavy work:

- Understand the user request.
- Ask Claude planner mode to split the request into at most five independent worker tasks.
- Keep the current Claude session as conductor in Claude-first mode (`/ccx-run` / `ccx run --no-conductor`).
- Review worker validations and questions before approval.
- Approve execution with `ccx approve <repo> --run <run-id>` only after consensus.
- Watch progress with `ccx watch` rather than asking the user whether to poll.
- Review handoffs, integrate worker branches into the integration worktree, resolve conflicts, run checks, commit, push, and open a PR.
- Never merge without explicit human approval.

### Codex Worker

Codex workers own execution-heavy work:

- Receive a task-specific prompt generated from the Claude planner output, not the full user request as the assignment.
- Read the installed static protocol at `src/claude_codex/prompts/hard_rules.md` without `@file` expansion.
- Validate assigned scope before editing.
- Write `ccx validation` output first.
- Write `ccx question` and pause when scope is unclear, overlapping, risky, or blocked.
- Wait for `ccx check-barrier` before implementation.
- Modify only the assigned worktree and owned scope.
- Write `ccx handoff` when finished.
- Never push, merge, or mutate global run state.

### cmux

cmux provides the terminal workspace, panes, and surfaces. ccx launches worker panes, records their surface refs in run state, and renames each worker surface tab to `<worker-id>: <task title>` so the conductor can identify roles visually.

### Git Worktrees

ccx creates an integration worktree and one worker worktree per task. It uses `git worktree add` first, then overlays the starting repository's dirty tracked files and untracked non-ignored source files into each worktree. Internal orchestration paths such as `.git`, `.ccx`, `.orchestrator`, and `.ccx-worktrees` are excluded.

## Runtime State

A normal run stores state under the target repository:

```text
.ccx/
├── current-run
└── runs/
    └── <run-id>/
        ├── run-state.json
        ├── plan.md
        ├── worktrees.md
        ├── tasks/
        ├── validations/
        ├── approvals/
        ├── questions/
        │   └── resolved/
        ├── handoffs/
        └── prompts/
            ├── claude-conductor.md
            └── worker-NN.md
```

Worker-local fallbacks are written inside the worker worktree when Codex sandboxing rejects shared-state writes:

```text
<worker-worktree>/.ccx-local/runs/<run-id>/
├── questions/
└── handoffs/
```

`ccx status`, `ccx watch`, and `ccx approve` count worker-local fallback questions. `ccx status` and `ccx watch` also count worker-local fallback handoffs.

## Barrier Contract

Workers may inspect files and write validation before approval. Workers may not edit code until both are true:

```text
.ccx/runs/<run-id>/approvals/approved.json exists
ccx check-barrier <repo> --run <run-id> succeeds
```

`ccx check-barrier` refuses stopped runs even if `approved.json` exists.

## Prompt Model

Runtime prompts are generated per run:

- `prompts/claude-conductor.md` tells Claude to watch progress, review validations/questions, approve, integrate, verify, and open PRs.
- `prompts/worker-NN.md` contains only that worker's planner-assigned task, run paths, and exact command shapes.
- The shared worker protocol is installed once as `src/claude_codex/prompts/hard_rules.md` and referenced by path, not expanded into each worker prompt.

## Stop And Resume

- `Ctrl-C` in a launched agent goes through `ccx agent` and marks the run stopped when the child exits with a signal status.
- `Esc` is handled by Claude/Codex directly and may leave stale `running` state. Generated prompts instruct the conductor to recover stale interrupted state, while workers report and wait rather than calling `ccx stop`.
- `ccx resume <repo> --run <run-id>` relaunches worker panes from persisted state.

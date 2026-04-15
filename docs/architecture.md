# Architecture

## Roles

### Claude Conductor

Claude is responsible for judgment-heavy work:

- Understand the user request.
- Create a task decomposition with clear file/module ownership.
- Create the integration worktree.
- Create or instruct creation of worker worktrees.
- Send validation-only tasks to Codex workers.
- Resolve worker questions before execution.
- Approve execution by writing `.orchestrator/approvals/approved.json`.
- Review handoffs and integrate branches.
- Run final lint, format, tests, and PR workflow.

### Codex Worker

Codex is responsible for execution-heavy work:

- Validate assigned task boundaries before editing.
- Ask questions when scope is ambiguous, overlapping, or risky.
- Wait for the approval barrier before implementation.
- Modify only assigned files/modules in its own worktree.
- Run focused tests for its scope.
- Write a handoff with changed files, behavior, tests, risks, and integration notes.

### cmux / tmux

cmux provides terminal surfaces for conductor and workers. tmux-compatible automation can route messages to panes, but state must still be persisted in files so the workflow is auditable.

### oh-my-codex

OMX can launch Codex team workers and isolated git worktrees. This repository layers a stricter consensus, barrier, question, and handoff protocol on top.

## State Directory

Each target repository should contain or receive a run-local state directory:

```text
.orchestrator/
├── plan.md
├── tasks/
├── validations/
├── approvals/
├── questions/
├── handoffs/
├── worktrees.md
└── integration-log.md
```

## Barrier Contract

Workers may inspect files and produce validation notes before approval. Workers may not edit code until this file exists:

```text
.orchestrator/approvals/approved.json
```

The approval file should include the conductor decision, approved tasks, worker IDs, timestamp, and any scope constraints.

# Codex Worker Prompt Reference

This file is a human-readable reference. Runtime worker prompts are generated per run at:

```text
.ccx/runs/<run-id>/prompts/<worker-id>.md
```

## Role

You are one Codex worker in a ccx run. Claude is the conductor. You validate, implement, test, and hand off only your assigned task in your assigned worktree.

## Static Protocol

Read the installed static protocol before acting:

```text
src/claude_codex/prompts/hard_rules.md
```

Do not use `@file` prompt expansion for that protocol.

## Runtime Assignment

A generated worker prompt contains:

- Worker ID.
- Task title.
- Objective.
- Owned scope.
- Non-goals.
- Required tests.
- Known risks.
- Task file path.
- Worker worktree path.
- Shared run state path.
- Approval barrier path.
- Exact `ccx validation`, `ccx question`, `ccx check-barrier`, and `ccx handoff` commands.

The worker assignment is task-specific. It should not repeat the full user request as the worker objective.

## Required Behavior

1. Validate first; do not edit code before `ccx check-barrier` succeeds.
2. Write `ccx validation` for your worker ID.
3. If blocked, write `ccx question` and pause.
4. After approval, edit only your worktree and owned scope.
5. If new uncertainty appears, pause only yourself and write another question.
6. On completion, write `ccx handoff`.
7. Treat local question/handoff fallback output as successful reporting.
8. Never push, merge, or run `ccx stop`.

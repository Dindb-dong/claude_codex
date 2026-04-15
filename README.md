# claude_codex

Claude conductor + Codex worker orchestration kit for `cmux` and `oh-my-codex` workflows.

This repository defines the operating protocol for using Claude Opus as the planner/reviewer and Codex workers as implementation agents in isolated git worktrees.

## Goal

Reduce expensive Claude execution loops by using Claude only for planning, arbitration, integration review, and final verification while Codex workers handle implementation and tests.

## Core Protocol

1. Claude creates an integration worktree and decomposes the user request.
2. Claude creates isolated worker worktrees and sends validation-only tasks to Codex workers.
3. Codex workers validate scope first and do not edit code before approval.
4. If a task is unclear, overlapping, or risky, the worker writes a question and stops.
5. Claude resolves questions and writes the approval barrier.
6. Workers implement independently after approval.
7. Workers stop only themselves when new uncertainty appears.
8. Workers write handoff documents when done.
9. Claude reviews handoffs, integrates branches, resolves conflicts, and runs checks.
10. Claude splits commits, pushes, opens a PR, and waits for explicit human approval before merge.

## Repository Layout

- `docs/architecture.md`: system model and responsibilities.
- `docs/workflow.md`: end-to-end operating flow.
- `prompts/claude-conductor.md`: prompt for the Claude conductor pane.
- `prompts/codex-worker.md`: prompt for each Codex worker pane.
- `templates/`: task, validation, question, and handoff templates.
- `scripts/bootstrap-run.sh`: creates a local orchestration state directory for a run.

## Quick Start

```bash
cd /Users/maxkim/claude_codex
./scripts/bootstrap-run.sh /path/to/target-repo feature-name 3
```

Then start `cmux omx` or open Claude/Codex panes manually and paste the conductor/worker prompts.

## Safety Rules

- Workers must not edit files before `.orchestrator/approvals/approved.json` exists.
- Each worker owns a separate worktree and a clearly bounded file/module scope.
- Same-file edits by multiple workers require explicit Claude arbitration.
- Merge requires explicit human approval.

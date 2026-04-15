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
- `src/claude_codex/`: dependency-free MVP CLI.
- `scripts/claude-codex`: local CLI wrapper.
- `scripts/bootstrap-run.sh`: compatibility wrapper for `claude-codex init`.

## Quick Start

Install a global local command:

```bash
ln -sfn /Users/maxkim/claude_codex/scripts/claude-codex ~/.local/bin/claude-codex
```

After that, `claude-codex` works from any directory as long as `~/.local/bin` is in `PATH`.

Initialize orchestration state in a target git repository:

```bash
cd /Users/maxkim/claude_codex
claude-codex init /path/to/target-repo feature-name 3
```

Check status:

```bash
claude-codex status /path/to/target-repo
```

Workers write validation files before implementation:

```bash
claude-codex validation /path/to/target-repo worker-01 \
  --scope-coherence "Scope is coherent." \
  --overlap-check "No overlap with other workers." \
  --recommendation approve
```

Claude writes the approval barrier only after validations are complete and questions are resolved:

```bash
claude-codex approve /path/to/target-repo
claude-codex check-barrier /path/to/target-repo
```

Workers write handoffs when finished:

```bash
claude-codex handoff /path/to/target-repo worker-01 \
  --branch worker/feature-area \
  --worktree /path/to/worktree \
  --summary "Implemented assigned task." \
  --file src/example.py \
  --test "python -m unittest"
```

Then start `cmux omx` or open Claude/Codex panes manually and paste the conductor/worker prompts.

## CLI Commands

```text
claude-codex init <target-repo> <run-name> <worker-count>
claude-codex status <target-repo> [--json]
claude-codex validation <target-repo> <worker-id> ...
claude-codex question <target-repo> <worker-id> ...
claude-codex resolve-question <target-repo> <question-name> --answer "..."
claude-codex approve <target-repo> [--force]
claude-codex check-barrier <target-repo>
claude-codex handoff <target-repo> <worker-id> ...
```

## Safety Rules

- Workers must not edit files before `.orchestrator/approvals/approved.json` exists.
- Each worker owns a separate worktree and a clearly bounded file/module scope.
- Same-file edits by multiple workers require explicit Claude arbitration.
- Merge requires explicit human approval.

## Development

Run tests:

```bash
PYTHONPATH=src python3 -m unittest -v
```

Run the local CLI:

```bash
./scripts/claude-codex --help
```

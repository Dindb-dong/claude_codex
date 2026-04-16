# claude-codex (`ccx`)

`ccx` is a local orchestration CLI for teams who use **cmux + Claude Code + OpenAI Codex CLI**. It launches Claude as the conductor and Codex workers as implementation agents in isolated git worktrees.

The goal is to spend Claude on planning, arbitration, review, and integration while Codex handles implementation-heavy loops.

## Requirements

Install these first:

- `git`
- `cmux`
- `claude` from Claude Code
- `codex` from OpenAI Codex CLI
- Python 3.11+

Check your environment after installation:

```bash
ccx doctor
```

`ccx run` uses non-interactive `claude --print` for planning. `ccx doctor`
also checks Claude Code CLI auth; if it reports logged out, run `claude` in the
same terminal and execute `/login` before launching ccx.

## Install

Clone the repository and install editable mode:

```bash
git clone https://github.com/Dindb-dong/claude_codex.git
cd claude_codex
python3 -m pip install -e .
ccx install-claude-commands
ccx doctor
```

Or run the helper script:

```bash
./scripts/install-local.sh
```

This installs two console commands:

```text
ccx
claude-codex
```

It also installs Claude Code user slash commands into `~/.claude/commands/`:

```text
/ccx-status   status(ccx): show orchestration state
/ccx-run      run(ccx): start worker orchestration from current Claude
/ccx-watch    watch(ccx): watch progress
/ccx-resume   resume(ccx): relaunch conductor/workers
/ccx-stop     stop(ccx): mark run stopped
```

Claude native slash commands remain available. The ccx commands are namespaced to avoid collisions with Claude commands such as `/status`, `/model`, and `/help`.

## Quick Start

Recommended Claude-first flow:

```bash
cd /path/to/your-repo
claude
```

Then run `/ccx-run <task request>` inside Claude. ccx creates run state,
worktrees, and Codex worker panes, while the already-open Claude session remains
the conductor.

Standalone flow:

```bash
cd /path/to/your-repo
ccx
```

Then describe the task. `ccx` asks Claude Opus to decide the worker split, creates run-scoped state under `.ccx/runs/<run-id>/`, creates integration/worker git worktrees, launches Codex worker panes in cmux, and starts the Claude conductor in the current `ccx` terminal.

At the pre-launch prompt, type `/` to open a styled slash-command picker. Use arrow keys
to move and Enter to select. The list includes Claude-native command references and ccx
commands labeled like `status(ccx)` to avoid ambiguity.

One-shot form:

```bash
ccx run "implement the requested feature"
ccx run --no-conductor "implement the requested feature"
```

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

## Runtime Commands

```bash
ccx status [target-repo] [--json]
ccx watch [target-repo] [--interval 2] [--once]
ccx resume [target-repo]
ccx stop [target-repo] [--close-cmux]
```

Multiple `ccx` runs can exist in the same repository. `ccx status` uses `.ccx/current-run` by default. Pass `--run <run-id>` to inspect or resume a specific run:

```bash
ccx status --run 20260415123456000000-feature
ccx watch --run 20260415123456000000-feature --once
ccx resume --run 20260415123456000000-feature
ccx stop --run 20260415123456000000-feature
```

Codex workers run through a lightweight `ccx agent` wrapper. The Claude conductor is launched as a foreground CLI in the original `ccx` terminal so the user can approve, arbitrate, and review from Claude directly. Pressing `Ctrl-C` interrupts the active Claude/Codex child process and marks the current ccx run as `stopped` when the child exits with a signal status. The pane stays open by default; use `ccx stop --close-cmux` only when you also want to close the recorded cmux worker workspace.

`Esc` remains a native Claude/Codex interrupt. Since it may not notify ccx, generated conductor and worker prompts include an interrupt recovery rule: before resuming after an explicit user interrupt, the agent checks `ccx status --run <run-id> --json`; if the run is still stale `running`, it first runs `ccx stop --run <run-id>`.

Manual state commands:

```bash
ccx init <target-repo> <run-name> <worker-count>
ccx validation <target-repo> <worker-id> \
  --scope-coherence "Scope is coherent." \
  --overlap-check "No overlap with other workers." \
  --recommendation approve
ccx question <target-repo> <worker-id> --title "Question" --body "Details"
ccx resolve-question <target-repo> <question-name> --answer "Decision"
ccx approve <target-repo>
ccx check-barrier <target-repo>
ccx handoff <target-repo> <worker-id> \
  --branch worker/feature-area \
  --worktree /path/to/worktree \
  --summary "Implemented assigned task." \
  --file src/example.py \
  --test "python -m unittest"
```

## Default Models

- Claude conductor: `opus`, effort `medium`.
- Codex workers: `gpt-5.3-codex`, reasoning effort `medium`.
- Human label `normal` is treated as `medium` internally because the installed CLIs accept `medium`, not `normal`.

Environment overrides:

```bash
export CCX_CLAUDE_MODEL=opus
export CCX_CLAUDE_EFFORT=medium
export CCX_CODEX_MODEL=gpt-5.3-codex
export CCX_CODEX_EFFORT=medium
```

## Safety Rules

- Workers must not edit files before the run approval barrier exists.
- Each worker owns a separate worktree and a clearly bounded file/module scope.
- Same-file edits by multiple workers require explicit Claude arbitration.
- `Ctrl-C` in a launched pane and `ccx stop` mark state stopped by default. They close cmux panes only with `--close-cmux`.
- `Esc` is recovered by prompt protocol on resume: agents only stop stale `running` state when recovering from an explicit user interrupt.
- Merge requires explicit human approval.

## Repository Layout

- `docs/architecture.md`: system model and responsibilities.
- `docs/workflow.md`: end-to-end operating flow.
- `prompts/claude-conductor.md`: prompt for the Claude conductor running in the current ccx terminal.
- `prompts/codex-worker.md`: prompt for each Codex worker pane.
- `templates/`: task, validation, question, and handoff templates.
- `src/claude_codex/`: Python CLI implementation.
- `scripts/install-local.sh`: editable install + Claude command install + doctor.
- `scripts/claude-codex`: local wrapper for repo development.
- `scripts/bootstrap-run.sh`: compatibility wrapper for `claude-codex init`.

## Development

Run tests:

```bash
PYTHONPATH=src python3 -m unittest -v
```

Run lint/format:

```bash
ruff format .
ruff check .
```

Run the local wrapper without installing:

```bash
./scripts/claude-codex --help
```

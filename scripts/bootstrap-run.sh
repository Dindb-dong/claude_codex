#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <target-repo> <run-name> <worker-count>" >&2
  exit 2
fi

TARGET_REPO="$1"
RUN_NAME="$2"
WORKER_COUNT="$3"

if [[ ! -d "$TARGET_REPO/.git" ]]; then
  echo "Target repo must be a git repository: $TARGET_REPO" >&2
  exit 1
fi

if ! [[ "$WORKER_COUNT" =~ ^[0-9]+$ ]] || [[ "$WORKER_COUNT" -lt 1 ]]; then
  echo "worker-count must be a positive integer" >&2
  exit 1
fi

STATE_DIR="$TARGET_REPO/.orchestrator"
mkdir -p "$STATE_DIR"/{tasks,validations,approvals,questions,handoffs}

cat > "$STATE_DIR/plan.md" <<PLAN
# Orchestration Plan

- Run: $RUN_NAME
- Target repo: $TARGET_REPO
- Worker count: $WORKER_COUNT
- Status: planning

## User Request


## Decomposition


## Integration Strategy


PLAN

cat > "$STATE_DIR/worktrees.md" <<WORKTREES
# Worktrees

## Integration

- Branch:
- Path:

## Workers

WORKTREES

for i in $(seq 1 "$WORKER_COUNT"); do
  WORKER_ID=$(printf "worker-%02d" "$i")
  TASK_FILE="$STATE_DIR/tasks/$WORKER_ID.md"
  cat > "$TASK_FILE" <<TASK
# Worker Task

## Worker

- ID: $WORKER_ID
- Branch:
- Worktree:

## Objective


## Owned Scope


## Non-Goals


## Validation Requirements

1. Confirm this scope is coherent.
2. Confirm this scope does not overlap with other workers.
3. Identify missing context before implementation.

## Implementation Requirements

Do not edit code until .orchestrator/approvals/approved.json exists.

## Required Tests


## Handoff Path

.orchestrator/handoffs/$WORKER_ID.md
TASK
  echo "- $WORKER_ID: see .orchestrator/tasks/$WORKER_ID.md" >> "$STATE_DIR/worktrees.md"
done

echo "Created orchestration state at $STATE_DIR"

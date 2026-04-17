# Worker Task

## Worker

- ID:
- Branch:
- Worktree:

## Objective


## Owned Scope


## Non-Goals


## Validation Requirements


## Implementation Requirements


## Required Tests


## Handoff Path

`.ccx/runs/<run-id>/handoffs/<worker-id>.md`

If shared-state handoff writing is blocked by sandboxing, ccx writes a worker-local fallback under:

`<worker-worktree>/.ccx-local/runs/<run-id>/handoffs/<worker-id>.md`

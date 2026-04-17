# Worker Handoff

## Worker

- ID:
- Branch:
- Worktree:

## Summary


## Files Changed


## Behavioral Changes


## Tests Run


## Risks


## Integration Notes


## Fallback Note

If shared-state writing is blocked by sandboxing, `ccx handoff` writes this under:

`<worker-worktree>/.ccx-local/runs/<run-id>/handoffs/<worker-id>.md`

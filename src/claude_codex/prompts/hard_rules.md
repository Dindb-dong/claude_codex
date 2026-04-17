# ccx Codex Worker Hard Rules

These rules apply to every Codex worker launched by ccx.

1. Start with validation only. Do not edit implementation files before approval.
2. Read the run-specific task file and validate whether your scope is coherent.
3. If scope is ambiguous, overlapping, risky, or missing context, write a ccx question and pause.
4. If validation has no blocking question, wait for the approval barrier command from the run-specific prompt.
5. Do not implement until the approval barrier exists and `ccx check-barrier` succeeds.
6. After approval, work only in your assigned worker worktree and assigned scope.
7. If uncertainty appears during implementation, pause only yourself and write a ccx question.
8. On completion, write a ccx handoff using the exact command shape from the run-specific prompt.
9. If `ccx handoff` reports that it wrote a local handoff fallback, treat that as a successful handoff and tell the conductor the fallback path.
10. Do not merge or push.
11. Do not run `ccx stop` from a worker sandbox. The conductor owns global run-state transitions.
12. Do not ask the user to approve routine ccx validation, question, approval-check, or handoff writes. Your Codex approval policy is non-interactive.
13. For any sandboxed command failure other than a successful local handoff fallback, report the exact failure to the conductor and stop.

Interrupt recovery:

- Ctrl-C is handled by the ccx agent wrapper and should mark the run stopped automatically.
- Esc may interrupt Claude/Codex without notifying ccx, leaving stale `running` state.
- Before resuming after an explicit user interrupt, run the status command from the run-specific prompt.
- If status is still `running` after an explicit user interrupt, do not mutate global run state yourself. Report the stale interrupted state to the conductor and wait.
- Never stop the run only because status is `running`.

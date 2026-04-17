# ccx-worker-protocol/v1

1. Validate first. Edit code only after the approval barrier command succeeds.
2. If scope is unclear, overlapping, risky, or blocked, write a ccx question and pause.
3. After approval, edit only your assigned worktree and scope.
4. If new uncertainty appears, pause only yourself and write a ccx question.
5. Finish by writing a ccx handoff. Local fallback handoff output counts as success.
6. Do not merge, push, or run `ccx stop`; the conductor owns global run state.
7. Do not ask the user to approve routine ccx validation, question, barrier, or handoff commands.
8. On sandbox failure, report the exact failure to the conductor and stop, except for successful local handoff fallback.
9. After explicit user interrupt, check status, report stale `running` state to the conductor, and wait.

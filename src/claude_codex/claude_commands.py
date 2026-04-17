"""Install Claude Code slash commands for ccx runtime operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROUTINE_ALLOWED_TOOLS = [
    "Bash(ccx:*)",
    "Bash(cmux read-screen:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
]


@dataclass(frozen=True)
class ClaudeCommand:
    """Claude Code custom slash command definition.

    Args:
        name: Command file stem.
        description: Description displayed by Claude Code.
        argument_hint: Optional argument hint for command completion.
        body: Prompt body for Claude.
        allowed_tools: Bash tool allowlist for the command.
    """

    name: str
    description: str
    argument_hint: str
    body: str
    allowed_tools: list[str]

    def render(self) -> str:
        """Render the slash command markdown file."""
        allowed = ", ".join(self.allowed_tools)
        return f"""---
allowed-tools: {allowed}
argument-hint: {self.argument_hint}
description: {self.description}
---

{self.body}
"""


COMMANDS = [
    ClaudeCommand(
        name="ccx-run",
        description="run(ccx): Start worker orchestration from the current Claude session",
        argument_hint="<task request>",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Start a ccx worker orchestration while this Claude session remains the conductor.

Use this when the user wants Claude-first orchestration. Do not launch a nested Claude CLI.

Run:

```bash
ccx run --no-conductor $ARGUMENTS
```

After the command completes:

1. Read the printed run id and conductor prompt path.
2. Run `ccx status --json` and summarize the run state.
3. Read the conductor prompt file and follow it as your operating protocol.
4. Review worker validations and questions before approving.
5. Do not ask the user whether to poll, wait, or watch. Use `ccx watch --once`
   to inspect progress and proceed from observed state.
6. Ask the user before writing the approval barrier only if a scope/question is
   genuinely unclear after your review.
7. When consensus is reached, run `ccx approve <repo-path> --run <run-id>`.
8. Continue as the conductor: review handoffs, run `ccx integrate <repo-path>
   --run <run-id>`, run checks, commit, push, and open PRs.

Use simple, single-command Bash calls for routine ccx/cmux inspection. Do not chain
commands with `&&`, pipes, command substitution, or shell scripts unless the user
explicitly asks.
""",
    ),
    ClaudeCommand(
        name="ccx-status",
        description="status(ccx): Show the current ccx orchestration state",
        argument_hint="[repo-path]",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Show the current ccx orchestration state.

Run this command and summarize the result:

```bash
ccx status $ARGUMENTS
```
""",
    ),
    ClaudeCommand(
        name="ccx-watch",
        description="watch(ccx): Watch ccx orchestration progress",
        argument_hint="[repo-path] [--interval seconds]",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Watch ccx orchestration progress.

Run this command. If the user did not pass a repo path, use the current repository:

```bash
ccx watch $ARGUMENTS
```

Stop watching when the user asks you to stop or when the command exits.
""",
    ),
    ClaudeCommand(
        name="ccx-integrate",
        description="integrate(ccx): Merge completed worker branches",
        argument_hint="[repo-path] [--run run-id]",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Merge completed ccx worker branches into the integration worktree.

Run this only after worker handoffs are present and open questions are resolved:

```bash
ccx integrate $ARGUMENTS
```

If integration fails, read the printed report path and decide whether to resolve
the conflict directly or create a focused follow-up worker task.
""",
    ),
    ClaudeCommand(
        name="ccx-resume",
        description="resume(ccx): Relaunch the ccx conductor and worker panes",
        argument_hint="[repo-path]",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Resume a previous ccx run.

Run this command and report which cmux workspace was created:

```bash
ccx resume $ARGUMENTS
```
""",
    ),
    ClaudeCommand(
        name="ccx-stop",
        description="stop(ccx): Mark a ccx run as stopped",
        argument_hint="[repo-path] [--close-cmux]",
        allowed_tools=ROUTINE_ALLOWED_TOOLS,
        body="""Stop a ccx run.

By default this marks the run as stopped without closing cmux panes.
Only pass `--close-cmux` when the user explicitly asks to close panes.

```bash
ccx stop $ARGUMENTS
```
""",
    ),
]


def command_dir() -> Path:
    """Return the user-level Claude commands directory."""
    return Path.home() / ".claude" / "commands"


def install_claude_commands() -> list[Path]:
    """Install or update ccx Claude Code slash commands.

    Returns:
        Paths written.
    """
    directory = command_dir()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for command in COMMANDS:
        path = directory / f"{command.name}.md"
        path.write_text(command.render(), encoding="utf-8")
        written.append(path)
    return written

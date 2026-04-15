"""Install Claude Code slash commands for ccx runtime operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
        name="ccx-status",
        description="status(ccx): Show the current ccx orchestration state",
        argument_hint="[repo-path]",
        allowed_tools=["Bash(ccx *)"],
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
        allowed_tools=["Bash(ccx *)"],
        body="""Watch ccx orchestration progress.

Run this command. If the user did not pass a repo path, use the current repository:

```bash
ccx watch $ARGUMENTS
```

Stop watching when the user asks you to stop or when the command exits.
""",
    ),
    ClaudeCommand(
        name="ccx-resume",
        description="resume(ccx): Relaunch the ccx conductor and worker panes",
        argument_hint="[repo-path]",
        allowed_tools=["Bash(ccx *)"],
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
        allowed_tools=["Bash(ccx *)"],
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

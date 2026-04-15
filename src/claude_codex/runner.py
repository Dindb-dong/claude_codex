"""Interactive orchestration runner for Claude conductor and Codex workers."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.shortcuts import CompleteStyle
    from prompt_toolkit.styles import Style
except ModuleNotFoundError:  # pragma: no cover - exercised only in incomplete installs.
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment,misc]
    Completion = None  # type: ignore[assignment]
    CompleteStyle = None  # type: ignore[assignment]
    Document = object  # type: ignore[assignment,misc]
    HTML = None  # type: ignore[assignment]
    KeyBindings = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]

from claude_codex.claude_commands import install_claude_commands
from claude_codex.cli import CliError, StatePaths, ensure_state_dirs, write_text

MAX_AUTO_WORKERS = 6
EFFORT_ALIASES = {"normal": "medium", "med": "medium"}
CCX_DIR_NAME = ".ccx"
RUNS_DIR_NAME = "runs"
CURRENT_RUN_FILE = "current-run"
DEFAULT_CCX_BIN = "ccx"


@dataclass(frozen=True)
class WorkerTask:
    """Planner-assigned worker task.

    Args:
        worker_id: Stable worker identifier.
        title: Short task title.
        objective: Implementation objective.
        owned_scope: File or module ownership boundaries.
        non_goals: Explicit exclusions.
        required_tests: Required verification commands or test areas.
        risks: Known task risks.
        branch: Worker branch name.
        worktree: Worker worktree path.
    """

    worker_id: str
    title: str
    objective: str
    owned_scope: list[str]
    non_goals: list[str]
    required_tests: list[str]
    risks: list[str]
    branch: str
    worktree: Path


@dataclass(frozen=True)
class Plan:
    """Claude-generated orchestration plan.

    Args:
        summary: Plan summary.
        worker_count: Number of Codex workers.
        tasks: Worker task assignments.
    """

    summary: str
    worker_count: int
    tasks: list[WorkerTask]


@dataclass(frozen=True)
class RunConfig:
    """Runtime configuration for an orchestration launch.

    Args:
        repo: Target git repository.
        request: User request to implement.
        claude_model: Claude conductor model alias or full name.
        claude_effort: Claude effort level.
        codex_model: Codex worker model.
        codex_effort: Codex reasoning effort.
        requested_workers: Optional manual worker count.
        dry_run: Whether to avoid worktree and cmux side effects.
        skip_launch: Whether to prepare files but skip cmux launch.
        force_state: Whether to overwrite existing orchestration state.
    """

    repo: Path
    request: str
    claude_model: str
    claude_effort: str
    codex_model: str
    codex_effort: str
    requested_workers: int | None
    dry_run: bool
    skip_launch: bool
    force_state: bool


@dataclass(frozen=True)
class SlashCommand:
    """Slash command shown in the interactive picker.

    Args:
        trigger: User-facing slash command trigger.
        description: Short command description.
        source: Command owner, such as claude or ccx.
        action: Local action performed when selected.
    """

    trigger: str
    description: str
    source: str
    action: str = "reference"


def slash_commands() -> list[SlashCommand]:
    """Return Claude-native reference commands plus local ccx commands."""
    return [
        SlashCommand(
            "/browse",
            "Fast headless browser for QA testing and site dogfooding",
            "claude",
        ),
        SlashCommand(
            "/review",
            "Pre-landing PR review against the base branch",
            "claude",
        ),
        SlashCommand("/review-pr", "Review a pull request", "claude"),
        SlashCommand(
            "/qa",
            "Systematically QA test a web application and identify fixes",
            "claude",
        ),
        SlashCommand(
            "/update-config",
            "Configure the Claude Code harness and local project context",
            "claude",
        ),
        SlashCommand("/add-dir", "Add a working directory to the Claude session", "claude"),
        SlashCommand("/status", "status(ccx): Show current orchestration state", "ccx", "status"),
        SlashCommand("/watch", "watch(ccx): Watch orchestration progress", "ccx", "watch"),
        SlashCommand(
            "/resume", "resume(ccx): Relaunch conductor and worker panes", "ccx", "resume"
        ),
        SlashCommand("/stop", "stop(ccx): Mark current run stopped", "ccx", "stop"),
        SlashCommand("/doctor", "doctor(ccx): Check cmux, claude, codex, and git", "ccx", "doctor"),
        SlashCommand("/exit", "Exit ccx without launching a run", "ccx", "exit"),
        SlashCommand("/quit", "Quit ccx without launching a run", "ccx", "exit"),
    ]


class SlashCommandCompleter(Completer):
    """Prompt-toolkit completer for slash commands."""

    def __init__(self, commands: list[SlashCommand]) -> None:
        """Create a slash completer.

        Args:
            commands: Commands to show.
        """
        self.commands = commands

    def get_completions(self, document: Document, _complete_event: Any) -> Any:
        """Yield matching slash command completions.

        Args:
            document: Current prompt document.
            _complete_event: Prompt-toolkit completion event.
        """
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        query = text[1:].lower()
        for command in self.commands:
            haystack = f"{command.trigger} {command.description} {command.source}".lower()
            if query and query not in haystack:
                continue
            display_meta = f"{command.description}"
            yield Completion(
                command.trigger,
                start_position=-len(text),
                display=command.trigger,
                display_meta=display_meta,
            )


def slash_command_style() -> Any:
    """Return prompt-toolkit styling for the ccx prompt."""
    if Style is None:
        return None
    return Style.from_dict(
        {
            "prompt": "bold #f4f1ea",
            "path": "#c5cf6a",
            "muted": "#8b909a",
            "accent": "bold #8fd0cb",
            "warning": "bold #f0ce73",
            "completion-menu": "bg:#242933 #aeb4be",
            "completion-menu.completion.current": "bg:#c4cf6f #171a20 bold",
            "completion-menu.meta": "bg:#242933 #8f959f",
            "completion-menu.meta.completion.current": "bg:#c4cf6f #171a20",
            "scrollbar.background": "bg:#242933",
            "scrollbar.button": "bg:#c4cf6f",
            "bottom-toolbar": "bg:#20252e #aeb4be",
        }
    )


def slash_bottom_toolbar(repo: Path) -> Any:
    """Return a bottom toolbar similar to modern agent CLIs.

    Args:
        repo: Target repository path.
    """
    if HTML is None:
        return ""
    branch = ""
    with suppress(CliError):
        branch = run_command(["git", "branch", "--show-current"], cwd=repo, timeout=10)
    branch_text = branch or "detached"
    return HTML(
        "<style bg='#20252e'>"
        "<accent> Context:</accent> [..............] 12% | "
        f"<warning>{branch_text}</warning> | "
        "<muted>type / for commands, arrows to move, enter to select</muted>"
        "</style>"
    )


def prompt_key_bindings() -> Any:
    """Return prompt key bindings that open completions as soon as `/` is typed."""
    if KeyBindings is None:
        return None
    bindings = KeyBindings()

    @bindings.add("/")
    def _open_slash_completion(event: Any) -> None:
        event.current_buffer.insert_text("/")
        event.current_buffer.start_completion(select_first=False)

    return bindings


def normalize_effort(value: str) -> str:
    """Normalize human effort labels to CLI-compatible values.

    Args:
        value: Raw effort value.
    """
    normalized = value.strip().lower()
    return EFFORT_ALIASES.get(normalized, normalized)


def slugify(value: str, *, max_length: int = 32) -> str:
    """Create a branch-safe slug.

    Args:
        value: Raw string to slugify.
        max_length: Maximum output length.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return (slug or "task")[:max_length].strip("-") or "task"


def run_command(command: list[str], *, cwd: Path | None = None, timeout: int = 120) -> str:
    """Run a subprocess and return stdout.

    Args:
        command: Command and arguments.
        cwd: Optional working directory.
        timeout: Command timeout in seconds.
    """
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise CliError(f"command failed ({' '.join(command)}): {stderr}")
    return completed.stdout.strip()


def git_root(path: Path) -> Path:
    """Resolve the git root for a path.

    Args:
        path: Path inside a git repository.
    """
    output = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path)
    return Path(output).resolve()


def ensure_git_exclude(repo: Path, patterns: list[str]) -> None:
    """Add local-only ignore patterns to .git/info/exclude.

    Args:
        repo: Target repository path.
        patterns: Patterns to ensure.
    """
    exclude_path = repo / ".git" / "info" / "exclude"
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    additions = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if additions:
        with exclude_path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            for pattern in additions:
                handle.write(f"{pattern}\n")


def ccx_root(repo: Path) -> Path:
    """Return the repository-local ccx state root.

    Args:
        repo: Target repository path.
    """
    return repo / CCX_DIR_NAME


def runs_root(repo: Path) -> Path:
    """Return the repository-local run state root.

    Args:
        repo: Target repository path.
    """
    return ccx_root(repo) / RUNS_DIR_NAME


def run_state_root(repo: Path, run_id: str) -> Path:
    """Return the state directory for a run.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
    """
    return runs_root(repo) / run_id


def current_run_path(repo: Path) -> Path:
    """Return the current-run pointer path.

    Args:
        repo: Target repository path.
    """
    return ccx_root(repo) / CURRENT_RUN_FILE


def write_current_run(repo: Path, run_id: str) -> None:
    """Persist the current run pointer.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
    """
    ccx_root(repo).mkdir(parents=True, exist_ok=True)
    current_run_path(repo).write_text(run_id + "\n", encoding="utf-8")


def read_current_run(repo: Path) -> str:
    """Read the current run pointer.

    Args:
        repo: Target repository path.
    """
    path = current_run_path(repo)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def list_runs(repo: Path) -> list[str]:
    """List known run identifiers.

    Args:
        repo: Target repository path.
    """
    root = runs_root(repo)
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def resolve_state_paths(repo: Path, run_id: str | None = None) -> StatePaths:
    """Resolve the state directory for a run or compatibility state.

    Args:
        repo: Target repository path.
        run_id: Optional run identifier.
    """
    root = git_root(repo)
    selected_run = run_id or read_current_run(root)
    if selected_run:
        return StatePaths(root, run_state_root(root, selected_run))
    legacy = StatePaths(root)
    if legacy.root.exists():
        return legacy
    return StatePaths(root, legacy.root)


def runtime_state_path(paths: StatePaths) -> Path:
    """Return the ccx runtime state file path.

    Args:
        paths: Shared orchestration paths.
    """
    return paths.root / "run-state.json"


def read_runtime_state(repo: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read the ccx runtime state file.

    Args:
        repo: Target repository path.
        run_id: Optional run identifier.
    """
    paths = resolve_state_paths(repo, run_id)
    state_path = runtime_state_path(paths)
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def write_runtime_state(repo: Path, state: dict[str, Any], run_id: str | None = None) -> Path:
    """Write the ccx runtime state file.

    Args:
        repo: Target repository path.
        state: Runtime state payload.
        run_id: Optional run identifier.
    """
    paths = resolve_state_paths(repo, run_id or state.get("run_id"))
    ensure_state_dirs(paths)
    state_path = runtime_state_path(paths)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state_path


def collect_repo_snapshot(repo: Path) -> str:
    """Collect bounded repository context for Claude planning.

    Args:
        repo: Target repository path.
    """
    branch = run_command(["git", "branch", "--show-current"], cwd=repo)
    status = run_command(["git", "status", "--short"], cwd=repo)
    files = run_command(["git", "ls-files"], cwd=repo).splitlines()
    bounded_files = files[:400]
    overflow = len(files) - len(bounded_files)
    file_text = "\n".join(f"- {path}" for path in bounded_files)
    if overflow > 0:
        file_text += f"\n- ... {overflow} more files omitted"
    return f"""Repository: {repo}
Current branch: {branch or "(detached)"}
Git status:
{status or "(clean)"}

Tracked files sample:
{file_text or "(no tracked files)"}
"""


def planner_schema() -> str:
    """Return the JSON schema required from Claude planner."""
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["summary", "worker_count", "tasks"],
        "properties": {
            "summary": {"type": "string"},
            "worker_count": {"type": "integer", "minimum": 1, "maximum": MAX_AUTO_WORKERS},
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_AUTO_WORKERS,
                "items": {
                    "type": "object",
                    "required": [
                        "title",
                        "objective",
                        "owned_scope",
                        "non_goals",
                        "required_tests",
                    ],
                    "properties": {
                        "title": {"type": "string"},
                        "objective": {"type": "string"},
                        "owned_scope": {"type": "array", "items": {"type": "string"}},
                        "non_goals": {"type": "array", "items": {"type": "string"}},
                        "required_tests": {"type": "array", "items": {"type": "string"}},
                        "risks": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }
    return json.dumps(schema)


def planner_prompt(config: RunConfig, snapshot: str) -> str:
    """Build the Claude planning prompt.

    Args:
        config: Runner configuration.
        snapshot: Bounded repository context.
    """
    worker_hint = (
        f"Use exactly {config.requested_workers} workers."
        if config.requested_workers
        else f"Choose 1-{MAX_AUTO_WORKERS} Codex workers."
    )
    return f"""You are the Claude Opus conductor for a Claude + Codex cmux workflow.

User request:
{config.request}

Repository context:
{snapshot}

Decide how many Codex workers should implement this. {worker_hint}
Prefer fewer workers unless task boundaries are truly independent.
Split by file/module ownership to minimize merge conflicts.
Return only structured JSON matching the provided schema.
"""


def parse_json_object(raw_output: str) -> dict[str, Any]:
    """Parse a JSON object from Claude output.

    Args:
        raw_output: Claude stdout.
    """
    try:
        value = json.loads(raw_output)
    except json.JSONDecodeError:
        start = raw_output.find("{")
        end = raw_output.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise CliError("Claude did not return JSON plan") from None
        value = json.loads(raw_output[start : end + 1])
    if not isinstance(value, dict):
        raise CliError("Claude plan was not a JSON object")
    return value


def request_plan(config: RunConfig) -> dict[str, Any]:
    """Ask Claude for a worker decomposition plan.

    Args:
        config: Runner configuration.
    """
    snapshot = collect_repo_snapshot(config.repo)
    command = [
        "claude",
        "--print",
        "--model",
        config.claude_model,
        "--effort",
        config.claude_effort,
        "--json-schema",
        planner_schema(),
        planner_prompt(config, snapshot),
    ]
    output = run_command(command, cwd=config.repo, timeout=240)
    return parse_json_object(output)


def normalize_plan(
    raw_plan: dict[str, Any], config: RunConfig, run_id: str, worktree_root: Path
) -> Plan:
    """Normalize Claude planner output into internal tasks.

    Args:
        raw_plan: Parsed planner JSON.
        config: Runner configuration.
        run_id: Run identifier.
        worktree_root: Base worktree directory.
    """
    raw_tasks = raw_plan.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise CliError("Claude plan did not include tasks")
    if config.requested_workers:
        raw_tasks = raw_tasks[: config.requested_workers]
    raw_tasks = raw_tasks[:MAX_AUTO_WORKERS]

    tasks: list[WorkerTask] = []
    for index, item in enumerate(raw_tasks, start=1):
        if not isinstance(item, dict):
            raise CliError("Claude task item was not an object")
        worker_id = f"worker-{index:02d}"
        title = str(item.get("title") or f"Task {index}").strip()
        branch = f"ccx/{run_id}/{worker_id}"
        worktree = worktree_root / worker_id
        tasks.append(
            WorkerTask(
                worker_id=worker_id,
                title=title,
                objective=str(item.get("objective") or title).strip(),
                owned_scope=[str(value) for value in item.get("owned_scope", [])],
                non_goals=[str(value) for value in item.get("non_goals", [])],
                required_tests=[str(value) for value in item.get("required_tests", [])],
                risks=[str(value) for value in item.get("risks", [])],
                branch=branch,
                worktree=worktree,
            )
        )
    return Plan(
        summary=str(raw_plan.get("summary") or config.request).strip(),
        worker_count=len(tasks),
        tasks=tasks,
    )


def markdown_list(items: list[str]) -> str:
    """Render a markdown list with a stable empty state.

    Args:
        items: Items to render.
    """
    return "\n".join(f"- {item}" for item in items) if items else "- None"


def write_orchestrator_state(
    config: RunConfig, plan: Plan, run_id: str, integration_worktree: Path
) -> StatePaths:
    """Write shared orchestration state files.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        run_id: Run identifier.
        integration_worktree: Integration worktree path.
    """
    paths = StatePaths(config.repo, run_state_root(config.repo, run_id))
    if paths.root.exists() and any(paths.root.iterdir()) and not config.force_state:
        raise CliError(f"state already exists, pass --force-state to overwrite: {paths.root}")
    ensure_state_dirs(paths)
    (paths.root / "prompts").mkdir(parents=True, exist_ok=True)

    plan_content = f"""# Orchestration Plan

- Run: {run_id}
- Target repo: {config.repo}
- Integration worktree: {integration_worktree}
- Worker count: {plan.worker_count}
- Status: planned

## User Request

{config.request}

## Summary

{plan.summary}

## Decomposition

"""
    for task in plan.tasks:
        plan_content += f"- {task.worker_id}: {task.title}\n"
    write_text(paths.root / "plan.md", plan_content, force=config.force_state)

    worktrees_content = f"""# Worktrees

## Integration

- Branch: ccx/{run_id}/integration
- Path: {integration_worktree}

## Workers

"""
    for task in plan.tasks:
        worktrees_content += f"- {task.worker_id}: {task.worktree} ({task.branch})\n"
    write_text(paths.root / "worktrees.md", worktrees_content, force=config.force_state)

    for task in plan.tasks:
        task_content = f"""# Worker Task

## Worker

- ID: {task.worker_id}
- Branch: {task.branch}
- Worktree: {task.worktree}

## Objective

{task.objective}

## Owned Scope

{markdown_list(task.owned_scope)}

## Non-Goals

{markdown_list(task.non_goals)}

## Validation Requirements

1. Confirm this scope is coherent.
2. Confirm this scope does not overlap with other workers.
3. Identify missing context before implementation.

## Implementation Requirements

Do not edit code until {paths.approval_file} exists.
Work only in this worktree: {task.worktree}
Write questions and handoff files to the shared state directory: {paths.root}

## Required Tests

{markdown_list(task.required_tests)}

## Risks

{markdown_list(task.risks)}

## Handoff Path

{paths.handoffs / f"{task.worker_id}.md"}
"""
        write_text(paths.tasks / f"{task.worker_id}.md", task_content, force=config.force_state)
    return paths


def create_worktrees(repo: Path, run_id: str, plan: Plan, integration_worktree: Path) -> None:
    """Create integration and worker git worktrees.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
        plan: Normalized run plan.
        integration_worktree: Integration worktree path.
    """
    integration_worktree.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "git",
            "worktree",
            "add",
            "-b",
            f"ccx/{run_id}/integration",
            str(integration_worktree),
            "HEAD",
        ],
        cwd=repo,
    )
    for task in plan.tasks:
        task.worktree.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            ["git", "worktree", "add", "-b", task.branch, str(task.worktree), "HEAD"],
            cwd=repo,
        )


def interrupt_recovery_prompt(config: RunConfig, run_id: str) -> str:
    """Return prompt rules for recovering stale run state after user interrupts.

    Args:
        config: Runner configuration.
        run_id: Run identifier.
    """
    status_command = f"ccx status {config.repo} --run {run_id} --json"
    stop_command = f"ccx stop {config.repo} --run {run_id}"
    lines = [
        "Interrupt recovery:",
        "- Ctrl-C is handled by the ccx agent wrapper and should mark this run stopped "
        "automatically.",
        "- Esc may interrupt Claude/Codex without notifying ccx, leaving stale `running` state.",
        f"- Before resuming work after any explicit user interrupt, run: {status_command}",
        "- If you are recovering from an explicit user interrupt and status is still "
        f"`running`, run: {stop_command}",
        "- Never stop the run only because status is `running`; only stop it when you are "
        "recovering from a user interrupt or stale interrupted agent state.",
    ]
    return "\n".join(lines)


def conductor_prompt(
    config: RunConfig,
    plan: Plan,
    paths: StatePaths,
    integration_worktree: Path,
    run_id: str,
) -> str:
    """Build the interactive Claude conductor prompt.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        paths: Shared state paths.
        integration_worktree: Integration worktree path.
        run_id: Run identifier.
    """
    return f"""You are the Claude conductor for this ccx run.

Model role:
- You plan, arbitrate, review, integrate, test, commit, push, and open PRs.
- Codex workers implement in their isolated worktrees.

User request:
{config.request}

Shared state directory:
{paths.root}

Integration worktree:
{integration_worktree}

Plan summary:
{plan.summary}

Hard workflow:
1. Review worker validations in {paths.validations}.
2. If questions appear in {paths.questions}, resolve them before approval.
3. Only after consensus, run: ccx approve {config.repo}
4. Workers must not implement before {paths.approval_file} exists.
5. Review handoffs in {paths.handoffs} as they arrive.
6. Integrate worker branches into {integration_worktree}.
7. Resolve conflicts or reassign focused fixes.
8. Run formatting, linting, and tests.
9. Split coherent commits and push a branch/PR.
10. Do not merge without explicit human approval.

{interrupt_recovery_prompt(config, run_id)}

Start by reading {paths.root / "plan.md"} and the task files.
"""


def worker_prompt(config: RunConfig, task: WorkerTask, paths: StatePaths, run_id: str) -> str:
    """Build the interactive Codex worker prompt.

    Args:
        config: Runner configuration.
        task: Worker task.
        paths: Shared state paths.
        run_id: Run identifier.
    """
    return f"""You are {task.worker_id}, a Codex worker in a ccx Claude + Codex run.

Target request:
{config.request}

Shared state directory:
{paths.root}

Your task file:
{paths.tasks / f"{task.worker_id}.md"}

Hard rules:
1. First validate your task boundary only. Do not edit code yet.
2. Write validation to: {paths.validations / f"{task.worker_id}.md"}
3. If anything is ambiguous, overlapping, or risky, write a question under: {paths.questions}
4. Do not implement until this approval barrier exists: {paths.approval_file}
5. After approval, work only in this worktree: {task.worktree}
6. If uncertainty appears during implementation, pause only yourself and write a question.
7. On completion, write handoff to: {paths.handoffs / f"{task.worker_id}.md"}
8. Do not merge or push.

{interrupt_recovery_prompt(config, run_id)}

Begin by reading your task file and producing validation.
"""


def write_prompt_files(
    config: RunConfig,
    plan: Plan,
    paths: StatePaths,
    integration_worktree: Path,
    run_id: str,
) -> dict[str, Path]:
    """Write conductor and worker prompt files.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        paths: Shared state paths.
        integration_worktree: Integration worktree path.
        run_id: Run identifier.
    """
    prompt_dir = paths.root / "prompts"
    prompt_paths: dict[str, Path] = {}
    conductor_path = prompt_dir / "claude-conductor.md"
    write_text(
        conductor_path,
        conductor_prompt(config, plan, paths, integration_worktree, run_id),
        force=config.force_state,
    )
    prompt_paths["conductor"] = conductor_path
    for task in plan.tasks:
        path = prompt_dir / f"{task.worker_id}.md"
        write_text(path, worker_prompt(config, task, paths, run_id), force=config.force_state)
        prompt_paths[task.worker_id] = path
    return prompt_paths


def ccx_executable() -> str:
    """Return the ccx executable used inside launched cmux panes."""
    return os.environ.get("CCX_BIN", DEFAULT_CCX_BIN)


def agent_command_with_prompt(
    *,
    repo: Path,
    run_id: str,
    role: str,
    prompt_path: Path,
    child_command: list[str],
    worker_id: str | None = None,
) -> str:
    """Build a shell command that runs a child agent through the ccx wrapper.

    Args:
        repo: Target repository path used for run state updates.
        run_id: Run identifier.
        role: Agent role, such as conductor or worker.
        prompt_path: Prompt file to read at runtime.
        child_command: Claude or Codex command arguments before the prompt.
        worker_id: Optional worker identifier.
    """
    command = [
        ccx_executable(),
        "agent",
        "--repo",
        str(repo),
        "--run",
        run_id,
        "--role",
        role,
        "--prompt",
        str(prompt_path),
    ]
    if worker_id:
        command.extend(["--worker-id", worker_id])
    command.extend(["--", *child_command])
    return shlex.join(command)


def parse_ref(output: str, prefix: str) -> str:
    """Parse a cmux ref from command output.

    Args:
        output: cmux command output.
        prefix: Expected ref prefix, such as workspace or pane.
    """
    match = re.search(rf"{re.escape(prefix)}:[0-9]+", output)
    if not match:
        raise CliError(f"could not parse cmux {prefix} ref from output: {output}")
    return match.group(0)


def parse_ref_or_none(output: str, prefix: str) -> str | None:
    """Parse a cmux ref if present.

    Args:
        output: cmux command output.
        prefix: Expected ref prefix.
    """
    match = re.search(rf"{re.escape(prefix)}:[0-9]+", output)
    return match.group(0) if match else None


def focused_pane_ref(output: str) -> str:
    """Parse the focused or last pane ref from list-panes output.

    Args:
        output: cmux list-panes output.
    """
    fallback: str | None = None
    for line in output.splitlines():
        pane = parse_ref_or_none(line, "pane")
        if not pane:
            continue
        fallback = pane
        if line.lstrip().startswith("*"):
            return pane
    if not fallback:
        raise CliError(f"could not parse cmux pane ref from output: {output}")
    return fallback


def first_surface_ref(output: str) -> str:
    """Parse the first cmux surface ref from output.

    Args:
        output: cmux command output.
    """
    match = re.search(r"surface:[0-9]+", output)
    if not match:
        raise CliError(f"could not parse cmux surface ref from output: {output}")
    return match.group(0)


def launch_cmux(
    config: RunConfig,
    plan: Plan,
    paths: StatePaths,
    prompt_paths: dict[str, Path],
    run_id: str,
    integration_worktree: Path,
) -> str:
    """Launch Claude conductor and Codex worker panes in cmux.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        paths: Shared state paths.
        prompt_paths: Prompt files by role or worker ID.
        run_id: Run identifier.
        integration_worktree: Integration worktree path.
    """
    conductor_command = agent_command_with_prompt(
        repo=config.repo,
        run_id=run_id,
        role="conductor",
        prompt_path=prompt_paths["conductor"],
        child_command=[
            "claude",
            "--model",
            config.claude_model,
            "--effort",
            config.claude_effort,
            "--add-dir",
            str(config.repo),
            "--add-dir",
            str(paths.root),
        ],
    )
    workspace_output = run_command(
        [
            "cmux",
            "new-workspace",
            "--name",
            f"ccx {config.repo.name} {run_id}",
            "--cwd",
            str(integration_worktree),
            "--command",
            conductor_command,
        ],
        cwd=config.repo,
        timeout=30,
    )
    workspace = parse_ref_or_none(workspace_output, "workspace")
    if not workspace:
        workspace = run_command(["cmux", "current-workspace"], cwd=config.repo, timeout=30)

    directions = ["right", "down", "right", "down", "right", "down"]
    for index, task in enumerate(plan.tasks):
        pane_output = run_command(
            [
                "cmux",
                "new-pane",
                "--workspace",
                workspace,
                "--direction",
                directions[index % len(directions)],
            ],
            cwd=config.repo,
            timeout=30,
        )
        pane = parse_ref_or_none(pane_output, "pane")
        if not pane:
            panes_output = run_command(
                ["cmux", "list-panes", "--workspace", workspace],
                cwd=config.repo,
                timeout=30,
            )
            pane = focused_pane_ref(panes_output)
        surface_output = run_command(
            ["cmux", "list-pane-surfaces", "--workspace", workspace, "--pane", pane],
            cwd=config.repo,
            timeout=30,
        )
        surface = first_surface_ref(surface_output)
        codex_command = agent_command_with_prompt(
            repo=config.repo,
            run_id=run_id,
            role="worker",
            worker_id=task.worker_id,
            prompt_path=prompt_paths[task.worker_id],
            child_command=[
                "codex",
                "--model",
                config.codex_model,
                "-c",
                f'model_reasoning_effort="{config.codex_effort}"',
                "--cd",
                str(task.worktree),
            ],
        )
        run_command(
            [
                "cmux",
                "respawn-pane",
                "--workspace",
                workspace,
                "--surface",
                surface,
                "--command",
                codex_command,
            ],
            cwd=config.repo,
            timeout=30,
        )
    return workspace


def runtime_counts(paths: StatePaths) -> dict[str, int]:
    """Count state files in the shared runtime directory.

    Args:
        paths: Shared orchestration paths.
    """
    tasks = list(paths.tasks.glob("*.md")) if paths.tasks.exists() else []
    validations = list(paths.validations.glob("*.md")) if paths.validations.exists() else []
    questions = [
        path
        for path in paths.questions.glob("*.md")
        if paths.questions.exists() and path.parent == paths.questions
    ]
    resolved = (
        list(paths.resolved_questions.glob("*.md")) if paths.resolved_questions.exists() else []
    )
    handoffs = list(paths.handoffs.glob("*.md")) if paths.handoffs.exists() else []
    return {
        "tasks": len(tasks),
        "validations": len(validations),
        "questions": len(questions),
        "resolved_questions": len(resolved),
        "handoffs": len(handoffs),
    }


def runtime_status(repo: Path, run_id: str | None = None) -> dict[str, Any]:
    """Return current ccx runtime status.

    Args:
        repo: Target repository path.
        run_id: Optional run identifier.
    """
    root = git_root(repo)
    paths = resolve_state_paths(root, run_id)
    state = read_runtime_state(root, run_id)
    counts = runtime_counts(paths)
    return {
        "repo": str(root),
        "state_dir": str(paths.root),
        "has_state": bool(state),
        "status": state.get("status", "not-started"),
        "run_id": state.get("run_id", ""),
        "current_run": read_current_run(root),
        "runs": list_runs(root),
        "request": state.get("request", ""),
        "cmux_workspace": state.get("cmux_workspace", ""),
        "stopped_at": state.get("stopped_at", ""),
        "stopped_by": state.get("stopped_by", ""),
        "stop_reason": state.get("stop_reason", ""),
        "stopped_agent": state.get("stopped_agent", ""),
        "approved": paths.approval_file.exists(),
        "counts": counts,
        "workers": state.get("workers", []),
    }


def format_runtime_status(status: dict[str, Any]) -> str:
    """Format runtime status for terminal output.

    Args:
        status: Runtime status payload.
    """
    counts = status["counts"]
    lines = [
        f"repo: {status['repo']}",
        f"state: {status['state_dir']}",
        f"status: {status['status']}",
    ]
    if status["run_id"]:
        lines.append(f"run: {status['run_id']}")
    if status["current_run"]:
        lines.append(f"current: {status['current_run']}")
    if status["runs"]:
        lines.append(f"runs: {', '.join(status['runs'])}")
    if status["cmux_workspace"]:
        lines.append(f"cmux workspace: {status['cmux_workspace']}")
    if status["stopped_at"]:
        lines.append(f"stopped at: {status['stopped_at']}")
    if status["stopped_by"]:
        detail = status["stopped_by"]
        if status["stop_reason"]:
            detail += f" ({status['stop_reason']})"
        if status["stopped_agent"]:
            detail += f" by {status['stopped_agent']}"
        lines.append(f"stopped by: {detail}")
    if status["request"]:
        lines.append(f"request: {status['request']}")
    lines.extend(
        [
            f"approved: {'yes' if status['approved'] else 'no'}",
            f"validations: {counts['validations']}/{counts['tasks']}",
            f"questions: {counts['questions']} open, {counts['resolved_questions']} resolved",
            f"handoffs: {counts['handoffs']}/{counts['tasks']}",
        ]
    )
    return "\n".join(lines)


def print_runtime_status(repo: Path, *, as_json: bool = False, run_id: str | None = None) -> int:
    """Print current ccx runtime status.

    Args:
        repo: Target repository path.
        as_json: Whether to print JSON.
        run_id: Optional run identifier.
    """
    status = runtime_status(repo, run_id)
    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(format_runtime_status(status))
    return 0


def watch_runtime(
    repo: Path,
    *,
    interval: float,
    once: bool = False,
    max_ticks: int = 0,
    run_id: str | None = None,
) -> int:
    """Watch runtime status until interrupted.

    Args:
        repo: Target repository path.
        interval: Poll interval in seconds.
        once: Whether to print one snapshot and exit.
        max_ticks: Optional maximum number of polling iterations.
        run_id: Optional run identifier.
    """
    ticks = 0
    while True:
        print(format_runtime_status(runtime_status(repo, run_id)))
        ticks += 1
        if once or (max_ticks and ticks >= max_ticks):
            return 0
        print("")
        time.sleep(interval)


def launch_cmux_from_state(repo: Path, state: dict[str, Any], paths: StatePaths) -> str:
    """Relaunch conductor and worker panes from persisted state.

    Args:
        repo: Target repository path.
        state: Runtime state payload.
        paths: Shared state paths.
    """
    prompt_dir = paths.root / "prompts"
    conductor_prompt_path = prompt_dir / "claude-conductor.md"
    if not conductor_prompt_path.exists():
        raise CliError(f"missing conductor prompt: {conductor_prompt_path}")
    integration = state.get("integration", {})
    integration_worktree = Path(integration.get("worktree", ""))
    if not integration_worktree.exists():
        raise CliError(f"missing integration worktree: {integration_worktree}")
    models = state.get("models", {})
    run_id = str(state.get("run_id") or read_current_run(repo))
    if not run_id:
        raise CliError("missing run id in runtime state")
    conductor_command = agent_command_with_prompt(
        repo=repo,
        run_id=run_id,
        role="conductor",
        prompt_path=conductor_prompt_path,
        child_command=[
            "claude",
            "--model",
            str(models.get("claude") or "opus"),
            "--effort",
            str(models.get("claude_effort") or "medium"),
            "--add-dir",
            str(repo),
            "--add-dir",
            str(paths.root),
        ],
    )
    workspace_output = run_command(
        [
            "cmux",
            "new-workspace",
            "--name",
            f"ccx {repo.name} resume",
            "--cwd",
            str(integration_worktree),
            "--command",
            conductor_command,
        ],
        cwd=repo,
        timeout=30,
    )
    workspace = parse_ref_or_none(workspace_output, "workspace")
    if not workspace:
        workspace = run_command(["cmux", "current-workspace"], cwd=repo, timeout=30)

    directions = ["right", "down", "right", "down", "right", "down"]
    for index, worker in enumerate(state.get("workers", [])):
        worker_id = str(worker.get("id"))
        prompt_path = prompt_dir / f"{worker_id}.md"
        worktree = Path(str(worker.get("worktree", "")))
        if not prompt_path.exists() or not worktree.exists():
            continue
        pane_output = run_command(
            [
                "cmux",
                "new-pane",
                "--workspace",
                workspace,
                "--direction",
                directions[index % len(directions)],
            ],
            cwd=repo,
            timeout=30,
        )
        pane = parse_ref_or_none(pane_output, "pane")
        if not pane:
            panes_output = run_command(
                ["cmux", "list-panes", "--workspace", workspace],
                cwd=repo,
                timeout=30,
            )
            pane = focused_pane_ref(panes_output)
        surface_output = run_command(
            ["cmux", "list-pane-surfaces", "--workspace", workspace, "--pane", pane],
            cwd=repo,
            timeout=30,
        )
        surface = first_surface_ref(surface_output)
        codex_command = agent_command_with_prompt(
            repo=repo,
            run_id=run_id,
            role="worker",
            worker_id=worker_id,
            prompt_path=prompt_path,
            child_command=[
                "codex",
                "--model",
                str(models.get("codex") or "gpt-5.3-codex"),
                "-c",
                f'model_reasoning_effort="{models.get("codex_effort") or "medium"}"',
                "--cd",
                str(worktree),
            ],
        )
        run_command(
            [
                "cmux",
                "respawn-pane",
                "--workspace",
                workspace,
                "--surface",
                surface,
                "--command",
                codex_command,
            ],
            cwd=repo,
            timeout=30,
        )
    return workspace


def resume_runtime(repo: Path, run_id: str | None = None) -> int:
    """Resume a previous ccx run in a new cmux workspace.

    Args:
        repo: Target repository path.
        run_id: Optional run identifier.
    """
    root = git_root(repo)
    paths = resolve_state_paths(root, run_id)
    state = read_runtime_state(root, run_id)
    if not state:
        raise CliError("no ccx runtime state found")
    workspace = launch_cmux_from_state(root, state, paths)
    state["status"] = "running"
    state["cmux_workspace"] = workspace
    state["resumed_at"] = datetime.now(UTC).isoformat()
    write_current_run(root, state["run_id"])
    write_runtime_state(root, state, state["run_id"])
    print(f"resumed ccx workspace: {workspace}")
    return 0


def mark_runtime_stopped(
    repo: Path,
    *,
    close_cmux: bool = False,
    run_id: str | None = None,
    stopped_by: str = "cli",
    stop_reason: str = "",
    stopped_agent: str = "",
) -> dict[str, Any]:
    """Mark a ccx run as stopped and optionally close its cmux workspace.

    Args:
        repo: Target repository path.
        close_cmux: Whether to close the cmux workspace.
        run_id: Optional run identifier.
        stopped_by: Actor or mechanism that stopped the run.
        stop_reason: Optional machine-readable stop reason.
        stopped_agent: Optional agent label that observed the stop.
    """
    root = git_root(repo)
    state = read_runtime_state(root, run_id)
    if not state:
        raise CliError("no ccx runtime state found")
    workspace = state.get("cmux_workspace")
    if close_cmux and workspace:
        run_command(
            ["cmux", "close-workspace", "--workspace", str(workspace)], cwd=root, timeout=30
        )
    state["status"] = "stopped"
    state["stopped_at"] = datetime.now(UTC).isoformat()
    state["stopped_by"] = stopped_by
    if stop_reason:
        state["stop_reason"] = stop_reason
    if stopped_agent:
        state["stopped_agent"] = stopped_agent
    write_runtime_state(root, state, state["run_id"])
    return state


def stop_runtime(repo: Path, *, close_cmux: bool = False, run_id: str | None = None) -> int:
    """Stop a ccx run from the CLI.

    Args:
        repo: Target repository path.
        close_cmux: Whether to close the cmux workspace.
        run_id: Optional run identifier.
    """
    mark_runtime_stopped(repo, close_cmux=close_cmux, run_id=run_id, stopped_by="cli")
    print("stopped ccx run")
    return 0


def signal_name(signum: int) -> str:
    """Return a readable signal name.

    Args:
        signum: Numeric signal value.
    """
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal-{signum}"


def run_agent_wrapper(
    *,
    repo: Path,
    run_id: str,
    role: str,
    prompt_path: Path,
    child_command: list[str],
    worker_id: str | None = None,
) -> int:
    """Run a Claude or Codex child process and stop ccx on interrupt.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
        role: Agent role.
        prompt_path: Prompt file appended as the final child argument.
        child_command: Child command arguments.
        worker_id: Optional worker identifier.
    """
    root = git_root(repo)
    if not prompt_path.exists():
        raise CliError(f"prompt file does not exist: {prompt_path}")
    if not child_command:
        raise CliError("agent child command is required")

    prompt = prompt_path.read_text(encoding="utf-8")
    command = [*child_command, prompt]
    agent_label = f"{role}:{worker_id}" if worker_id else role
    child: subprocess.Popen[Any] | None = None
    stopped = False

    def record_stop(reason: str) -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        try:
            mark_runtime_stopped(
                root,
                run_id=run_id,
                stopped_by="signal",
                stop_reason=reason,
                stopped_agent=agent_label,
            )
            print(
                f"\nccx: marked run {run_id} stopped after {reason} from {agent_label}",
                file=sys.stderr,
                flush=True,
            )
        except CliError as exc:
            print(f"\nccx: failed to mark run stopped: {exc}", file=sys.stderr, flush=True)

    def handle_signal(signum: int, _frame: Any) -> None:
        reason = signal_name(signum)
        record_stop(reason)
        if child and child.poll() is None:
            with suppress(ProcessLookupError):
                child.send_signal(signum)

    previous_handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        child = subprocess.Popen(command)
        return_code = child.wait()
    except FileNotFoundError as exc:
        raise CliError(f"agent child command not found: {child_command[0]}") from exc
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)

    if return_code < 0:
        reason = signal_name(-return_code)
        if reason in {"SIGINT", "SIGTERM"}:
            record_stop(reason)
    return return_code


def show_slash_menu() -> None:
    """Print the ccx slash command preview for the pre-launch prompt."""
    install_claude_commands()
    print("Claude native commands and ccx commands:")
    for command in slash_commands():
        source = f"{command.source}:".ljust(8)
        print(f"  {command.trigger.ljust(16)} {source} {command.description}")
    print("In the interactive prompt, type / and use arrow keys to choose.")


def handle_slash_command(raw_command: str, repo: Path) -> str | None:
    """Handle a selected slash command in the pre-launch ccx prompt.

    Args:
        raw_command: Slash command text returned by the prompt.
        repo: Current target repository.
    """
    command_name = raw_command.strip().split(maxsplit=1)[0]
    command = next((item for item in slash_commands() if item.trigger == command_name), None)
    if command is None:
        print(f"unknown slash command: {command_name}")
        return None
    if command.action == "exit":
        return ""
    if command.action == "status":
        print_runtime_status(repo)
        return None
    if command.action == "doctor":
        print("Run `ccx doctor` from another shell to check local dependencies.")
        return None
    if command.source == "ccx":
        print(f"Selected {command.description}. Run: ccx {command.action} {repo}")
        return None
    print(
        f"Selected Claude command {command.trigger}. "
        "It is available inside the launched Claude conductor session."
    )
    return None


def run_orchestration(config: RunConfig) -> int:
    """Run the full interactive orchestration bootstrap.

    Args:
        config: Runner configuration.
    """
    repo = git_root(config.repo)
    install_claude_commands()
    config = RunConfig(
        repo=repo,
        request=config.request,
        claude_model=config.claude_model,
        claude_effort=normalize_effort(config.claude_effort),
        codex_model=config.codex_model,
        codex_effort=normalize_effort(config.codex_effort),
        requested_workers=config.requested_workers,
        dry_run=config.dry_run,
        skip_launch=config.skip_launch,
        force_state=config.force_state,
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    run_id = f"{timestamp}-{slugify(config.request)}"
    worktree_root = config.repo.parent / ".ccx-worktrees" / config.repo.name / run_id
    integration_worktree = worktree_root / "integration"

    print("ccx: asking Claude to decompose the task...")
    raw_plan = request_plan(config)
    plan = normalize_plan(raw_plan, config, run_id, worktree_root)
    print(f"ccx: Claude selected {plan.worker_count} Codex worker(s).")

    if config.dry_run:
        print(json.dumps(raw_plan, indent=2, sort_keys=True))
        return 0

    ensure_git_exclude(config.repo, [".orchestrator/", ".ccx-worktrees/"])
    paths = write_orchestrator_state(config, plan, run_id, integration_worktree)
    write_current_run(config.repo, run_id)
    print(f"ccx: wrote shared state to {paths.root}")
    state = {
        "status": "prepared",
        "run_id": run_id,
        "repo": str(config.repo),
        "request": config.request,
        "created_at": datetime.now(UTC).isoformat(),
        "integration": {
            "branch": f"ccx/{run_id}/integration",
            "worktree": str(integration_worktree),
        },
        "models": {
            "claude": config.claude_model,
            "claude_effort": config.claude_effort,
            "codex": config.codex_model,
            "codex_effort": config.codex_effort,
        },
        "workers": [
            {
                "id": task.worker_id,
                "branch": task.branch,
                "worktree": str(task.worktree),
            }
            for task in plan.tasks
        ],
    }
    write_runtime_state(config.repo, state, run_id)

    create_worktrees(config.repo, run_id, plan, integration_worktree)
    print(f"ccx: created worktrees under {worktree_root}")

    prompt_paths = write_prompt_files(config, plan, paths, integration_worktree, run_id)
    print(f"ccx: wrote prompts to {paths.root / 'prompts'}")

    if config.skip_launch:
        print("ccx: skipped cmux launch")
        return 0

    workspace = launch_cmux(config, plan, paths, prompt_paths, run_id, integration_worktree)
    state["status"] = "running"
    state["cmux_workspace"] = workspace
    state["launched_at"] = datetime.now(UTC).isoformat()
    write_runtime_state(config.repo, state, run_id)
    print(f"ccx: launched cmux workspace {workspace}")
    return 0


def prompt_for_request(repo: Path) -> str:
    """Prompt the user for the implementation request."""
    install_claude_commands()
    print("ccx interactive orchestrator")
    print("Describe what you want Claude to plan and Codex workers to implement.")
    print('Type "/" for Claude + ccx commands, then use arrow keys to choose.')
    if PromptSession is not None and sys.stdin.isatty() and sys.stdout.isatty():
        session = PromptSession(
            completer=SlashCommandCompleter(slash_commands()),
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            key_bindings=prompt_key_bindings(),
            style=slash_command_style(),
            bottom_toolbar=lambda: slash_bottom_toolbar(repo),
        )
        while True:
            request = session.prompt([("class:prompt", "› ")]).strip()
            if request.startswith("/"):
                handled = handle_slash_command(request, repo)
                if handled == "":
                    return ""
                continue
            if request:
                return request

    while True:
        try:
            request = input("ccx> ").strip()
        except EOFError as exc:
            raise CliError("request is required when stdin is not interactive") from exc
        if request == "/":
            show_slash_menu()
            continue
        if request.startswith("/"):
            handled = handle_slash_command(request, repo)
            if handled == "":
                return ""
            continue
        if request in {"/exit", "/quit"}:
            return ""
        if request:
            return request


def interactive_default(cwd: Path) -> int:
    """Run default interactive mode when `ccx` has no arguments.

    Args:
        cwd: Current working directory.
    """
    request = prompt_for_request(cwd)
    if not request:
        print("ccx: no request provided")
        return 1
    config = RunConfig(
        repo=cwd,
        request=request,
        claude_model=os.environ.get("CCX_CLAUDE_MODEL", "opus"),
        claude_effort=os.environ.get("CCX_CLAUDE_EFFORT", "medium"),
        codex_model=os.environ.get("CCX_CODEX_MODEL", "gpt-5.3-codex"),
        codex_effort=os.environ.get("CCX_CODEX_EFFORT", "medium"),
        requested_workers=None,
        dry_run=False,
        skip_launch=False,
        force_state=False,
    )
    return run_orchestration(config)

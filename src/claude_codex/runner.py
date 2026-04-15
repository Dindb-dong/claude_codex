"""Interactive orchestration runner for Claude conductor and Codex workers."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_codex.cli import CliError, StatePaths, ensure_state_dirs, write_text

MAX_AUTO_WORKERS = 6
EFFORT_ALIASES = {"normal": "medium", "med": "medium"}


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
    paths = StatePaths(config.repo)
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


def conductor_prompt(
    config: RunConfig, plan: Plan, paths: StatePaths, integration_worktree: Path
) -> str:
    """Build the interactive Claude conductor prompt.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        paths: Shared state paths.
        integration_worktree: Integration worktree path.
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

Start by reading {paths.root / "plan.md"} and the task files.
"""


def worker_prompt(config: RunConfig, task: WorkerTask, paths: StatePaths) -> str:
    """Build the interactive Codex worker prompt.

    Args:
        config: Runner configuration.
        task: Worker task.
        paths: Shared state paths.
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

Begin by reading your task file and producing validation.
"""


def write_prompt_files(
    config: RunConfig,
    plan: Plan,
    paths: StatePaths,
    integration_worktree: Path,
) -> dict[str, Path]:
    """Write conductor and worker prompt files.

    Args:
        config: Runner configuration.
        plan: Normalized run plan.
        paths: Shared state paths.
        integration_worktree: Integration worktree path.
    """
    prompt_dir = paths.root / "prompts"
    prompt_paths: dict[str, Path] = {}
    conductor_path = prompt_dir / "claude-conductor.md"
    write_text(
        conductor_path,
        conductor_prompt(config, plan, paths, integration_worktree),
        force=config.force_state,
    )
    prompt_paths["conductor"] = conductor_path
    for task in plan.tasks:
        path = prompt_dir / f"{task.worker_id}.md"
        write_text(path, worker_prompt(config, task, paths), force=config.force_state)
        prompt_paths[task.worker_id] = path
    return prompt_paths


def command_with_prompt(base_command: list[str], prompt_path: Path) -> str:
    """Build a shell command that injects a prompt file as one argument.

    Args:
        base_command: Command arguments before the prompt.
        prompt_path: Prompt file to read at runtime.
    """
    return f'{shlex.join(base_command)} "$(cat {shlex.quote(str(prompt_path))})"'


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
    conductor_command = command_with_prompt(
        [
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
        prompt_paths["conductor"],
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
        codex_command = command_with_prompt(
            [
                "codex",
                "--model",
                config.codex_model,
                "-c",
                f'model_reasoning_effort="{config.codex_effort}"',
                "--cd",
                str(task.worktree),
            ],
            prompt_paths[task.worker_id],
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


def run_orchestration(config: RunConfig) -> int:
    """Run the full interactive orchestration bootstrap.

    Args:
        config: Runner configuration.
    """
    repo = git_root(config.repo)
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
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
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
    print(f"ccx: wrote shared state to {paths.root}")

    create_worktrees(config.repo, run_id, plan, integration_worktree)
    print(f"ccx: created worktrees under {worktree_root}")

    prompt_paths = write_prompt_files(config, plan, paths, integration_worktree)
    print(f"ccx: wrote prompts to {paths.root / 'prompts'}")

    if config.skip_launch:
        print("ccx: skipped cmux launch")
        return 0

    workspace = launch_cmux(config, plan, paths, prompt_paths, run_id, integration_worktree)
    print(f"ccx: launched cmux workspace {workspace}")
    return 0


def prompt_for_request() -> str:
    """Prompt the user for the implementation request."""
    print("ccx interactive orchestrator")
    print("Describe what you want Claude to plan and Codex workers to implement.")
    return input("ccx> ").strip()


def interactive_default(cwd: Path) -> int:
    """Run default interactive mode when `ccx` has no arguments.

    Args:
        cwd: Current working directory.
    """
    request = prompt_for_request()
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

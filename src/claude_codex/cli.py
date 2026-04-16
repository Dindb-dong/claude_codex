"""Command line helpers for Claude conductor and Codex worker orchestration."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_codex.preflight import check_claude_auth

STATE_DIR_NAME = ".orchestrator"
RECOMMENDATIONS = {"approve", "revise", "reject"}
WORKER_ID_PATTERN = re.compile(r"^worker-[0-9]{2}$")


class CliError(Exception):
    """Expected user-facing command failure."""


@dataclass(frozen=True)
class StatePaths:
    """Resolved paths for an orchestration state directory.

    Args:
        repo: Target git repository path.
        state_root: Optional explicit state directory root.
    """

    repo: Path
    state_root: Path | None = None

    @property
    def root(self) -> Path:
        """Return the orchestration state root path."""
        if self.state_root is not None:
            return self.state_root
        return self.repo / STATE_DIR_NAME

    @property
    def tasks(self) -> Path:
        """Return the task directory path."""
        return self.root / "tasks"

    @property
    def validations(self) -> Path:
        """Return the validation directory path."""
        return self.root / "validations"

    @property
    def approvals(self) -> Path:
        """Return the approvals directory path."""
        return self.root / "approvals"

    @property
    def questions(self) -> Path:
        """Return the questions directory path."""
        return self.root / "questions"

    @property
    def resolved_questions(self) -> Path:
        """Return the resolved questions directory path."""
        return self.questions / "resolved"

    @property
    def handoffs(self) -> Path:
        """Return the handoffs directory path."""
        return self.root / "handoffs"

    @property
    def approval_file(self) -> Path:
        """Return the approval barrier file path."""
        return self.approvals / "approved.json"


def positive_int(value: str) -> int:
    """Parse and validate a positive integer.

    Args:
        value: Raw CLI argument value.
    """
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if parsed > 99:
        raise argparse.ArgumentTypeError("must be 99 or less")
    return parsed


def validate_worker_id(worker_id: str) -> str:
    """Validate a worker identifier.

    Args:
        worker_id: Candidate worker ID such as worker-01.
    """
    if not WORKER_ID_PATTERN.match(worker_id):
        raise argparse.ArgumentTypeError("worker id must match worker-NN, for example worker-01")
    return worker_id


def resolve_repo(raw_path: str) -> Path:
    """Resolve and validate a git repository path.

    Args:
        raw_path: CLI path to the target repository.
    """
    repo = Path(raw_path).expanduser().resolve()
    if not repo.exists() or not repo.is_dir():
        raise CliError(f"target repository does not exist: {repo}")
    if not (repo / ".git").exists():
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise CliError(f"target repository is not a git repository: {repo}")
        return Path(completed.stdout.strip()).resolve()
    return repo


def ensure_state_dirs(paths: StatePaths) -> None:
    """Create the orchestration state directory tree.

    Args:
        paths: Resolved orchestration paths.
    """
    for directory in (
        paths.tasks,
        paths.validations,
        paths.approvals,
        paths.questions,
        paths.resolved_questions,
        paths.handoffs,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str, *, force: bool = False) -> None:
    """Write UTF-8 text while protecting existing files by default.

    Args:
        path: Destination file path.
        content: Text content to write.
        force: Whether to overwrite an existing file.
    """
    if path.exists() and not force:
        raise CliError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def bullet_list(items: list[str]) -> str:
    """Render markdown bullet items.

    Args:
        items: Items to render.
    """
    if not items:
        return "- None recorded\n"
    return "".join(f"- {item}\n" for item in items)


def task_content(worker_id: str) -> str:
    """Create a worker task template.

    Args:
        worker_id: Worker identifier.
    """
    return f"""# Worker Task

## Worker

- ID: {worker_id}
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

.orchestrator/handoffs/{worker_id}.md
"""


def parse_task_file(task_file: Path) -> dict[str, Any]:
    """Parse basic metadata from a worker task file.

    Args:
        task_file: Task markdown file path.
    """
    metadata: dict[str, Any] = {
        "id": task_file.stem,
        "branch": "",
        "worktree": "",
        "scope": [],
    }
    in_scope = False
    for raw_line in task_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_scope = line == "## Owned Scope"
            continue
        if line.startswith("- ID:"):
            metadata["id"] = line.split(":", 1)[1].strip() or task_file.stem
        elif line.startswith("- Branch:"):
            metadata["branch"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Worktree:"):
            metadata["worktree"] = line.split(":", 1)[1].strip()
        elif in_scope and line.startswith("-"):
            scope = line.lstrip("- ").strip()
            if scope:
                metadata["scope"].append(scope)
    return metadata


def list_markdown_files(directory: Path) -> list[Path]:
    """List markdown files in a directory.

    Args:
        directory: Directory to scan.
    """
    if not directory.exists():
        return []
    return sorted(directory.glob("*.md"))


def next_question_path(paths: StatePaths, worker_id: str) -> Path:
    """Return the next numbered question file path for a worker.

    Args:
        paths: Resolved orchestration paths.
        worker_id: Worker identifier.
    """
    existing = sorted(paths.questions.glob(f"{worker_id}-*.md"))
    next_number = len(existing) + 1
    return paths.questions / f"{worker_id}-{next_number:03d}.md"


def resolve_question_name(question_name: str) -> str:
    """Normalize a question file name while rejecting path traversal.

    Args:
        question_name: Question file name or stem from the CLI.
    """
    name = Path(question_name).name
    if name != question_name:
        raise argparse.ArgumentTypeError("question name must not include directories")
    if not name.endswith(".md"):
        name = f"{name}.md"
    if not name.startswith("worker-") or not name.endswith(".md"):
        raise argparse.ArgumentTypeError("question name must look like worker-NN-001.md")
    return name


def command_init(args: argparse.Namespace) -> int:
    """Initialize orchestration state for a target repository.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    if paths.root.exists() and any(paths.root.iterdir()) and not args.force:
        raise CliError(f"state already exists, pass --force to overwrite templates: {paths.root}")

    ensure_state_dirs(paths)
    plan = f"""# Orchestration Plan

- Run: {args.run_name}
- Target repo: {repo}
- Worker count: {args.worker_count}
- Status: planning

## User Request


## Decomposition


## Integration Strategy


"""
    write_text(paths.root / "plan.md", plan, force=args.force)

    worktrees = [
        "# Worktrees\n\n",
        "## Integration\n\n",
        "- Branch:\n",
        "- Path:\n\n",
        "## Workers\n\n",
    ]
    for index in range(1, args.worker_count + 1):
        worker_id = f"worker-{index:02d}"
        write_text(paths.tasks / f"{worker_id}.md", task_content(worker_id), force=args.force)
        worktrees.append(f"- {worker_id}: see .orchestrator/tasks/{worker_id}.md\n")
    write_text(paths.root / "worktrees.md", "".join(worktrees), force=args.force)
    print(f"created orchestration state: {paths.root}")
    return 0


def build_status(paths: StatePaths) -> dict[str, Any]:
    """Build a machine-readable orchestration status object.

    Args:
        paths: Resolved orchestration paths.
    """
    task_files = list_markdown_files(paths.tasks)
    validation_files = list_markdown_files(paths.validations)
    question_files = list_markdown_files(paths.questions)
    handoff_files = list_markdown_files(paths.handoffs)
    validation_ids = {path.stem for path in validation_files}
    handoff_ids = {path.stem for path in handoff_files}
    worker_ids = [path.stem for path in task_files]
    missing_validations = [worker_id for worker_id in worker_ids if worker_id not in validation_ids]
    missing_handoffs = [worker_id for worker_id in worker_ids if worker_id not in handoff_ids]
    return {
        "repo": str(paths.repo),
        "state_dir": str(paths.root),
        "approved": paths.approval_file.exists(),
        "task_count": len(task_files),
        "validation_count": len(validation_files),
        "question_count": len(question_files),
        "handoff_count": len(handoff_files),
        "resolved_question_count": len(list_markdown_files(paths.resolved_questions)),
        "workers": worker_ids,
        "missing_validations": missing_validations,
        "missing_handoffs": missing_handoffs,
        "questions": [path.name for path in question_files],
    }


def command_status(args: argparse.Namespace) -> int:
    """Print orchestration status.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import print_runtime_status

    return print_runtime_status(resolve_repo(args.target_repo), as_json=args.json, run_id=args.run)


def command_watch(args: argparse.Namespace) -> int:
    """Watch orchestration status.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import watch_runtime

    return watch_runtime(
        resolve_repo(args.target_repo),
        interval=args.interval,
        once=args.once,
        max_ticks=args.count,
        run_id=args.run,
    )


def command_resume(args: argparse.Namespace) -> int:
    """Resume a previous ccx run.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import resume_runtime

    return resume_runtime(resolve_repo(args.target_repo), run_id=args.run)


def command_stop(args: argparse.Namespace) -> int:
    """Stop a ccx run.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import stop_runtime

    return stop_runtime(resolve_repo(args.target_repo), close_cmux=args.close_cmux, run_id=args.run)


def command_agent(args: argparse.Namespace) -> int:
    """Run a Claude or Codex agent through the ccx interrupt wrapper.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import run_agent_wrapper

    child_command = list(args.child_command)
    if child_command and child_command[0] == "--":
        child_command = child_command[1:]
    if args.role == "worker" and not args.worker_id:
        raise CliError("--worker-id is required for worker agents")
    if args.role == "conductor" and args.worker_id:
        raise CliError("--worker-id is only valid for worker agents")
    return run_agent_wrapper(
        repo=resolve_repo(args.repo),
        run_id=args.run,
        role=args.role,
        worker_id=args.worker_id,
        prompt_path=Path(args.prompt).expanduser().resolve(),
        child_command=child_command,
    )


def command_install_claude_commands(_: argparse.Namespace) -> int:
    """Install ccx Claude Code slash commands.

    Args:
        _: Parsed CLI arguments.
    """
    from claude_codex.claude_commands import install_claude_commands

    for path in install_claude_commands():
        print(f"installed: {path}")
    return 0


def command_doctor(_: argparse.Namespace) -> int:
    """Check whether required external CLIs are available.

    Args:
        _: Parsed CLI arguments.
    """
    checks = {
        "git": shutil.which("git"),
        "cmux": shutil.which("cmux"),
        "claude": shutil.which("claude"),
        "codex": shutil.which("codex"),
    }
    failed = False
    for name, path in checks.items():
        if path:
            print(f"[OK] {name}: {path}")
        else:
            print(f"[!!] {name}: not found in PATH")
            failed = True
    auth_check = check_claude_auth()
    if auth_check.logged_in:
        print(f"[OK] claude auth: {auth_check.auth_method} ({auth_check.api_provider})")
    else:
        print(f"[!!] claude auth: {auth_check.error}")
        print("     Run `claude`, execute `/login`, then retry `ccx`.")
        failed = True
    if failed:
        return 1
    print("ccx doctor: all required commands found and Claude auth is ready")
    return 0


def command_check_barrier(args: argparse.Namespace) -> int:
    """Check whether the approval barrier exists.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    if paths.approval_file.exists():
        print(f"approved: {paths.approval_file}")
        return 0
    print(f"not approved: missing {paths.approval_file}")
    return 1


def command_approve(args: argparse.Namespace) -> int:
    """Write the approval barrier after validations are complete.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    if not paths.root.exists():
        raise CliError(f"state directory does not exist: {paths.root}")

    task_files = list_markdown_files(paths.tasks)
    if not task_files:
        raise CliError("no worker tasks found")

    status = build_status(paths)
    if status["questions"] and not args.force:
        raise CliError("open questions exist; resolve them or pass --force")
    if status["missing_validations"] and not args.force:
        missing = ", ".join(status["missing_validations"])
        raise CliError(f"missing validations: {missing}")

    workers = [parse_task_file(task_file) for task_file in task_files]
    payload = {
        "approved": True,
        "approved_at": datetime.now(UTC).isoformat(),
        "conductor": args.conductor,
        "workers": workers,
        "constraints": [
            "Workers may edit only assigned scope.",
            "Same-file edits across workers require conductor arbitration.",
            "Workers must write handoffs before integration.",
        ],
    }
    approval_content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    write_text(paths.approval_file, approval_content, force=args.force)
    print(f"wrote approval barrier: {paths.approval_file}")
    return 0


def command_question(args: argparse.Namespace) -> int:
    """Create a worker question file.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    ensure_state_dirs(paths)
    question_file = next_question_path(paths, args.worker_id)
    content = f"""# Worker Question

## Worker

- ID: {args.worker_id}

## Blocking Question

{args.title}

{args.body}

## Why This Blocks Work

{args.blocks}

## Options

{bullet_list(args.option)}

## Recommended Resolution

{args.recommendation}
"""
    write_text(question_file, content)
    print(f"wrote question: {question_file}")
    return 0


def command_resolve_question(args: argparse.Namespace) -> int:
    """Move an open question into the resolved question archive.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    ensure_state_dirs(paths)
    source = paths.questions / args.question_name
    if not source.exists():
        raise CliError(f"question does not exist: {source}")
    resolved_content = (
        source.read_text(encoding="utf-8") + "\n## Conductor Resolution\n\n" + args.answer + "\n"
    )
    destination = paths.resolved_questions / args.question_name
    write_text(destination, resolved_content, force=args.force)
    source.unlink()
    print(f"resolved question: {destination}")
    return 0


def command_validation(args: argparse.Namespace) -> int:
    """Create or replace a worker validation file.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    ensure_state_dirs(paths)
    validation_file = paths.validations / f"{args.worker_id}.md"
    content = f"""# Worker Validation

## Worker

- ID: {args.worker_id}
- Task file: .orchestrator/tasks/{args.worker_id}.md

## Scope Coherence

{args.scope_coherence}

## Overlap Check

{args.overlap_check}

## Missing Context

{args.missing_context}

## Risks

{bullet_list(args.risk)}

## Questions

{bullet_list(args.question)}

## Recommendation

{args.recommendation.title()}
"""
    write_text(validation_file, content, force=args.force)
    print(f"wrote validation: {validation_file}")
    return 0


def command_handoff(args: argparse.Namespace) -> int:
    """Create or replace a worker handoff file.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = StatePaths(repo)
    ensure_state_dirs(paths)
    handoff_file = paths.handoffs / f"{args.worker_id}.md"
    content = f"""# Worker Handoff

## Worker

- ID: {args.worker_id}
- Branch: {args.branch}
- Worktree: {args.worktree}

## Summary

{args.summary}

## Files Changed

{bullet_list(args.file)}

## Behavioral Changes

{args.behavior}

## Tests Run

{bullet_list(args.test)}

## Risks

{bullet_list(args.risk)}

## Integration Notes

{args.integration_notes}
"""
    write_text(handoff_file, content, force=args.force)
    print(f"wrote handoff: {handoff_file}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    """Launch the interactive Claude + Codex orchestration flow.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import RunConfig, run_orchestration

    request = " ".join(args.request).strip()
    if not request:
        try:
            request = input("ccx> ").strip()
        except EOFError as exc:
            raise CliError("request is required when stdin is not interactive") from exc
    if not request:
        raise CliError("request is required")
    config = RunConfig(
        repo=Path(args.repo).expanduser().resolve(),
        request=request,
        claude_model=args.claude_model,
        claude_effort=args.claude_effort,
        codex_model=args.codex_model,
        codex_effort=args.codex_effort,
        requested_workers=args.workers,
        dry_run=args.dry_run,
        skip_launch=args.skip_launch,
        force_state=args.force_state,
        skip_conductor=args.no_conductor,
    )
    return run_orchestration(config)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(prog="claude-codex")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="launch interactive cmux orchestration")
    run_parser.add_argument("request", nargs="*")
    run_parser.add_argument("--repo", default=".")
    run_parser.add_argument("--claude-model", default="opus")
    run_parser.add_argument("--claude-effort", default="medium")
    run_parser.add_argument("--codex-model", default="gpt-5.3-codex")
    run_parser.add_argument("--codex-effort", default="medium")
    run_parser.add_argument("--workers", type=positive_int)
    run_parser.add_argument("--dry-run", action="store_true", help="print Claude plan only")
    run_parser.add_argument("--skip-launch", action="store_true", help="skip cmux launch")
    run_parser.add_argument(
        "--no-conductor",
        action="store_true",
        help="launch worker panes only; use the current Claude session as conductor",
    )
    run_parser.add_argument(
        "--force-state",
        action="store_true",
        help="overwrite existing .orchestrator files",
    )
    run_parser.set_defaults(func=command_run)

    install_commands_parser = subparsers.add_parser(
        "install-claude-commands",
        help="install ccx commands into Claude Code slash commands",
    )
    install_commands_parser.set_defaults(func=command_install_claude_commands)

    doctor_parser = subparsers.add_parser("doctor", help="check required external CLIs")
    doctor_parser.set_defaults(func=command_doctor)

    init_parser = subparsers.add_parser("init", help="initialize orchestration state")
    init_parser.add_argument("target_repo")
    init_parser.add_argument("run_name")
    init_parser.add_argument("worker_count", type=positive_int)
    init_parser.add_argument("--force", action="store_true", help="overwrite generated templates")
    init_parser.set_defaults(func=command_init)

    status_parser = subparsers.add_parser("status", help="show orchestration status")
    status_parser.add_argument("target_repo", nargs="?", default=".")
    status_parser.add_argument("--run", help="select a specific run id")
    status_parser.add_argument("--json", action="store_true", help="print JSON status")
    status_parser.set_defaults(func=command_status)

    watch_parser = subparsers.add_parser("watch", help="watch orchestration status")
    watch_parser.add_argument("target_repo", nargs="?", default=".")
    watch_parser.add_argument("--run", help="select a specific run id")
    watch_parser.add_argument("--interval", type=float, default=2.0)
    watch_parser.add_argument("--once", action="store_true")
    watch_parser.add_argument("--count", type=positive_int, default=0)
    watch_parser.set_defaults(func=command_watch)

    resume_parser = subparsers.add_parser("resume", help="resume a previous ccx run")
    resume_parser.add_argument("target_repo", nargs="?", default=".")
    resume_parser.add_argument("--run", help="select a specific run id")
    resume_parser.set_defaults(func=command_resume)

    stop_parser = subparsers.add_parser("stop", help="stop a ccx run")
    stop_parser.add_argument("target_repo", nargs="?", default=".")
    stop_parser.add_argument("--run", help="select a specific run id")
    stop_parser.add_argument(
        "--close-cmux",
        action="store_true",
        help="also close the recorded cmux workspace",
    )
    stop_parser.set_defaults(func=command_stop)

    agent_parser = subparsers.add_parser(
        "agent",
        help="run a launched agent with ccx signal handling",
    )
    agent_parser.add_argument("--repo", required=True)
    agent_parser.add_argument("--run", required=True, help="run id to stop on interrupt")
    agent_parser.add_argument("--role", choices=["conductor", "worker"], required=True)
    agent_parser.add_argument("--worker-id", type=validate_worker_id)
    agent_parser.add_argument("--prompt", required=True, help="prompt file to append to child")
    agent_parser.add_argument("child_command", nargs=argparse.REMAINDER)
    agent_parser.set_defaults(func=command_agent)

    barrier_parser = subparsers.add_parser("check-barrier", help="check approval barrier")
    barrier_parser.add_argument("target_repo")
    barrier_parser.set_defaults(func=command_check_barrier)

    approve_parser = subparsers.add_parser("approve", help="write approval barrier")
    approve_parser.add_argument("target_repo")
    approve_parser.add_argument("--conductor", default="claude")
    approve_parser.add_argument(
        "--force",
        action="store_true",
        help="ignore questions/missing validations",
    )
    approve_parser.set_defaults(func=command_approve)

    question_parser = subparsers.add_parser("question", help="write a worker question")
    question_parser.add_argument("target_repo")
    question_parser.add_argument("worker_id", type=validate_worker_id)
    question_parser.add_argument("--title", required=True)
    question_parser.add_argument("--body", required=True)
    question_parser.add_argument(
        "--blocks",
        default="The worker needs conductor arbitration before proceeding.",
    )
    question_parser.add_argument("--option", action="append", default=[])
    question_parser.add_argument("--recommendation", default="Wait for conductor decision.")
    question_parser.set_defaults(func=command_question)

    resolve_parser = subparsers.add_parser(
        "resolve-question",
        help="mark a worker question as resolved",
    )
    resolve_parser.add_argument("target_repo")
    resolve_parser.add_argument("question_name", type=resolve_question_name)
    resolve_parser.add_argument("--answer", required=True)
    resolve_parser.add_argument("--force", action="store_true")
    resolve_parser.set_defaults(func=command_resolve_question)

    validation_parser = subparsers.add_parser("validation", help="write a worker validation")
    validation_parser.add_argument("target_repo")
    validation_parser.add_argument("worker_id", type=validate_worker_id)
    validation_parser.add_argument("--scope-coherence", required=True)
    validation_parser.add_argument("--overlap-check", required=True)
    validation_parser.add_argument("--missing-context", default="None identified.")
    validation_parser.add_argument("--risk", action="append", default=[])
    validation_parser.add_argument("--question", action="append", default=[])
    validation_parser.add_argument(
        "--recommendation",
        choices=sorted(RECOMMENDATIONS),
        required=True,
    )
    validation_parser.add_argument("--force", action="store_true")
    validation_parser.set_defaults(func=command_validation)

    handoff_parser = subparsers.add_parser("handoff", help="write a worker handoff")
    handoff_parser.add_argument("target_repo")
    handoff_parser.add_argument("worker_id", type=validate_worker_id)
    handoff_parser.add_argument("--branch", required=True)
    handoff_parser.add_argument("--worktree", required=True)
    handoff_parser.add_argument("--summary", required=True)
    handoff_parser.add_argument("--file", action="append", default=[])
    handoff_parser.add_argument("--behavior", default="No behavioral changes recorded.")
    handoff_parser.add_argument("--test", action="append", default=[])
    handoff_parser.add_argument("--risk", action="append", default=[])
    handoff_parser.add_argument("--integration-notes", default="No special integration notes.")
    handoff_parser.add_argument("--force", action="store_true")
    handoff_parser.set_defaults(func=command_handoff)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the claude-codex CLI.

    Args:
        argv: Optional argument vector, excluding executable name.
    """
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        from claude_codex.runner import interactive_default

        try:
            return interactive_default(Path.cwd())
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

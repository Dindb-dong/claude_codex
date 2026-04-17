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

CCX_DIR_NAME = ".ccx"
LOCAL_CCX_DIR_NAME = ".ccx-local"
RUNS_DIR_NAME = "runs"
CURRENT_RUN_FILE = "current-run"
RECOMMENDATIONS = {"approve", "revise", "reject"}
WORKER_ID_PATTERN = re.compile(r"^worker-[0-9]{2}$")


class CliError(Exception):
    """Expected user-facing command failure."""


@dataclass(frozen=True)
class StatePaths:
    """Resolved paths for a ccx state directory.

    Args:
        repo: Target git repository path.
        state_root: Explicit state directory root.
    """

    repo: Path
    state_root: Path

    @property
    def root(self) -> Path:
        """Return the state root path."""
        return self.state_root

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


def run_state_root(repo: Path, run_id: str) -> Path:
    """Return the run-scoped state directory.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
    """
    return repo / CCX_DIR_NAME / RUNS_DIR_NAME / run_id


def runs_root(repo: Path) -> Path:
    """Return the ccx runs directory.

    Args:
        repo: Target repository path.
    """
    return repo / CCX_DIR_NAME / RUNS_DIR_NAME


def read_current_run(repo: Path) -> str:
    """Read the current ccx run pointer.

    Args:
        repo: Target repository path.
    """
    path = repo / CCX_DIR_NAME / CURRENT_RUN_FILE
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def resolve_command_state_paths(repo: Path, run_id: str | None = None) -> StatePaths:
    """Resolve state paths for commands that operate on existing run state.

    Args:
        repo: Target repository path.
        run_id: Optional explicit run identifier.
    """
    selected_run = run_id or read_current_run(repo)
    if selected_run:
        return StatePaths(repo, run_state_root(repo, selected_run))
    return StatePaths(repo, runs_root(repo))


def existing_command_state_paths(repo: Path, run_id: str | None = None) -> StatePaths:
    """Resolve and require an existing command state directory.

    Args:
        repo: Target repository path.
        run_id: Optional explicit run identifier.
    """
    if run_id is None and not read_current_run(repo):
        raise CliError("no current ccx run selected; pass --run <run-id> or start a new ccx run")
    paths = resolve_command_state_paths(repo, run_id)
    if not paths.root.exists():
        raise CliError(f"state directory does not exist: {paths.root}")
    return paths


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


def local_question_path(worktree: Path, run_id: str, question_name: str) -> Path:
    """Return the worker-local question fallback path.

    Args:
        worktree: Worker worktree path.
        run_id: Shared ccx run id.
        question_name: Question file name.
    """
    return (
        worktree
        / LOCAL_CCX_DIR_NAME
        / RUNS_DIR_NAME
        / local_run_id(run_id)
        / "questions"
        / question_name
    )


def local_question_files(state: dict[str, Any]) -> list[Path]:
    """Return worker-local question fallback files recorded under worker worktrees.

    Args:
        state: Runtime state payload.
    """
    run_id = str(state.get("run_id") or "")
    files: list[Path] = []
    for worker in state.get("workers", []):
        if not isinstance(worker, dict):
            continue
        worktree = Path(str(worker.get("worktree") or ""))
        if not worktree:
            continue
        question_dir = (
            worktree / LOCAL_CCX_DIR_NAME / RUNS_DIR_NAME / local_run_id(run_id) / "questions"
        )
        if question_dir.exists():
            files.extend(sorted(question_dir.glob("*.md")))
    return files


def find_local_question_file(state: dict[str, Any], question_name: str) -> Path | None:
    """Return a worker-local question fallback by file name, if present.

    Args:
        state: Runtime state payload.
        question_name: Question file name.
    """
    for question_file in local_question_files(state):
        if question_file.name == question_name:
            return question_file
    return None


def next_question_name(paths: StatePaths, state: dict[str, Any], worker_id: str) -> str:
    """Return the next numbered question file name for a worker.

    Args:
        paths: Resolved orchestration paths.
        state: Runtime state payload.
        worker_id: Worker identifier.
    """
    shared = list(paths.questions.glob(f"{worker_id}-*.md"))
    local = [path for path in local_question_files(state) if path.name.startswith(f"{worker_id}-")]
    existing = sorted({path.name for path in [*shared, *local]})
    next_number = len(existing) + 1
    return f"{worker_id}-{next_number:03d}.md"


def next_question_path(paths: StatePaths, state: dict[str, Any], worker_id: str) -> Path:
    """Return the next numbered shared question file path for a worker.

    Args:
        paths: Resolved orchestration paths.
        state: Runtime state payload.
        worker_id: Worker identifier.
    """
    return paths.questions / next_question_name(paths, state, worker_id)


def local_run_id(run_id: str) -> str:
    """Return a stable local fallback run id.

    Args:
        run_id: Shared ccx run id, when available.
    """
    return run_id or "legacy"


def local_handoff_path(worktree: Path, run_id: str, worker_id: str) -> Path:
    """Return the worker-local handoff fallback path.

    Args:
        worktree: Worker worktree path.
        run_id: Shared ccx run id.
        worker_id: Worker identifier.
    """
    return (
        worktree
        / LOCAL_CCX_DIR_NAME
        / RUNS_DIR_NAME
        / local_run_id(run_id)
        / "handoffs"
        / f"{worker_id}.md"
    )


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


def markdown_metadata_value(content: str, label: str) -> str:
    """Return a markdown metadata value such as ``- Branch: ...``.

    Args:
        content: Markdown content to inspect.
        label: Metadata label without the leading dash.
    """
    prefix = f"- {label}:"
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def handoff_metadata(handoff_file: Path) -> dict[str, str]:
    """Parse the worker, branch, and worktree fields from a handoff.

    Args:
        handoff_file: Handoff markdown file.
    """
    content = handoff_file.read_text(encoding="utf-8")
    return {
        "worker_id": markdown_metadata_value(content, "ID") or handoff_file.stem,
        "branch": markdown_metadata_value(content, "Branch"),
        "worktree": markdown_metadata_value(content, "Worktree"),
    }


def worker_state_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return runtime worker metadata keyed by worker id.

    Args:
        state: Runtime state payload.
    """
    workers: dict[str, dict[str, Any]] = {}
    for worker in state.get("workers", []):
        if not isinstance(worker, dict):
            continue
        worker_id = str(worker.get("id") or "")
        if worker_id:
            workers[worker_id] = worker
    return workers


def handoff_file_for_worker(
    paths: StatePaths, state: dict[str, Any], worker_id: str
) -> Path | None:
    """Return the shared or local handoff file for a worker.

    Args:
        paths: Resolved orchestration paths.
        state: Runtime state payload.
        worker_id: Worker identifier.
    """
    shared = paths.handoffs / f"{worker_id}.md"
    if shared.exists():
        return shared
    workers = worker_state_by_id(state)
    worker = workers.get(worker_id, {})
    worktree = Path(str(worker.get("worktree") or ""))
    run_id = str(state.get("run_id") or "")
    if worktree and run_id:
        local = local_handoff_path(worktree, run_id, worker_id)
        if local.exists():
            return local
    return None


def git_output(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command and capture text output.

    Args:
        command: Git arguments including the executable.
        cwd: Working directory.
    """
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def require_clean_git_worktree(worktree: Path) -> None:
    """Require the integration worktree to have no pending changes.

    Args:
        worktree: Integration git worktree path.
    """
    status = git_output(["git", "status", "--porcelain"], worktree)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "").strip()
        raise CliError(f"failed to inspect integration worktree: {detail}")
    if status.stdout.strip():
        raise CliError(f"integration worktree has uncommitted changes: {worktree}")


def write_integration_report(paths: StatePaths, report: dict[str, Any]) -> Path:
    """Write an integration report under the run state directory.

    Args:
        paths: Resolved orchestration paths.
        report: Report payload.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    report_path = paths.root / "integration" / f"{timestamp}.json"
    write_text(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n", force=True)
    return report_path


def command_integrate(args: argparse.Namespace) -> int:
    """Merge worker branches into the integration worktree.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = existing_command_state_paths(repo, args.run)
    state = read_runtime_state(paths)
    run_id = command_run_id(repo, args.run, paths)
    status = build_status(paths, state)
    if state.get("status") == "stopped" and not args.force:
        raise CliError("run is stopped; resume it first or pass --force")
    if status["questions"] and not args.force:
        raise CliError("open questions exist; resolve them or pass --force")
    if not paths.approval_file.exists() and not args.force:
        raise CliError("approval barrier is missing; approve the run before integration")

    worker_ids = args.worker or status["workers"]
    if not worker_ids:
        raise CliError("no worker tasks found")

    missing_handoffs = [
        worker_id
        for worker_id in worker_ids
        if handoff_file_for_worker(paths, state, worker_id) is None
    ]
    if missing_handoffs and not args.force:
        raise CliError(f"missing handoffs: {', '.join(missing_handoffs)}")

    integration = state.get("integration", {})
    if not isinstance(integration, dict):
        integration = {}
    integration_worktree = Path(str(integration.get("worktree") or ""))
    if not integration_worktree:
        raise CliError("run-state.json does not record an integration worktree")
    if not integration_worktree.exists():
        raise CliError(f"integration worktree does not exist: {integration_worktree}")
    if not args.dry_run and not args.allow_dirty:
        require_clean_git_worktree(integration_worktree)

    worker_state = worker_state_by_id(state)
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "integration_worktree": str(integration_worktree),
        "status": "dry-run" if args.dry_run else "running",
        "workers": [],
    }

    for worker_id in worker_ids:
        handoff_file = handoff_file_for_worker(paths, state, worker_id)
        metadata = handoff_metadata(handoff_file) if handoff_file is not None else {}
        branch = str(metadata.get("branch") or worker_state.get(worker_id, {}).get("branch") or "")
        if not branch:
            raise CliError(f"missing branch for {worker_id}")
        worker_report = {
            "id": worker_id,
            "branch": branch,
            "handoff": str(handoff_file) if handoff_file is not None else "",
            "result": "planned" if args.dry_run else "pending",
        }
        report["workers"].append(worker_report)
        if args.dry_run:
            print(f"would merge {worker_id}: {branch}")
            continue
        print(f"merging {worker_id}: {branch}")
        merge = git_output(["git", "merge", "--no-ff", "--no-edit", branch], integration_worktree)
        worker_report["stdout"] = merge.stdout.strip()
        worker_report["stderr"] = merge.stderr.strip()
        worker_report["returncode"] = merge.returncode
        if merge.returncode != 0:
            worker_report["result"] = "failed"
            report["status"] = "failed"
            report["failed_worker"] = worker_id
            report["finished_at"] = datetime.now(UTC).isoformat()
            report_path = write_integration_report(paths, report)
            integration["status"] = "failed"
            integration["last_report"] = str(report_path)
            integration["failed_worker"] = worker_id
            state["integration"] = integration
            write_runtime_state(paths, state)
            print(f"integration failed for {worker_id}; report: {report_path}", file=sys.stderr)
            return 1
        worker_report["result"] = "merged"

    report["status"] = "integrated" if not args.dry_run else "dry-run"
    report["finished_at"] = datetime.now(UTC).isoformat()
    report_path = write_integration_report(paths, report)
    if not args.dry_run:
        integration["status"] = "integrated"
        integration["last_integrated_at"] = report["finished_at"]
        integration["last_report"] = str(report_path)
        integration["merged_workers"] = worker_ids
        state["integration"] = integration
        write_runtime_state(paths, state)
    print(f"integration report: {report_path}")
    return 0


def build_status(paths: StatePaths, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a machine-readable orchestration status object.

    Args:
        paths: Resolved orchestration paths.
        state: Optional runtime state payload.
    """
    state = state if state is not None else read_runtime_state(paths)
    task_files = list_markdown_files(paths.tasks)
    validation_files = list_markdown_files(paths.validations)
    question_files = list_markdown_files(paths.questions)
    resolved_question_files = list_markdown_files(paths.resolved_questions)
    shared_question_names = {path.name for path in question_files}
    resolved_question_names = {path.name for path in resolved_question_files}
    local_questions = [
        path
        for path in local_question_files(state)
        if path.name not in shared_question_names and path.name not in resolved_question_names
    ]
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
        "question_count": len(question_files) + len(local_questions),
        "shared_question_count": len(question_files),
        "local_question_count": len(local_questions),
        "handoff_count": len(handoff_files),
        "resolved_question_count": len(resolved_question_files),
        "workers": worker_ids,
        "missing_validations": missing_validations,
        "missing_handoffs": missing_handoffs,
        "questions": [path.name for path in [*question_files, *local_questions]],
        "local_questions": [str(path) for path in local_questions],
    }


def runtime_state_path(paths: StatePaths) -> Path:
    """Return the run-state path for a ccx run.

    Args:
        paths: Resolved orchestration paths.
    """
    return paths.root / "run-state.json"


def read_runtime_state(paths: StatePaths) -> dict[str, Any]:
    """Read run-state.json when present.

    Args:
        paths: Resolved orchestration paths.
    """
    path = runtime_state_path(paths)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_runtime_state(paths: StatePaths, state: dict[str, Any]) -> None:
    """Write run-state.json.

    Args:
        paths: Resolved orchestration paths.
        state: Runtime state payload.
    """
    write_text(
        runtime_state_path(paths), json.dumps(state, indent=2, sort_keys=True) + "\n", force=True
    )


def command_run_id(repo: Path, explicit_run: str | None, paths: StatePaths) -> str:
    """Resolve the run id used by a state command.

    Args:
        repo: Target repository path.
        explicit_run: Optional CLI run id.
        paths: Resolved orchestration paths.
    """
    if explicit_run:
        return explicit_run
    current_run = read_current_run(repo)
    if current_run:
        return current_run
    if paths.root.parent.name == RUNS_DIR_NAME:
        return paths.root.name
    return ""


def completed_process_detail(exc: subprocess.CalledProcessError) -> str:
    """Return compact stderr/stdout details for a failed subprocess.

    Args:
        exc: Failed subprocess error.
    """
    detail = str(exc)
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    if stderr:
        detail += f"; stderr: {stderr[:500]}"
    if stdout:
        detail += f"; stdout: {stdout[:500]}"
    return detail


def validation_recommendation(validation_file: Path) -> str:
    """Read the recommendation value from a worker validation file.

    Args:
        validation_file: Validation markdown file.
    """
    in_recommendation = False
    for raw_line in validation_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "## Recommendation":
            in_recommendation = True
            continue
        if in_recommendation and line:
            return line.lower()
    return ""


def all_validations_approve(paths: StatePaths) -> bool:
    """Return whether every expected worker validation recommends approval.

    Args:
        paths: Resolved orchestration paths.
    """
    state = read_runtime_state(paths)
    status = build_status(paths, state)
    if status["missing_validations"] or status["questions"]:
        return False
    validation_files = list_markdown_files(paths.validations)
    if len(validation_files) != status["task_count"]:
        return False
    return all(validation_recommendation(path) == "approve" for path in validation_files)


def approval_resume_prompt(repo: Path, run_id: str, worker_id: str) -> str:
    """Build the prompt sent to workers after approval.

    Args:
        repo: Target repository path.
        run_id: Run identifier.
        worker_id: Worker identifier.
    """
    return (
        f"Approval ready for {run_id}. {worker_id}: continue your assigned implementation "
        "in your worker worktree. Do not run ccx stop. If blocked, write a ccx question "
        "and pause. When done, write ccx handoff for this run."
    )


def notify_workers_of_approval(repo: Path, paths: StatePaths, run_id: str) -> int:
    """Send an approval resume prompt to recorded cmux worker surfaces.

    Args:
        repo: Target repository path.
        paths: Resolved orchestration paths.
        run_id: Run identifier.
    """
    state = read_runtime_state(paths)
    workspace = str(state.get("cmux_workspace") or "")
    workers = [worker for worker in state.get("workers", []) if isinstance(worker, dict)]
    if not workspace or not workers:
        return 0

    notified = 0
    for worker in workers:
        worker_id = str(worker.get("id") or "")
        surface = str(worker.get("surface") or "")
        if not worker_id or not surface:
            continue
        message = approval_resume_prompt(repo, run_id, worker_id)
        try:
            subprocess.run(
                ["cmux", "send", "--workspace", workspace, "--surface", surface, message],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["cmux", "send-key", "--workspace", workspace, "--surface", surface, "enter"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            print(f"warning: failed to notify {worker_id} after approval: {exc}", file=sys.stderr)
            continue
        except subprocess.CalledProcessError as exc:
            detail = completed_process_detail(exc)
            print(
                f"warning: failed to notify {worker_id} after approval: {detail}", file=sys.stderr
            )
            continue
        notified += 1

    if notified:
        state["approval_notified_at"] = datetime.now(UTC).isoformat()
        state["approval_notified_workers"] = notified
        write_runtime_state(paths, state)
        print(f"notified workers after approval: {notified}")
    return notified


def notify_conductor_validations_ready(repo: Path, paths: StatePaths, run_id: str) -> bool:
    """Notify the recorded conductor surface that validations are ready.

    Args:
        repo: Target repository path.
        paths: Resolved orchestration paths.
        run_id: Run identifier.
    """
    if paths.approval_file.exists() or not all_validations_approve(paths):
        return False
    state = read_runtime_state(paths)
    conductor = state.get("conductor", {})
    if not isinstance(conductor, dict):
        return False
    workspace = str(conductor.get("workspace") or state.get("cmux_workspace") or "")
    surface = str(conductor.get("surface") or "")
    if not workspace or not surface:
        return False
    message = (
        f"All ccx validations are complete for {run_id} with approve recommendations. "
        f"Review validations, then run: ccx approve {repo} --run {run_id}"
    )
    try:
        subprocess.run(
            ["cmux", "send", "--workspace", workspace, "--surface", surface, message],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["cmux", "send-key", "--workspace", workspace, "--surface", surface, "enter"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        print(f"warning: failed to notify conductor after validations: {exc}", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        detail = completed_process_detail(exc)
        print(f"warning: failed to notify conductor after validations: {detail}", file=sys.stderr)
        return False
    print("notified conductor that validations are ready")
    return True


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
    paths = existing_command_state_paths(repo, args.run)
    state = read_runtime_state(paths)
    status = str(state.get("status") or "")
    if status == "stopped":
        print(f"not approved: run is stopped in {runtime_state_path(paths)}")
        return 1
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
    paths = existing_command_state_paths(repo, args.run)
    state = read_runtime_state(paths)
    if state.get("status") == "stopped" and not args.force:
        raise CliError("run is stopped; resume it first or pass --force")

    task_files = list_markdown_files(paths.tasks)
    if not task_files:
        raise CliError("no worker tasks found")

    status = build_status(paths, state)
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
    if not args.no_notify_workers:
        notify_workers_of_approval(repo, paths, command_run_id(repo, args.run, paths))
    return 0


def command_question(args: argparse.Namespace) -> int:
    """Create a worker question file.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = existing_command_state_paths(repo, args.run)
    ensure_state_dirs(paths)
    state = read_runtime_state(paths)
    question_file = next_question_path(paths, state, args.worker_id)
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
    run_id = command_run_id(repo, args.run, paths)
    try:
        write_text(question_file, content)
        print(f"wrote question: {question_file}")
    except PermissionError as exc:
        fallback_file = local_question_path(Path.cwd(), run_id, question_file.name)
        write_text(fallback_file, content, force=True)
        print(f"warning: shared question write failed: {exc}", file=sys.stderr)
        print(f"wrote local question fallback: {fallback_file}")
    return 0


def command_resolve_question(args: argparse.Namespace) -> int:
    """Move an open question into the resolved question archive.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = existing_command_state_paths(repo, args.run)
    ensure_state_dirs(paths)
    state = read_runtime_state(paths)
    source = paths.questions / args.question_name
    if not source.exists():
        local_source = find_local_question_file(state, args.question_name)
        if local_source is None:
            raise CliError(f"question does not exist: {source}")
        source = local_source
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
    paths = existing_command_state_paths(repo, args.run)
    ensure_state_dirs(paths)
    validation_file = paths.validations / f"{args.worker_id}.md"
    content = f"""# Worker Validation

## Worker

- ID: {args.worker_id}
- Task file: {paths.tasks / f"{args.worker_id}.md"}

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
    notify_conductor_validations_ready(repo, paths, command_run_id(repo, args.run, paths))
    return 0


def command_handoff(args: argparse.Namespace) -> int:
    """Create or replace a worker handoff file.

    Args:
        args: Parsed CLI arguments.
    """
    repo = resolve_repo(args.target_repo)
    paths = existing_command_state_paths(repo, args.run)
    ensure_state_dirs(paths)
    run_id = command_run_id(repo, args.run, paths)
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
    try:
        write_text(handoff_file, content, force=args.force)
        print(f"wrote handoff: {handoff_file}")
    except PermissionError as exc:
        fallback_file = local_handoff_path(Path.cwd(), run_id, args.worker_id)
        write_text(fallback_file, content, force=True)
        print(f"warning: shared handoff write failed: {exc}", file=sys.stderr)
        print(f"wrote local handoff fallback: {fallback_file}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    """Launch the interactive Claude + Codex orchestration flow.

    Args:
        args: Parsed CLI arguments.
    """
    from claude_codex.runner import MAX_AUTO_WORKERS, RunConfig, run_orchestration

    request = " ".join(args.request).strip()
    if not request:
        try:
            request = input("ccx> ").strip()
        except EOFError as exc:
            raise CliError("request is required when stdin is not interactive") from exc
    if not request:
        raise CliError("request is required")
    if args.workers and args.workers > MAX_AUTO_WORKERS:
        raise CliError(f"worker count must be {MAX_AUTO_WORKERS} or less")
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
        help="overwrite existing .ccx run state files",
    )
    run_parser.set_defaults(func=command_run)

    install_commands_parser = subparsers.add_parser(
        "install-claude-commands",
        help="install ccx commands into Claude Code slash commands",
    )
    install_commands_parser.set_defaults(func=command_install_claude_commands)

    doctor_parser = subparsers.add_parser("doctor", help="check required external CLIs")
    doctor_parser.set_defaults(func=command_doctor)

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
    barrier_parser.add_argument("--run", help="select a specific run id")
    barrier_parser.set_defaults(func=command_check_barrier)

    approve_parser = subparsers.add_parser("approve", help="write approval barrier")
    approve_parser.add_argument("target_repo")
    approve_parser.add_argument("--run", help="select a specific run id")
    approve_parser.add_argument("--conductor", default="claude")
    approve_parser.add_argument(
        "--no-notify-workers",
        action="store_true",
        help="only write approved.json; do not send cmux resume prompts to workers",
    )
    approve_parser.add_argument(
        "--force",
        action="store_true",
        help="ignore questions/missing validations",
    )
    approve_parser.set_defaults(func=command_approve)

    integrate_parser = subparsers.add_parser(
        "integrate",
        help="merge worker branches into the integration worktree",
    )
    integrate_parser.add_argument("target_repo", nargs="?", default=".")
    integrate_parser.add_argument("--run", help="select a specific run id")
    integrate_parser.add_argument(
        "--worker",
        action="append",
        type=validate_worker_id,
        help="integrate a specific worker; may be passed multiple times",
    )
    integrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned merges and write a report without running git merge",
    )
    integrate_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow merging into a dirty integration worktree",
    )
    integrate_parser.add_argument(
        "--force",
        action="store_true",
        help="ignore missing approval, open questions, stopped state, or missing handoffs",
    )
    integrate_parser.set_defaults(func=command_integrate)

    question_parser = subparsers.add_parser("question", help="write a worker question")
    question_parser.add_argument("target_repo")
    question_parser.add_argument("worker_id", type=validate_worker_id)
    question_parser.add_argument("--run", help="select a specific run id")
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
    resolve_parser.add_argument("--run", help="select a specific run id")
    resolve_parser.add_argument("--answer", required=True)
    resolve_parser.add_argument("--force", action="store_true")
    resolve_parser.set_defaults(func=command_resolve_question)

    validation_parser = subparsers.add_parser("validation", help="write a worker validation")
    validation_parser.add_argument("target_repo")
    validation_parser.add_argument("worker_id", type=validate_worker_id)
    validation_parser.add_argument("--run", help="select a specific run id")
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
    handoff_parser.add_argument("--run", help="select a specific run id")
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

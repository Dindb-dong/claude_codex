"""Regression tests for the claude-codex CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from claude_codex.cli import main
from claude_codex.preflight import ClaudeAuthCheck
from claude_codex.runner import installed_worker_hard_rules_path


def authenticated_claude_check() -> ClaudeAuthCheck:
    """Return a passing Claude auth check for orchestration tests."""
    return ClaudeAuthCheck(
        claude_path="/usr/local/bin/claude",
        logged_in=True,
        auth_method="oauth",
        api_provider="firstParty",
    )


class CliTestCase(unittest.TestCase):
    """Test the CLI against temporary git repositories."""

    def setUp(self) -> None:
        """Create an isolated git repository for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, text=True)
        (self.repo / "README.md").write_text("# test repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: init test repo"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        """Clean up the temporary git repository."""
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> int:
        """Run the CLI with a target argument list.

        Args:
            *args: CLI arguments excluding the executable name.
        """
        return main(list(args))

    def create_run_state(self, run_id: str = "20260416000000000000-demo") -> Path:
        """Create a minimal current-run state fixture.

        Args:
            run_id: Run identifier to write under .ccx/runs.
        """
        run_root = self.repo / ".ccx/runs" / run_id
        (self.repo / ".ccx").mkdir(exist_ok=True)
        (self.repo / ".ccx/current-run").write_text(run_id + "\n", encoding="utf-8")
        (run_root / "tasks").mkdir(parents=True, exist_ok=True)
        (run_root / "validations").mkdir(exist_ok=True)
        (run_root / "tasks/worker-01.md").write_text(
            """# Worker Task

## Worker

- ID: worker-01
- Branch: ccx/demo/worker-01
- Worktree: /tmp/worker-01

## Owned Scope

- src/demo.py
""",
            encoding="utf-8",
        )
        return run_root

    def test_runtime_command_without_current_run_explains_selection(self) -> None:
        """state commands explain missing run selection instead of implying runtime legacy."""
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = self.run_cli("approve", str(self.repo))

        self.assertEqual(exit_code, 1)
        self.assertIn("no current ccx run selected", stderr.getvalue())
        self.assertIn("pass --run <run-id>", stderr.getvalue())

    def test_approve_requires_validations_without_force(self) -> None:
        """approve refuses to create a barrier before worker validations exist."""
        run_root = self.create_run_state()

        exit_code = self.run_cli("approve", str(self.repo))

        self.assertEqual(exit_code, 1)
        self.assertFalse((run_root / "approvals/approved.json").exists())

    def test_approve_writes_barrier_after_validation(self) -> None:
        """approve writes approved.json after all validations are present."""
        run_root = self.create_run_state()
        self.run_cli(
            "validation",
            str(self.repo),
            "worker-01",
            "--scope-coherence",
            "Scope is coherent.",
            "--overlap-check",
            "No overlap found.",
            "--recommendation",
            "approve",
        )

        exit_code = self.run_cli("approve", str(self.repo))

        approval_file = run_root / "approvals/approved.json"
        self.assertEqual(exit_code, 0)
        self.assertTrue(approval_file.exists())
        payload = json.loads(approval_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["approved"])
        self.assertEqual(payload["workers"][0]["id"], "worker-01")

    def test_approve_uses_current_run_scoped_state(self) -> None:
        """approve defaults to the .ccx current-run state directory."""
        run_id = "20260416000000000000-demo"
        run_root = self.repo / ".ccx/runs" / run_id
        (self.repo / ".ccx").mkdir()
        (self.repo / ".ccx/current-run").write_text(run_id + "\n", encoding="utf-8")
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "validations").mkdir()
        (run_root / "tasks/worker-01.md").write_text(
            """# Worker Task

## Worker

- ID: worker-01
- Branch: ccx/demo/worker-01
- Worktree: /tmp/worker-01

## Owned Scope

- src/demo.py
""",
            encoding="utf-8",
        )
        (run_root / "validations/worker-01.md").write_text(
            "# Worker Validation\n", encoding="utf-8"
        )

        exit_code = self.run_cli("approve", str(self.repo))

        approval_file = run_root / "approvals/approved.json"
        self.assertEqual(exit_code, 0)
        self.assertTrue(approval_file.exists())

    def test_approve_accepts_explicit_run(self) -> None:
        """approve can target a specific .ccx run."""
        run_id = "20260416000000000000-explicit"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "validations").mkdir()
        (run_root / "tasks/worker-01.md").write_text(
            """# Worker Task

## Worker

- ID: worker-01
- Branch: ccx/demo/worker-01
- Worktree: /tmp/worker-01
""",
            encoding="utf-8",
        )
        (run_root / "validations/worker-01.md").write_text(
            "# Worker Validation\n", encoding="utf-8"
        )

        exit_code = self.run_cli("approve", str(self.repo), "--run", run_id)

        self.assertEqual(exit_code, 0)
        self.assertTrue((run_root / "approvals/approved.json").exists())

    def test_approve_notifies_recorded_worker_surfaces(self) -> None:
        """approve sends a resume prompt to recorded cmux worker surfaces."""
        run_id = "20260416000000000000-notify"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "validations").mkdir()
        (run_root / "tasks/worker-01.md").write_text(
            """# Worker Task

## Worker

- ID: worker-01
- Branch: ccx/demo/worker-01
- Worktree: /tmp/worker-01
""",
            encoding="utf-8",
        )
        (run_root / "validations/worker-01.md").write_text(
            "# Worker Validation\n", encoding="utf-8"
        )
        (run_root / "run-state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "cmux_workspace": "workspace:1",
                    "workers": [
                        {
                            "id": "worker-01",
                            "surface": "surface:9",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("claude_codex.cli.subprocess.run", side_effect=fake_run):
            exit_code = self.run_cli("approve", str(self.repo), "--run", run_id)

        self.assertEqual(exit_code, 0)
        self.assertTrue(any(command[:2] == ["cmux", "send"] for command in calls))
        self.assertTrue(any(command[:2] == ["cmux", "send-key"] for command in calls))
        send_command = next(command for command in calls if command[:2] == ["cmux", "send"])
        self.assertIn("workspace:1", send_command)
        self.assertIn("surface:9", send_command)
        self.assertIn("Approval ready", " ".join(send_command))
        self.assertNotIn("check-barrier", " ".join(send_command))

    def test_check_barrier_refuses_stopped_run(self) -> None:
        """check-barrier blocks implementation when the run is stopped."""
        run_id = "20260416000000000000-stopped"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "approvals").mkdir(parents=True)
        (run_root / "approvals/approved.json").write_text('{"approved": true}\n', encoding="utf-8")
        (run_root / "run-state.json").write_text(
            json.dumps({"run_id": run_id, "status": "stopped"}),
            encoding="utf-8",
        )

        exit_code = self.run_cli("check-barrier", str(self.repo), "--run", run_id)

        self.assertEqual(exit_code, 1)

    def test_validation_notifies_conductor_when_all_workers_approve(self) -> None:
        """The final approve validation nudges the recorded conductor pane."""
        run_id = "20260416000000000000-ready"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "validations").mkdir()
        for worker_id in ("worker-01", "worker-02"):
            (run_root / f"tasks/{worker_id}.md").write_text(
                f"""# Worker Task

## Worker

- ID: {worker_id}
- Branch: ccx/demo/{worker_id}
- Worktree: /tmp/{worker_id}
""",
                encoding="utf-8",
            )
        (run_root / "validations/worker-01.md").write_text(
            "# Worker Validation\n\n## Recommendation\n\nApprove\n",
            encoding="utf-8",
        )
        (run_root / "run-state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "running",
                    "conductor": {
                        "workspace": "workspace:7",
                        "surface": "surface:3",
                    },
                }
            ),
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("claude_codex.cli.subprocess.run", side_effect=fake_run):
            exit_code = self.run_cli(
                "validation",
                str(self.repo),
                "worker-02",
                "--run",
                run_id,
                "--scope-coherence",
                "Scope is coherent.",
                "--overlap-check",
                "No overlap found.",
                "--recommendation",
                "approve",
            )

        self.assertEqual(exit_code, 0)
        send_command = next(command for command in calls if command[:2] == ["cmux", "send"])
        self.assertIn("workspace:7", send_command)
        self.assertIn("surface:3", send_command)
        self.assertIn("ccx approve", " ".join(send_command))

    def test_open_question_blocks_approval(self) -> None:
        """approve refuses to proceed when unresolved questions exist."""
        run_root = self.create_run_state()
        self.run_cli(
            "validation",
            str(self.repo),
            "worker-01",
            "--scope-coherence",
            "Scope is coherent.",
            "--overlap-check",
            "No overlap found.",
            "--recommendation",
            "approve",
        )
        self.run_cli(
            "question",
            str(self.repo),
            "worker-01",
            "--title",
            "Need scope confirmation",
            "--body",
            "Should this worker edit docs too?",
        )

        exit_code = self.run_cli("approve", str(self.repo))

        self.assertEqual(exit_code, 1)
        self.assertFalse((run_root / "approvals/approved.json").exists())

    def test_resolved_question_allows_approval(self) -> None:
        """approve proceeds after a question is moved to the resolved archive."""
        run_root = self.create_run_state()
        self.run_cli(
            "validation",
            str(self.repo),
            "worker-01",
            "--scope-coherence",
            "Scope is coherent.",
            "--overlap-check",
            "No overlap found.",
            "--recommendation",
            "approve",
        )
        self.run_cli(
            "question",
            str(self.repo),
            "worker-01",
            "--title",
            "Need scope confirmation",
            "--body",
            "Should this worker edit docs too?",
        )

        resolve_exit_code = self.run_cli(
            "resolve-question",
            str(self.repo),
            "worker-01-001",
            "--answer",
            "Docs are out of scope for this worker.",
        )
        approve_exit_code = self.run_cli("approve", str(self.repo))

        self.assertEqual(resolve_exit_code, 0)
        self.assertEqual(approve_exit_code, 0)
        self.assertFalse((run_root / "questions/worker-01-001.md").exists())
        self.assertTrue((run_root / "questions/resolved/worker-01-001.md").exists())

    def test_handoff_writes_worker_summary(self) -> None:
        """handoff records worker completion details."""
        run_root = self.create_run_state()

        exit_code = self.run_cli(
            "handoff",
            str(self.repo),
            "worker-01",
            "--branch",
            "worker/demo",
            "--worktree",
            str(self.repo),
            "--summary",
            "Implemented task.",
            "--file",
            "README.md",
            "--test",
            "python -m unittest",
        )

        handoff_file = run_root / "handoffs/worker-01.md"
        self.assertEqual(exit_code, 0)
        self.assertIn("Implemented task.", handoff_file.read_text(encoding="utf-8"))

    def test_handoff_writes_local_fallback_when_shared_state_is_blocked(self) -> None:
        """handoff falls back to the worker worktree when shared state is not writable."""
        run_id = "20260417000000000000-fallback"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "handoffs").mkdir(parents=True)
        worker_worktree = self.repo / "worker-01"
        worker_worktree.mkdir()

        def fake_write_text(path: Path, content: str, *, force: bool = False) -> None:
            if (
                path.name == "worker-01.md"
                and path.parent.name == "handoffs"
                and path.parent.parent.name == run_id
                and ".ccx" in path.parts
            ):
                raise PermissionError("sandbox denied shared state")
            if path.exists() and not force:
                raise AssertionError(f"unexpected overwrite refusal path: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        with (
            patch("claude_codex.cli.write_text", side_effect=fake_write_text),
            patch("claude_codex.cli.Path.cwd", return_value=worker_worktree),
        ):
            exit_code = self.run_cli(
                "handoff",
                str(self.repo),
                "worker-01",
                "--run",
                run_id,
                "--branch",
                "worker/demo",
                "--worktree",
                str(worker_worktree),
                "--summary",
                "Implemented task.",
            )

        fallback_file = worker_worktree / ".ccx-local/runs" / run_id / "handoffs/worker-01.md"
        self.assertEqual(exit_code, 0)
        self.assertFalse((run_root / "handoffs/worker-01.md").exists())
        self.assertIn("Implemented task.", fallback_file.read_text(encoding="utf-8"))

    def test_question_writes_local_fallback_when_shared_state_is_blocked(self) -> None:
        """question falls back to the worker worktree when shared state is not writable."""
        run_id = "20260417000000000000-question-fallback"
        run_root = self.repo / ".ccx/runs" / run_id
        (run_root / "questions").mkdir(parents=True)
        worker_worktree = self.repo / "worker-01"
        worker_worktree.mkdir()

        def fake_write_text(path: Path, content: str, *, force: bool = False) -> None:
            if (
                path.name == "worker-01-001.md"
                and path.parent.name == "questions"
                and path.parent.parent.name == run_id
                and ".ccx" in path.parts
            ):
                raise PermissionError("sandbox denied shared state")
            if path.exists() and not force:
                raise AssertionError(f"unexpected overwrite refusal path: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        with (
            patch("claude_codex.cli.write_text", side_effect=fake_write_text),
            patch("claude_codex.cli.Path.cwd", return_value=worker_worktree),
        ):
            exit_code = self.run_cli(
                "question",
                str(self.repo),
                "worker-01",
                "--run",
                run_id,
                "--title",
                "Need clarification",
                "--body",
                "The assigned scope conflicts with the task.",
            )

        fallback_file = worker_worktree / ".ccx-local/runs" / run_id / "questions/worker-01-001.md"
        self.assertEqual(exit_code, 0)
        self.assertFalse((run_root / "questions/worker-01-001.md").exists())
        self.assertIn("Need clarification", fallback_file.read_text(encoding="utf-8"))

    def test_run_dry_run_uses_claude_plan_without_side_effects(self) -> None:
        """run --dry-run asks for a plan but skips state, worktree, and cmux side effects."""
        plan = {
            "summary": "Update UI",
            "worker_count": 1,
            "tasks": [
                {
                    "title": "UI update",
                    "objective": "Implement the requested UI update.",
                    "owned_scope": ["src/ui"],
                    "non_goals": ["backend changes"],
                    "required_tests": ["npm test"],
                    "risks": ["visual regression"],
                }
            ],
        }

        with (
            patch(
                "claude_codex.runner.check_claude_auth", return_value=authenticated_claude_check()
            ),
            patch("claude_codex.runner.request_plan", return_value=plan),
        ):
            exit_code = self.run_cli(
                "run",
                "--repo",
                str(self.repo),
                "--dry-run",
                "--workers",
                "1",
                "make the UI cleaner",
            )

        self.assertEqual(exit_code, 0)

    def test_run_fails_before_planning_when_claude_is_logged_out(self) -> None:
        """run fails fast with a recovery message when Claude auth is missing."""
        logged_out = ClaudeAuthCheck(
            claude_path="/usr/local/bin/claude",
            logged_in=False,
            auth_method="none",
            api_provider="firstParty",
            error="Claude CLI is not logged in",
            raw_output='{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}',
        )
        stderr = StringIO()

        with (
            patch("claude_codex.runner.check_claude_auth", return_value=logged_out),
            patch("claude_codex.runner.request_plan") as request_plan,
            redirect_stderr(stderr),
        ):
            exit_code = self.run_cli(
                "run",
                "--repo",
                str(self.repo),
                "--dry-run",
                "make the UI cleaner",
            )

        self.assertEqual(exit_code, 1)
        self.assertFalse(request_plan.called)
        self.assertIn("Claude CLI is not logged in", stderr.getvalue())
        self.assertIn("Execute `/login`", stderr.getvalue())

    def test_run_without_request_handles_noninteractive_stdin(self) -> None:
        """run without a request fails cleanly when stdin cannot provide one."""
        with patch("builtins.input", side_effect=EOFError):
            exit_code = self.run_cli("run", "--repo", str(self.repo))

        self.assertEqual(exit_code, 1)

    def test_run_rejects_more_than_five_workers(self) -> None:
        """run caps manual worker requests at the planner maximum."""
        stderr = StringIO()

        with redirect_stderr(stderr):
            exit_code = self.run_cli("run", "--repo", str(self.repo), "--workers", "6", "do work")

        self.assertEqual(exit_code, 1)
        self.assertIn("worker count must be 5 or less", stderr.getvalue())

    def test_default_prompt_interrupt_exits_cleanly(self) -> None:
        """Ctrl-C at the default prompt exits without a traceback."""
        stderr = StringIO()

        with patch("builtins.input", side_effect=KeyboardInterrupt), redirect_stderr(stderr):
            exit_code = self.run_cli()

        self.assertEqual(exit_code, 130)
        self.assertIn("interrupted", stderr.getvalue())

    def test_run_prompt_interrupt_exits_cleanly(self) -> None:
        """Ctrl-C at the run prompt exits without a traceback."""
        stderr = StringIO()

        with patch("builtins.input", side_effect=KeyboardInterrupt), redirect_stderr(stderr):
            exit_code = self.run_cli("run", "--repo", str(self.repo))

        self.assertEqual(exit_code, 130)
        self.assertIn("interrupted", stderr.getvalue())

    def test_slash_menu_lists_claude_and_ccx_commands(self) -> None:
        """slash menu includes Claude references and ccx command labels."""
        from claude_codex.runner import show_slash_menu

        stdout = StringIO()

        with redirect_stdout(stdout):
            show_slash_menu()

        output = stdout.getvalue()
        self.assertIn("/browse", output)
        self.assertIn("/usage", output)
        self.assertIn("/status", output)
        self.assertIn("status(ccx)", output)

    def test_slash_usage_is_claude_reference(self) -> None:
        """usage is treated as a Claude-native reference, not an unknown command."""
        from claude_codex.runner import handle_slash_command

        stdout = StringIO()

        with redirect_stdout(stdout):
            result = handle_slash_command("/usage", self.repo)

        self.assertIsNone(result)
        self.assertIn("Claude command /usage", stdout.getvalue())

    def test_unknown_slash_command_is_explained(self) -> None:
        """unknown slash commands tell the user where Claude-native commands work."""
        from claude_codex.runner import handle_slash_command

        stdout = StringIO()

        with redirect_stdout(stdout):
            result = handle_slash_command("/not-a-command", self.repo)

        self.assertIsNone(result)
        self.assertIn("not handled by the ccx pre-launch prompt", stdout.getvalue())

    def test_slash_exit_returns_empty_request(self) -> None:
        """slash exit command exits the pre-launch prompt."""
        from claude_codex.runner import handle_slash_command

        self.assertEqual(handle_slash_command("/exit", self.repo), "")

    def test_run_skip_launch_uses_run_scoped_state(self) -> None:
        """run --skip-launch writes run-scoped state under .ccx/runs."""
        plan = {
            "summary": "Update UI",
            "worker_count": 1,
            "tasks": [
                {
                    "title": "UI update",
                    "objective": "Implement the requested UI update.",
                    "owned_scope": ["src/ui"],
                    "non_goals": ["backend changes"],
                    "required_tests": ["npm test"],
                    "risks": ["visual regression"],
                }
            ],
        }

        with (
            patch(
                "claude_codex.runner.check_claude_auth", return_value=authenticated_claude_check()
            ),
            patch("claude_codex.runner.request_plan", return_value=plan),
        ):
            exit_code = self.run_cli(
                "run",
                "--repo",
                str(self.repo),
                "--skip-launch",
                "--workers",
                "1",
                "make the UI cleaner",
            )

        runs_dir = self.repo / ".ccx/runs"
        run_dirs = list(runs_dir.iterdir())
        current_run = (self.repo / ".ccx/current-run").read_text(encoding="utf-8").strip()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(run_dirs), 1)
        self.assertEqual(run_dirs[0].name, current_run)
        self.assertTrue((run_dirs[0] / "run-state.json").exists())
        conductor_prompt = (run_dirs[0] / "prompts/claude-conductor.md").read_text(encoding="utf-8")
        worker_prompt = (run_dirs[0] / "prompts/worker-01.md").read_text(encoding="utf-8")
        installed_hard_rules_path = installed_worker_hard_rules_path()
        hard_rules_prompt = installed_hard_rules_path.read_text(encoding="utf-8")
        self.assertFalse((run_dirs[0] / "prompts/hard_rules.md").exists())
        resolved_repo = self.repo.resolve()
        self.assertIn("Esc may interrupt Claude/Codex without notifying ccx", conductor_prompt)
        self.assertIn(f"ccx status {resolved_repo} --run {current_run} --json", conductor_prompt)
        self.assertIn("Do not ask the user whether to poll, wait, or watch", conductor_prompt)
        self.assertIn(
            f"Immediately run `ccx watch {resolved_repo} --run {current_run} --once`",
            conductor_prompt,
        )
        self.assertIn(str(installed_hard_rules_path), worker_prompt)
        self.assertIn("Task assignment:", worker_prompt)
        self.assertIn("- title: UI update", worker_prompt)
        self.assertIn("- objective: Implement the requested UI update.", worker_prompt)
        self.assertNotIn("Request:\nmake the UI cleaner", worker_prompt)
        self.assertIn("Do not use `@file`", worker_prompt)
        self.assertNotIn(f"@{installed_hard_rules_path}", worker_prompt)
        self.assertNotIn(f"ccx stop {resolved_repo} --run {current_run}", worker_prompt)
        self.assertIn("ccx-worker-protocol/v1", hard_rules_prompt)
        self.assertIn("Do not merge, push, or run `ccx stop`", hard_rules_prompt)
        self.assertLess(len(hard_rules_prompt.split()), 140)
        self.assertIn(f"until ccx check-barrier {resolved_repo} --run {current_run}", worker_prompt)

    def test_run_no_conductor_launches_workers_without_nested_claude(self) -> None:
        """run --no-conductor launches workers and leaves current Claude as conductor."""
        from claude_codex.runner import WorkerLaunch, WorkerPane

        plan = {
            "summary": "Update UI",
            "worker_count": 1,
            "tasks": [
                {
                    "title": "UI update",
                    "objective": "Implement the requested UI update.",
                    "owned_scope": ["src/ui"],
                    "non_goals": ["backend changes"],
                    "required_tests": ["npm test"],
                    "risks": ["visual regression"],
                }
            ],
        }

        with (
            patch.dict(
                os.environ,
                {
                    "CMUX_WORKSPACE_ID": "workspace:conductor",
                    "CMUX_SURFACE_ID": "surface:conductor",
                },
            ),
            patch(
                "claude_codex.runner.check_claude_auth", return_value=authenticated_claude_check()
            ),
            patch("claude_codex.runner.request_plan", return_value=plan),
            patch(
                "claude_codex.runner.launch_cmux_workers_in_current_workspace",
                return_value=WorkerLaunch(
                    workspace="workspace:1",
                    panes=[
                        WorkerPane(
                            worker_id="worker-01",
                            pane="pane:1",
                            surface="surface:1",
                            title="worker-01: UI update",
                        )
                    ],
                ),
            ),
            patch("claude_codex.runner.launch_cmux_workers") as new_workspace_launcher,
            patch("claude_codex.runner.run_conductor_foreground") as conductor,
        ):
            exit_code = self.run_cli(
                "run",
                "--repo",
                str(self.repo),
                "--no-conductor",
                "--workers",
                "1",
                "make the UI cleaner",
            )

        current_run = (self.repo / ".ccx/current-run").read_text(encoding="utf-8").strip()
        run_state = json.loads(
            (self.repo / ".ccx/runs" / current_run / "run-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(run_state["status"], "running")
        self.assertEqual(run_state["cmux_workspace"], "workspace:1")
        self.assertEqual(run_state["conductor"]["workspace"], "workspace:conductor")
        self.assertEqual(run_state["conductor"]["surface"], "surface:conductor")
        self.assertEqual(run_state["workers"][0]["surface"], "surface:1")
        self.assertEqual(run_state["workers"][0]["tab_title"], "worker-01: UI update")
        self.assertFalse(conductor.called)
        self.assertFalse(new_workspace_launcher.called)

    def test_create_worktrees_overlay_source_snapshot(self) -> None:
        """worker worktrees include uncommitted tracked changes and untracked files."""
        from claude_codex.runner import Plan, WorkerTask, create_worktrees

        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "test: add tracked file"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        (self.repo / "tracked.txt").write_text("dirty working tree\n", encoding="utf-8")
        (self.repo / "benchmark_matmul.cpp").write_text(
            "int main() { return 0; }\n", encoding="utf-8"
        )
        (self.repo / "README.md").unlink()
        (self.repo / ".ccx/runs/demo").mkdir(parents=True)
        (self.repo / ".ccx/runs/demo/state.json").write_text("{}\n", encoding="utf-8")

        worktree_root = Path(self.temp_dir.name) / "worktrees"
        integration_worktree = worktree_root / "integration"
        worker_worktree = worktree_root / "worker-01"
        plan = Plan(
            summary="Port benchmark.",
            worker_count=1,
            tasks=[
                WorkerTask(
                    worker_id="worker-01",
                    title="Java port",
                    objective="Implement BenchmarkMatMul.java.",
                    owned_scope=["BenchmarkMatMul.java"],
                    non_goals=[],
                    required_tests=[],
                    risks=[],
                    branch="ccx/test-overlay/worker-01",
                    worktree=worker_worktree,
                )
            ],
        )

        create_worktrees(self.repo, "test-overlay", plan, integration_worktree)

        self.assertEqual(
            (worker_worktree / "tracked.txt").read_text(encoding="utf-8"),
            "dirty working tree\n",
        )
        self.assertTrue((worker_worktree / "benchmark_matmul.cpp").exists())
        self.assertFalse((worker_worktree / "README.md").exists())
        self.assertFalse((worker_worktree / ".ccx").exists())
        self.assertTrue((integration_worktree / "benchmark_matmul.cpp").exists())

    def test_current_workspace_worker_launcher_does_not_create_workspace(self) -> None:
        """Claude-first worker launch adds panes to the current cmux workspace."""
        from claude_codex.runner import (
            Plan,
            RunConfig,
            WorkerTask,
            launch_cmux_workers_in_current_workspace,
        )

        calls: list[list[str]] = []

        def fake_run_command(command: list[str], **_: object) -> str:
            calls.append(command)
            if command == ["cmux", "current-workspace"]:
                return "workspace:7"
            if command[:2] == ["cmux", "list-panes"]:
                return "* pane:1  [1 surface]  [focused]"
            if command[:2] == ["cmux", "focus-pane"]:
                return ""
            if command[:2] == ["cmux", "new-pane"]:
                count = len([call for call in calls if call[:2] == ["cmux", "new-pane"]])
                return f"pane:{count + 1}"
            if command[:2] == ["cmux", "list-pane-surfaces"]:
                pane = command[command.index("--pane") + 1]
                return f"surface:{pane.split(':')[1]}"
            if command[:2] == ["cmux", "respawn-pane"]:
                return ""
            if command[:2] == ["cmux", "rename-tab"]:
                return ""
            raise AssertionError(f"unexpected command: {command}")

        tasks = []
        prompt_paths = {}
        for index in range(1, 6):
            worker_id = f"worker-{index:02d}"
            tasks.append(
                WorkerTask(
                    worker_id=worker_id,
                    title=f"Worker {index}",
                    objective="Implement it.",
                    owned_scope=["src/ui"],
                    non_goals=[],
                    required_tests=[],
                    risks=[],
                    branch=f"ccx/run/{worker_id}",
                    worktree=self.repo / worker_id,
                )
            )
            prompt_path = self.repo / f"{worker_id}.md"
            prompt_path.write_text("worker prompt\n", encoding="utf-8")
            prompt_paths[worker_id] = prompt_path
        config = RunConfig(
            repo=self.repo,
            request="make the UI cleaner",
            claude_model="opus",
            claude_effort="medium",
            codex_model="gpt-5.3-codex",
            codex_effort="medium",
            requested_workers=1,
            dry_run=False,
            skip_launch=False,
            force_state=False,
            skip_conductor=True,
        )

        with (
            patch("claude_codex.runner.run_command", side_effect=fake_run_command),
            patch.dict(os.environ, {"CMUX_WORKSPACE_ID": "workspace:captured"}),
        ):
            launch = launch_cmux_workers_in_current_workspace(
                config,
                Plan(summary="Update UI", worker_count=5, tasks=tasks),
                prompt_paths,
                "run-1",
            )

        flattened = [" ".join(command[:2]) for command in calls]
        self.assertEqual(launch.workspace, "workspace:captured")
        self.assertEqual(
            [pane.pane for pane in launch.panes],
            ["pane:2", "pane:3", "pane:4", "pane:5", "pane:6"],
        )
        self.assertEqual(
            [pane.surface for pane in launch.panes],
            ["surface:2", "surface:3", "surface:4", "surface:5", "surface:6"],
        )
        self.assertNotIn(["cmux", "current-workspace"], calls)
        self.assertNotIn("cmux new-workspace", flattened)
        self.assertIn("cmux new-pane", flattened)
        self.assertIn("cmux respawn-pane", flattened)
        split_commands = [command for command in calls if command[:2] == ["cmux", "new-pane"]]
        self.assertEqual(
            [command[command.index("--direction") + 1] for command in split_commands],
            ["right", "down", "down", "right", "right"],
        )
        focus_commands = [command for command in calls if command[:2] == ["cmux", "focus-pane"]]
        self.assertEqual(
            [command[command.index("--pane") + 1] for command in focus_commands],
            ["pane:1", "pane:1", "pane:2", "pane:3", "pane:4", "pane:1"],
        )
        respawn_command = next(
            command for command in calls if command[:2] == ["cmux", "respawn-pane"]
        )
        self.assertIn(f"--add-dir {self.repo / '.ccx/runs/run-1'}", " ".join(respawn_command))
        rename_commands = [command for command in calls if command[:2] == ["cmux", "rename-tab"]]
        self.assertEqual(
            [command[-1] for command in rename_commands],
            [
                "worker-01: Worker 1",
                "worker-02: Worker 2",
                "worker-03: Worker 3",
                "worker-04: Worker 4",
                "worker-05: Worker 5",
            ],
        )

    def test_codex_child_command_adds_shared_state_writable_root(self) -> None:
        """Codex workers receive the run state directory as an extra writable root."""
        from claude_codex.runner import codex_child_command

        run_root = self.repo / ".ccx/runs/run-1"

        command = codex_child_command(
            model="gpt-5.3-codex",
            effort="medium",
            worktree=self.repo / "worker-01",
            writable_roots=[run_root],
        )

        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--ask-for-approval", command)
        self.assertIn("never", command)
        self.assertIn("--add-dir", command)
        self.assertEqual(command[command.index("--add-dir") + 1], str(run_root))

    def test_status_supports_target_repo(self) -> None:
        """status prints runtime state for a target repository."""
        exit_code = self.run_cli("status", str(self.repo))

        self.assertEqual(exit_code, 0)

    def test_status_without_run_uses_ccx_runs_root(self) -> None:
        """runtime status defaults to the ccx runs root when no run exists."""
        from claude_codex.runner import runtime_status

        status = runtime_status(self.repo)

        self.assertEqual(status["status"], "not-started")
        self.assertEqual(status["state_dir"], str(self.repo.resolve() / ".ccx/runs"))

    def test_status_counts_worker_local_handoff_fallbacks(self) -> None:
        """runtime status includes handoffs written under worker-local fallback state."""
        from claude_codex.runner import runtime_status

        run_id = "20260417000000000000-local-handoff"
        run_root = self.repo / ".ccx/runs" / run_id
        worker_worktree = self.repo / "worker-01"
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "handoffs").mkdir()
        worker_worktree.mkdir()
        (run_root / "tasks/worker-01.md").write_text("# Worker Task\n", encoding="utf-8")
        (run_root / "run-state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "running",
                    "workers": [
                        {
                            "id": "worker-01",
                            "worktree": str(worker_worktree),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fallback_file = worker_worktree / ".ccx-local/runs" / run_id / "handoffs/worker-01.md"
        fallback_file.parent.mkdir(parents=True)
        fallback_file.write_text("# Worker Handoff\n", encoding="utf-8")

        status = runtime_status(self.repo, run_id)

        self.assertEqual(status["counts"]["handoffs"], 1)
        self.assertEqual(status["counts"]["local_handoffs"], 1)

    def test_local_question_fallback_blocks_and_resolves_approval(self) -> None:
        """local fallback questions are visible to status and block approval."""
        from claude_codex.runner import runtime_status

        run_id = "20260417000000000000-local-question"
        run_root = self.repo / ".ccx/runs" / run_id
        worker_worktree = self.repo / "worker-01"
        (run_root / "tasks").mkdir(parents=True)
        (run_root / "validations").mkdir()
        (run_root / "questions").mkdir()
        (run_root / "approvals").mkdir()
        worker_worktree.mkdir()
        (run_root / "tasks/worker-01.md").write_text("# Worker Task\n", encoding="utf-8")
        (run_root / "validations/worker-01.md").write_text(
            "# Worker Validation\n\n## Recommendation\n\napprove\n",
            encoding="utf-8",
        )
        (run_root / "run-state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "running",
                    "workers": [
                        {
                            "id": "worker-01",
                            "worktree": str(worker_worktree),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fallback_file = worker_worktree / ".ccx-local/runs" / run_id / "questions/worker-01-001.md"
        fallback_file.parent.mkdir(parents=True)
        fallback_file.write_text("# Worker Question\n\nNeed scope decision.\n", encoding="utf-8")

        status = runtime_status(self.repo, run_id)
        approve_exit_code = self.run_cli("approve", str(self.repo), "--run", run_id)

        self.assertEqual(status["counts"]["questions"], 1)
        self.assertEqual(status["counts"]["local_questions"], 1)
        self.assertEqual(approve_exit_code, 1)
        self.assertFalse((run_root / "approvals/approved.json").exists())

        resolve_exit_code = self.run_cli(
            "resolve-question",
            str(self.repo),
            "worker-01-001",
            "--run",
            run_id,
            "--answer",
            "Worker scope is approved as assigned.",
        )
        approve_after_resolve = self.run_cli("approve", str(self.repo), "--run", run_id)

        self.assertEqual(resolve_exit_code, 0)
        self.assertEqual(approve_after_resolve, 0)
        self.assertFalse(fallback_file.exists())
        self.assertTrue((run_root / "questions/resolved/worker-01-001.md").exists())
        self.assertTrue((run_root / "approvals/approved.json").exists())

    def test_agent_command_wraps_child_with_prompt(self) -> None:
        """agent runs a child command with the prompt appended."""
        prompt_path = self.repo / "prompt.md"
        prompt_path.write_text("hello agent\n", encoding="utf-8")

        exit_code = self.run_cli(
            "agent",
            "--repo",
            str(self.repo),
            "--run",
            "run-1",
            "--role",
            "conductor",
            "--prompt",
            str(prompt_path),
            "--",
            sys.executable,
            "-c",
            "import sys; raise SystemExit(0 if sys.argv[-1] == 'hello agent\\n' else 2)",
        )

        self.assertEqual(exit_code, 0)

    def test_foreground_agent_script_appends_prompt_as_child_argument(self) -> None:
        """foreground launcher runs the child command with the prompt argument."""
        from claude_codex.runner import foreground_agent_script

        prompt_path = self.repo / "prompt.md"
        prompt_path.write_text("hello conductor\n", encoding="utf-8")
        script = foreground_agent_script(repo=self.repo, run_id="run-1", prompt_path=prompt_path)

        completed = subprocess.run(
            [
                "/bin/zsh",
                "-lc",
                script,
                "ccx-foreground-agent",
                sys.executable,
                "-c",
                "import sys; raise SystemExit(0 if sys.argv[-1] == 'hello conductor' else 2)",
            ],
            check=False,
            env={**os.environ, "CCX_PROMPT_PATH": str(prompt_path)},
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_agent_worker_requires_worker_id(self) -> None:
        """worker wrappers require an explicit worker id."""
        prompt_path = self.repo / "prompt.md"
        prompt_path.write_text("hello agent\n", encoding="utf-8")

        exit_code = self.run_cli(
            "agent",
            "--repo",
            str(self.repo),
            "--run",
            "run-1",
            "--role",
            "worker",
            "--prompt",
            str(prompt_path),
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        )

        self.assertEqual(exit_code, 1)

    def test_mark_runtime_stopped_records_signal_metadata(self) -> None:
        """signal stop metadata is persisted in the run state."""
        from claude_codex.runner import mark_runtime_stopped, write_current_run, write_runtime_state

        run_id = "20260416000000000000-test"
        write_current_run(self.repo, run_id)
        write_runtime_state(self.repo, {"run_id": run_id, "status": "running"}, run_id)

        mark_runtime_stopped(
            self.repo,
            run_id=run_id,
            stopped_by="signal",
            stop_reason="SIGINT",
            stopped_agent="worker:worker-01",
        )

        state_path = self.repo / ".ccx/runs" / run_id / "run-state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual(payload["stopped_by"], "signal")
        self.assertEqual(payload["stop_reason"], "SIGINT")
        self.assertEqual(payload["stopped_agent"], "worker:worker-01")

    def test_install_claude_commands_writes_user_commands(self) -> None:
        """install-claude-commands writes ccx slash commands under ~/.claude."""
        fake_home = Path(self.temp_dir.name) / "home"
        fake_home.mkdir()

        with patch("pathlib.Path.home", return_value=fake_home):
            exit_code = self.run_cli("install-claude-commands")

        self.assertEqual(exit_code, 0)
        command_file = fake_home / ".claude/commands/ccx-status.md"
        run_command_file = fake_home / ".claude/commands/ccx-run.md"
        self.assertTrue(command_file.exists())
        self.assertTrue(run_command_file.exists())
        status_content = command_file.read_text(encoding="utf-8")
        run_content = run_command_file.read_text(encoding="utf-8")
        self.assertIn("status(ccx)", status_content)
        self.assertIn("Bash(ccx:*)", status_content)
        self.assertIn("Bash(cmux read-screen:*)", run_content)
        self.assertIn("ccx run --no-conductor", run_content)
        self.assertIn("Do not chain", run_content)

    def test_doctor_runs(self) -> None:
        """doctor returns a process status after checking external commands."""
        exit_code = self.run_cli("doctor")

        self.assertIn(exit_code, {0, 1})

    def test_doctor_reports_claude_auth_failure(self) -> None:
        """doctor explains missing Claude auth."""
        logged_out = ClaudeAuthCheck(
            claude_path="/usr/local/bin/claude",
            logged_in=False,
            auth_method="none",
            api_provider="firstParty",
            error="Claude CLI is not logged in",
        )
        stdout = StringIO()

        with (
            patch("claude_codex.cli.shutil.which", return_value="/usr/local/bin/tool"),
            patch("claude_codex.cli.check_claude_auth", return_value=logged_out),
            redirect_stdout(stdout),
        ):
            exit_code = self.run_cli("doctor")

        self.assertEqual(exit_code, 1)
        self.assertIn("[!!] claude auth: Claude CLI is not logged in", stdout.getvalue())

    def test_claude_auth_check_parses_logged_out_json_with_nonzero_exit(self) -> None:
        """Claude may return logged-out JSON with a nonzero exit code."""
        from claude_codex.preflight import check_claude_auth

        completed = subprocess.CompletedProcess(
            ["claude", "auth", "status"],
            1,
            stdout='{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}',
            stderr="",
        )

        with (
            patch("claude_codex.preflight.shutil.which", return_value="/usr/local/bin/claude"),
            patch("claude_codex.preflight.subprocess.run", return_value=completed),
        ):
            check = check_claude_auth()

        self.assertFalse(check.logged_in)
        self.assertEqual(check.error, "Claude CLI is not logged in")
        self.assertEqual(check.auth_method, "none")
        self.assertEqual(check.api_provider, "firstParty")

    def test_parse_json_object_extracts_structured_output(self) -> None:
        """Claude --output-format json nests JSON schema results under structured_output."""
        from claude_codex.runner import parse_json_object

        raw_output = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "",
                "structured_output": {
                    "summary": "Update UI",
                    "worker_count": 1,
                    "tasks": [{"title": "UI update"}],
                },
            }
        )

        plan = parse_json_object(raw_output)

        self.assertEqual(plan["summary"], "Update UI")
        self.assertEqual(plan["worker_count"], 1)

    def test_request_plan_uses_json_output_format(self) -> None:
        """Structured planner calls request Claude's JSON wrapper output."""
        from claude_codex.runner import RunConfig, request_plan

        captured_command: list[str] = []

        class FakeProcess:
            """Minimal process stub for Claude planner subprocess."""

            returncode = 0

            def communicate(self, timeout: int) -> tuple[str, str]:
                output = json.dumps(
                    {
                        "structured_output": {
                            "summary": "Update UI",
                            "worker_count": 1,
                            "tasks": [
                                {
                                    "title": "UI update",
                                    "objective": "Implement it.",
                                    "owned_scope": ["src"],
                                    "non_goals": [],
                                    "required_tests": [],
                                }
                            ],
                        }
                    }
                )
                return output, ""

        def fake_popen(command: list[str], **_: object) -> FakeProcess:
            captured_command.extend(command)
            return FakeProcess()

        config = RunConfig(
            repo=self.repo,
            request="make the UI cleaner",
            claude_model="opus",
            claude_effort="medium",
            codex_model="gpt-5.3-codex",
            codex_effort="medium",
            requested_workers=1,
            dry_run=True,
            skip_launch=False,
            force_state=False,
        )

        with (
            patch("claude_codex.runner.collect_repo_snapshot", return_value="Repository snapshot"),
            patch("claude_codex.runner.subprocess.Popen", side_effect=fake_popen),
        ):
            plan = request_plan(config)

        self.assertEqual(plan["summary"], "Update UI")
        self.assertIn("--output-format", captured_command)
        self.assertIn("json", captured_command)

    def test_planner_prompt_caps_workers_at_five(self) -> None:
        """Claude planner instructions advertise the five-worker cap."""
        from claude_codex.runner import RunConfig, planner_prompt, planner_schema

        config = RunConfig(
            repo=self.repo,
            request="make the UI cleaner",
            claude_model="opus",
            claude_effort="medium",
            codex_model="gpt-5.3-codex",
            codex_effort="medium",
            requested_workers=None,
            dry_run=True,
            skip_launch=False,
            force_state=False,
        )
        schema = json.loads(planner_schema())

        self.assertIn("Choose 1-5 Codex workers.", planner_prompt(config, "snapshot"))
        self.assertEqual(schema["properties"]["worker_count"]["maximum"], 5)
        self.assertEqual(schema["properties"]["tasks"]["maxItems"], 5)


if __name__ == "__main__":
    unittest.main()

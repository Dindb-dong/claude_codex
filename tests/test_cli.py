"""Regression tests for the claude-codex CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from claude_codex.cli import main


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

    def test_init_creates_expected_state(self) -> None:
        """init creates plan, worktree, and task files."""
        exit_code = self.run_cli("init", str(self.repo), "demo", "2")

        self.assertEqual(exit_code, 0)
        self.assertTrue((self.repo / ".orchestrator/plan.md").exists())
        self.assertTrue((self.repo / ".orchestrator/worktrees.md").exists())
        self.assertTrue((self.repo / ".orchestrator/tasks/worker-01.md").exists())
        self.assertTrue((self.repo / ".orchestrator/tasks/worker-02.md").exists())

    def test_approve_requires_validations_without_force(self) -> None:
        """approve refuses to create a barrier before worker validations exist."""
        self.run_cli("init", str(self.repo), "demo", "1")

        exit_code = self.run_cli("approve", str(self.repo))

        self.assertEqual(exit_code, 1)
        self.assertFalse((self.repo / ".orchestrator/approvals/approved.json").exists())

    def test_approve_writes_barrier_after_validation(self) -> None:
        """approve writes approved.json after all validations are present."""
        self.run_cli("init", str(self.repo), "demo", "1")
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

        approval_file = self.repo / ".orchestrator/approvals/approved.json"
        self.assertEqual(exit_code, 0)
        self.assertTrue(approval_file.exists())
        payload = json.loads(approval_file.read_text(encoding="utf-8"))
        self.assertTrue(payload["approved"])
        self.assertEqual(payload["workers"][0]["id"], "worker-01")

    def test_open_question_blocks_approval(self) -> None:
        """approve refuses to proceed when unresolved questions exist."""
        self.run_cli("init", str(self.repo), "demo", "1")
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
        self.assertFalse((self.repo / ".orchestrator/approvals/approved.json").exists())

    def test_resolved_question_allows_approval(self) -> None:
        """approve proceeds after a question is moved to the resolved archive."""
        self.run_cli("init", str(self.repo), "demo", "1")
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
        self.assertFalse((self.repo / ".orchestrator/questions/worker-01-001.md").exists())
        self.assertTrue((self.repo / ".orchestrator/questions/resolved/worker-01-001.md").exists())

    def test_handoff_writes_worker_summary(self) -> None:
        """handoff records worker completion details."""
        self.run_cli("init", str(self.repo), "demo", "1")

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

        handoff_file = self.repo / ".orchestrator/handoffs/worker-01.md"
        self.assertEqual(exit_code, 0)
        self.assertIn("Implemented task.", handoff_file.read_text(encoding="utf-8"))

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

        with patch("claude_codex.runner.request_plan", return_value=plan):
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
        self.assertFalse((self.repo / ".orchestrator").exists())

    def test_run_without_request_handles_noninteractive_stdin(self) -> None:
        """run without a request fails cleanly when stdin cannot provide one."""
        with patch("builtins.input", side_effect=EOFError):
            exit_code = self.run_cli("run", "--repo", str(self.repo))

        self.assertEqual(exit_code, 1)

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

        with patch("claude_codex.runner.request_plan", return_value=plan):
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
        self.assertFalse((self.repo / ".orchestrator").exists())
        conductor_prompt = (run_dirs[0] / "prompts/claude-conductor.md").read_text(encoding="utf-8")
        worker_prompt = (run_dirs[0] / "prompts/worker-01.md").read_text(encoding="utf-8")
        resolved_repo = self.repo.resolve()
        self.assertIn("Esc may interrupt Claude/Codex without notifying ccx", conductor_prompt)
        self.assertIn(f"ccx status {resolved_repo} --run {current_run} --json", conductor_prompt)
        self.assertIn(f"ccx stop {resolved_repo} --run {current_run}", worker_prompt)

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
        self.assertTrue(command_file.exists())
        self.assertIn("status(ccx)", command_file.read_text(encoding="utf-8"))

    def test_doctor_runs(self) -> None:
        """doctor returns a process status after checking external commands."""
        exit_code = self.run_cli("doctor")

        self.assertIn(exit_code, {0, 1})


if __name__ == "__main__":
    unittest.main()

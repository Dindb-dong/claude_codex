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
        self.assertIn("ccx approval received", " ".join(send_command))

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
        self.assertFalse((self.repo / ".orchestrator").exists())

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
        self.assertFalse((self.repo / ".orchestrator").exists())
        conductor_prompt = (run_dirs[0] / "prompts/claude-conductor.md").read_text(encoding="utf-8")
        worker_prompt = (run_dirs[0] / "prompts/worker-01.md").read_text(encoding="utf-8")
        resolved_repo = self.repo.resolve()
        self.assertIn("Esc may interrupt Claude/Codex without notifying ccx", conductor_prompt)
        self.assertIn(f"ccx status {resolved_repo} --run {current_run} --json", conductor_prompt)
        self.assertIn(f"ccx stop {resolved_repo} --run {current_run}", worker_prompt)
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
        self.assertEqual(run_state["workers"][0]["surface"], "surface:1")
        self.assertFalse(conductor.called)
        self.assertFalse(new_workspace_launcher.called)

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
            if command[:2] == ["cmux", "new-pane"]:
                return f"pane:{len([call for call in calls if call[:2] == ['cmux', 'new-pane']])}"
            if command[:2] == ["cmux", "list-pane-surfaces"]:
                return "surface:9"
            if command[:2] == ["cmux", "respawn-pane"]:
                return ""
            raise AssertionError(f"unexpected command: {command}")

        task = WorkerTask(
            worker_id="worker-01",
            title="UI update",
            objective="Implement it.",
            owned_scope=["src/ui"],
            non_goals=[],
            required_tests=[],
            risks=[],
            branch="ccx/run/worker-01",
            worktree=self.repo / "worker-01",
        )
        prompt_path = self.repo / "worker-01.md"
        prompt_path.write_text("worker prompt\n", encoding="utf-8")
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
                Plan(summary="Update UI", worker_count=1, tasks=[task]),
                {"worker-01": prompt_path},
                "run-1",
            )

        flattened = [" ".join(command[:2]) for command in calls]
        self.assertEqual(launch.workspace, "workspace:captured")
        self.assertEqual(launch.panes[0].surface, "surface:9")
        self.assertNotIn(["cmux", "current-workspace"], calls)
        self.assertNotIn("cmux new-workspace", flattened)
        self.assertIn("cmux new-pane", flattened)
        self.assertIn("cmux respawn-pane", flattened)
        respawn_command = next(
            command for command in calls if command[:2] == ["cmux", "respawn-pane"]
        )
        self.assertIn(f"--add-dir {self.repo / '.ccx/runs/run-1'}", " ".join(respawn_command))

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
        self.assertIn("status(ccx)", command_file.read_text(encoding="utf-8"))
        self.assertIn("ccx run --no-conductor", run_command_file.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()

"""Preflight checks for local agent CLI dependencies."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeAuthCheck:
    """Claude CLI authentication check result.

    Args:
        claude_path: Resolved Claude executable path, when available.
        logged_in: Whether Claude reports an authenticated CLI session.
        auth_method: Claude auth method reported by the CLI.
        api_provider: Claude API provider reported by the CLI.
        error: User-facing error when the check could not pass.
        raw_output: Bounded raw command output for diagnostics.
    """

    claude_path: str | None
    logged_in: bool
    auth_method: str
    api_provider: str
    error: str = ""
    raw_output: str = ""


def check_claude_auth(timeout: int = 10) -> ClaudeAuthCheck:
    """Check whether the Claude Code CLI is authenticated.

    Args:
        timeout: Timeout in seconds for `claude auth status`.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        return ClaudeAuthCheck(
            claude_path=None,
            logged_in=False,
            auth_method="none",
            api_provider="unknown",
            error="claude CLI not found in PATH",
        )
    try:
        completed = subprocess.run(
            ["claude", "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ClaudeAuthCheck(
            claude_path=claude_path,
            logged_in=False,
            auth_method="unknown",
            api_provider="unknown",
            error=f"`claude auth status` timed out after {timeout}s",
        )
    raw_output = (completed.stdout or completed.stderr).strip()
    try:
        payload = json.loads(completed.stdout or raw_output)
    except json.JSONDecodeError:
        if completed.returncode != 0:
            return ClaudeAuthCheck(
                claude_path=claude_path,
                logged_in=False,
                auth_method="unknown",
                api_provider="unknown",
                error=f"`claude auth status` failed with exit {completed.returncode}",
                raw_output=raw_output[:1200],
            )
        return ClaudeAuthCheck(
            claude_path=claude_path,
            logged_in=False,
            auth_method="unknown",
            api_provider="unknown",
            error="`claude auth status` did not return JSON",
            raw_output=raw_output[:1200],
        )
    if not isinstance(payload, dict):
        return ClaudeAuthCheck(
            claude_path=claude_path,
            logged_in=False,
            auth_method="unknown",
            api_provider="unknown",
            error="`claude auth status` returned an unexpected payload",
            raw_output=raw_output[:1200],
        )
    logged_in = bool(payload.get("loggedIn"))
    auth_method = str(payload.get("authMethod") or "none")
    api_provider = str(payload.get("apiProvider") or "unknown")
    return ClaudeAuthCheck(
        claude_path=claude_path,
        logged_in=logged_in,
        auth_method=auth_method,
        api_provider=api_provider,
        error="" if logged_in else "Claude CLI is not logged in",
        raw_output=raw_output[:1200],
    )


def claude_auth_failure_message(check: ClaudeAuthCheck) -> str:
    """Build a concise Claude auth recovery message.

    Args:
        check: Failed auth check result.
    """
    details = [
        check.error or "Claude CLI is not logged in",
        "",
        "ccx uses non-interactive `claude --print` for planning, so it needs the",
        "same Claude Code CLI binary and terminal environment to be authenticated.",
        "",
        "Fix:",
        "  1. Run `claude` in this same terminal.",
        "  2. Execute `/login` inside Claude Code.",
        "  3. Retry `ccx`.",
        "",
        f"Checked claude: {check.claude_path or 'not found in PATH'}",
        f"Auth method: {check.auth_method}",
        f"API provider: {check.api_provider}",
    ]
    if check.raw_output:
        details.extend(["", f"Auth status output: {check.raw_output}"])
    return "\n".join(details)

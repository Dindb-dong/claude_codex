#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Compatibility wrapper for the legacy manual .orchestrator template flow.
printf '%s\n' 'warning: bootstrap-run uses legacy ccx init templates; prefer ccx run or /ccx-run for normal orchestration.' >&2
exec "$SCRIPT_DIR/claude-codex" init "$@"

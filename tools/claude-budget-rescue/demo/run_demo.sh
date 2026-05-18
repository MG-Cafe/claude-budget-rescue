#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/../.."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

python3 - "$TMPDIR/status.json" "$TMPDIR/hook.json" "$PWD" "$ROOT/demo/demo_transcript.jsonl" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
hook_path = Path(sys.argv[2])
cwd = sys.argv[3]
transcript = sys.argv[4]

demo_dir = Path(cwd) / "tools" / "claude-budget-rescue" / "demo"
status = json.loads((demo_dir / "demo_statusline_input.json").read_text())
hook = json.loads((demo_dir / "demo_stop_hook_input.json").read_text())

for payload in (status, hook):
    payload["cwd"] = cwd
    payload["transcript_path"] = transcript

status_path.write_text(json.dumps(status))
hook_path.write_text(json.dumps(hook))
PY

echo "1) Simulate Claude Code statusline near the limit"
python3 tools/claude-budget-rescue/budget_rescue.py statusline \
  < "$TMPDIR/status.json"

echo
echo "2) Show plan-aware automation budget math"
python3 tools/claude-budget-rescue/budget_rescue.py estimate \
  --plan pro \
  --model claude-sonnet-4.6 \
  --input-tokens 50000 \
  --output-tokens 15000

echo
echo "3) Simulate Stop hook triggering a rescue package"
python3 tools/claude-budget-rescue/budget_rescue.py hook --force --dry-run \
  < "$TMPDIR/hook.json"

echo
echo "4) Latest handoff folder"
ls -td .agent-handoff/* | head -1

echo
echo "5) Codex rescue command"
LATEST="$(ls -td .agent-handoff/* | head -1)"
cat "$LATEST/codex-plugin-command.txt"

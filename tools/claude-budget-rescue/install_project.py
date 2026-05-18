#!/usr/bin/env python3
"""
Install Claude Budget Rescue into the current project's .claude settings.

By default this script prints the planned settings changes. Pass --apply to write:
  - .claude/budget-rescue.json
  - .claude/settings.local.json statusLine + hooks

It never edits ~/.claude/settings.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


PLAN_CHOICES = [
    "pro",
    "max_5x",
    "max_20x",
    "team_standard",
    "team_premium",
    "enterprise_usage_based",
    "enterprise_seat_premium",
    "custom",
]


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def dump_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def hook_entry(command: str) -> Dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 30,
            }
        ]
    }


def contains_command(entries: List[Dict[str, Any]], command: str) -> bool:
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("command") == command:
                return True
    return False


def merge_hook(settings: Dict[str, Any], event: str, command: str) -> None:
    hooks = settings.setdefault("hooks", {})
    entries = hooks.setdefault(event, [])
    if not contains_command(entries, command):
        entries.append(hook_entry(command))


def build_settings(settings: Dict[str, Any], root: Path, launch_codex: bool) -> Dict[str, Any]:
    tool = root / "tools" / "claude-budget-rescue" / "budget_rescue.py"
    command = f"python3 {tool} hook"
    status_command = f"python3 {tool} statusline"

    merged = json.loads(json.dumps(settings))
    merged["statusLine"] = {
        "type": "command",
        "command": status_command,
        "refreshInterval": 5,
    }
    for event in ["Stop", "UserPromptSubmit", "StopFailure"]:
        merge_hook(merged, event, command)
    return merged


def apply_optional_float(cfg: Dict[str, Any], key: str, value: Optional[float]) -> None:
    if value is not None:
        cfg[key] = value


def build_config(root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    example = root / "tools" / "claude-budget-rescue" / "config.example.json"
    cfg = json.loads(example.read_text())
    cfg["launch_codex"] = args.launch_codex
    cfg["plugin_companion"] = None
    if args.plan:
        cfg["subscription_plan"] = args.plan
    apply_optional_float(cfg, "agent_sdk_credit_usd", args.credit_usd)
    apply_optional_float(cfg, "automation_credit_remaining_usd", args.remaining_usd)
    apply_optional_float(cfg, "automation_credit_spent_usd", args.spent_usd)
    apply_optional_float(cfg, "automation_budget_usd", args.automation_budget_usd)
    apply_optional_float(cfg, "automation_soft_pct", args.automation_soft_pct)
    if args.automation_model:
        cfg["automation_model"] = args.automation_model
    if args.codex_model:
        cfg["codex_model"] = args.codex_model
    if args.codex_effort:
        cfg["codex_effort"] = args.codex_effort
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write settings instead of printing a dry run")
    parser.add_argument("--launch-codex", action="store_true", help="Enable automatic background Codex launch")
    parser.add_argument("--plan", choices=PLAN_CHOICES, help="Subscription plan profile for Agent SDK credit defaults")
    parser.add_argument("--credit-usd", type=float, help="Override monthly Agent SDK credit in USD")
    parser.add_argument("--remaining-usd", type=float, help="Set known remaining automation credit in USD")
    parser.add_argument("--spent-usd", type=float, help="Set already-spent automation credit in USD")
    parser.add_argument("--automation-budget-usd", type=float, help="Hard override for the automation budget")
    parser.add_argument("--automation-soft-pct", type=float, help="Handoff threshold as a percent of automation credit")
    parser.add_argument("--automation-model", help="Model price key used for cost estimates")
    parser.add_argument("--codex-model", help="Codex model used for rescue jobs")
    parser.add_argument("--codex-effort", help="Codex reasoning effort used for rescue jobs")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root")
    args = parser.parse_args()

    root = args.root.resolve()
    settings_path = root / ".claude" / "settings.local.json"
    config_path = root / ".claude" / "budget-rescue.json"

    current_settings = load_json(settings_path)
    next_settings = build_settings(current_settings, root, args.launch_codex)
    next_config = build_config(root, args)

    if not args.apply:
        print("DRY RUN. Pass --apply to write these files.\n")
        print(f"--- {settings_path}")
        print(json.dumps(next_settings, indent=2))
        print(f"\n--- {config_path}")
        print(json.dumps(next_config, indent=2))
        return 0

    dump_json(settings_path, next_settings)
    dump_json(config_path, next_config)
    print(f"Wrote {settings_path}")
    print(f"Wrote {config_path}")
    print("Restart Claude Code or run /reload-plugins after installing plugin changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

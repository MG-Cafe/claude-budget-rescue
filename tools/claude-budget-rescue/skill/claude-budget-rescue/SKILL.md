---
name: claude-budget-rescue
description: Budget-aware Claude Code to Codex handoff. Use this whenever the user mentions Claude Code limits, usage limits, rate limits, Agent SDK credits, claude -p automation budget, Codex rescue, /codex:rescue, handoff folders, or continuing work in Codex before Claude hits a limit.
---

# Claude Budget Rescue

Use this skill to keep long Claude Code work from dying when the session, context, or automation budget is close to a limit.

The core idea:

1. Watch Claude Code usage using the status line data.
2. Trigger a hook when a soft limit is crossed.
3. Create a `.agent-handoff/<timestamp>/` rescue package.
4. Either print the `/codex:rescue` command or launch Codex through the installed `openai/codex-plugin-cc` companion runtime.

This does not bypass Anthropic limits. It preserves work and hands it to Codex before the limit breaks the workflow.

## Commands

From the repo root:

```bash
python3 tools/claude-budget-rescue/budget_rescue.py statusline
python3 tools/claude-budget-rescue/budget_rescue.py hook
python3 tools/claude-budget-rescue/budget_rescue.py handoff --reason "manual rescue"
python3 tools/claude-budget-rescue/budget_rescue.py estimate --plan pro --model claude-sonnet-4.6 --input-tokens 50000 --output-tokens 15000
python3 tools/claude-budget-rescue/budget_rescue.py automation --dry-run --plan pro --spent-usd 4.25 "fix the failing tests"
```

## Budget Config

The user can choose a plan profile:

- `pro`: $20 Agent SDK monthly credit
- `max_5x`: $100
- `max_20x`: $200
- `team_standard`: $20
- `team_premium`: $100
- `enterprise_usage_based`: $20
- `enterprise_seat_premium`: $200
- `custom`: $20 unless overridden

If the user knows they already spent part of the automation credit, set `automation_credit_spent_usd`.
If they know the exact remaining balance, set `automation_credit_remaining_usd`.

The active automation budget is chosen in this order:

1. command line `--budget-usd`
2. config `automation_budget_usd`
3. command line or config remaining credit
4. plan credit minus spent credit

## Accuracy

Do not present this as a limit bypass or a live Anthropic billing checker.

The tool can calculate budgets from config values, create handoff folders, and prepare Codex commands. It cannot know the user's real remaining Agent SDK credit unless the user supplies `--remaining-usd`, `--spent-usd`, or config values.

Automatic Codex launch requires Claude Code, the Codex plugin, local Codex authentication, and the plugin companion script.

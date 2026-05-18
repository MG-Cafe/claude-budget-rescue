# Claude Budget Rescue

Budget-aware handoff tooling for Claude Code, `claude -p`, and the `openai/codex-plugin-cc` plugin.

It does not bypass Anthropic limits. It watches soft limits, creates a complete `.agent-handoff/...` package, and can prepare or launch a Codex rescue job when Claude work is close to stopping.

## Requirements

- Python 3
- Claude Code
- Optional: `openai/codex-plugin-cc` for `/codex:rescue` handoff commands
- Optional: local Codex authentication for automatic background launch

## Install

Preview the project-level Claude settings change:

```bash
python3 tools/claude-budget-rescue/install_project.py
```

Apply it:

```bash
python3 tools/claude-budget-rescue/install_project.py --apply --plan pro
```

Common plan values:

```text
pro                  $20 Agent SDK credit
max_5x               $100 Agent SDK credit
max_20x              $200 Agent SDK credit
team_standard        $20 Agent SDK credit
team_premium         $100 Agent SDK credit
enterprise_usage_based       $20 Agent SDK credit
enterprise_seat_premium      $200 Agent SDK credit
```

If you know your remaining automation credit:

```bash
python3 tools/claude-budget-rescue/install_project.py --apply --plan pro --remaining-usd 7.50
```

If you know how much you already spent:

```bash
python3 tools/claude-budget-rescue/install_project.py --apply --plan pro --spent-usd 12.50
```

To allow automatic Codex launch:

```bash
python3 tools/claude-budget-rescue/install_project.py \
  --apply \
  --plan pro \
  --launch-codex \
  --codex-model gpt-5.4-mini \
  --codex-effort medium
```

Restart Claude Code after installing.

## Configure

The installer writes:

```text
.claude/settings.local.json
.claude/budget-rescue.json
```

Most users only need these fields in `.claude/budget-rescue.json`:

```json
{
  "subscription_plan": "pro",
  "automation_credit_remaining_usd": null,
  "automation_credit_spent_usd": 0.0,
  "automation_soft_pct": 80,
  "automation_model": "claude-sonnet-4.6",
  "five_hour_soft_pct": 85,
  "seven_day_soft_pct": 85,
  "context_soft_pct": 88,
  "launch_codex": false,
  "codex_model": "gpt-5.4-mini",
  "codex_effort": "medium"
}
```

Budget selection order:

1. command line `--budget-usd`
2. config `automation_budget_usd`
3. command line/config remaining credit
4. plan credit minus spent credit

## Use

Estimate a run:

```bash
python3 tools/claude-budget-rescue/budget_rescue.py estimate \
  --plan pro \
  --model claude-sonnet-4.6 \
  --input-tokens 50000 \
  --output-tokens 15000
```

Run `claude -p` with a soft budget:

```bash
python3 tools/claude-budget-rescue/budget_rescue.py automation \
  --plan pro \
  --spent-usd 4.25 \
  "fix the failing checkout test"
```

The wrapper streams Claude's progress live in the terminal and also captures the output into the handoff package if a rescue is needed.

Demo an automatic post-budget handoff without spending Claude credit:

```bash
python3 tools/claude-budget-rescue/budget_rescue.py automation \
  --plan pro \
  --spent-usd 4.25 \
  --simulate-budget-exit \
  --launch-codex \
  --codex-model gpt-5.4-mini \
  --codex-effort medium \
  "fix the failing checkout test"
```

This creates `.agent-handoff/...`, tries to launch Codex through the Codex plugin companion, and prints whether Codex is now handling the rescue. If the plugin companion is not ready, it falls back to a background `codex exec` run. If Codex cannot launch automatically at all, open the generated `codex-plugin-command.txt` and paste it into Claude Code.

After a successful automatic launch, check the background Codex job from the same terminal:

```bash
bash "$(ls -td .agent-handoff/* | head -1)/codex-status-command.sh"
```

This is more reliable for demos than opening a fresh Claude Code session, because a new session may not list older background plugin jobs with `/codex:status`.
The same status script also works when the tool used the `codex exec` fallback.

Create a manual handoff:

```bash
python3 tools/claude-budget-rescue/budget_rescue.py handoff --reason "continue this in Codex"
```

Each handoff contains:

```text
handoff.md
original-task.txt
codex-prompt.txt
codex-plugin-command.txt
codex-companion-command.sh
codex-status-command.sh
codex-exec-command.sh
codex-exec-output.log
codex-exec-final.txt
status.json
hook-input.json
transcript-tail.md
git-status.txt
diff.patch
staged.diff.patch
```

## Pricing Defaults

Pricing defaults were checked on 2026-05-17. They are editable in `config.example.json`.

Included model prices:

```text
claude-opus-4.7      $5 input / $25 output per MTok
claude-opus-4.6      $5 input / $25 output per MTok
claude-opus-4.5      $5 input / $25 output per MTok
claude-opus-4.1      $15 input / $75 output per MTok
claude-sonnet-4.6    $3 input / $15 output per MTok
claude-sonnet-4.5    $3 input / $15 output per MTok
claude-sonnet-4      $3 input / $15 output per MTok
claude-haiku-4.5     $1 input / $5 output per MTok
claude-haiku-3.5     $0.80 input / $4 output per MTok
```

Sources:

- [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [Extra usage for paid Claude plans](https://support.claude.com/en/articles/12429409-manage-extra-usage-for-paid-claude-plans)

## Accuracy

The tests verify this repo's budget math, pricing lookup, handoff creation, and hook response shape. They do not verify your live Anthropic billing balance.

The tool cannot know your real remaining Agent SDK credit unless you enter it with `--remaining-usd`, `--spent-usd`, or `.claude/budget-rescue.json`.

Automatic Codex launch requires Claude Code, the Codex plugin, Codex authentication, and the plugin companion script to be available locally.

For video demos, prefer `--simulate-budget-exit` first. It exercises the handoff and Codex-launch path without burning real Agent SDK credit.

## Test

```bash
python3 -m py_compile tools/claude-budget-rescue/budget_rescue.py tools/claude-budget-rescue/install_project.py
python3 -m unittest discover tools/claude-budget-rescue/tests -v
```

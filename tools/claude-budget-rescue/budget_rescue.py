#!/usr/bin/env python3
"""
Budget-aware rescue layer for Claude Code.

This tool is intentionally repo-local and reversible. It can be used as:

  python3 budget_rescue.py statusline < claude-status.json
  python3 budget_rescue.py hook < claude-hook-input.json
  python3 budget_rescue.py handoff --reason "manual rescue"
  python3 budget_rescue.py automation --dry-run --budget-usd 20 "fix the tests"

The Codex rescue plugin already provides /codex:rescue. This script adds the
missing budget-aware trigger and handoff package around it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import pty
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PLAN_AGENT_SDK_CREDITS_USD: Dict[str, float] = {
    "pro": 20.0,
    "max_5x": 100.0,
    "max_20x": 200.0,
    "team_standard": 20.0,
    "team_premium": 100.0,
    "enterprise_usage_based": 20.0,
    "enterprise_seat_premium": 200.0,
    "custom": 20.0,
}


MODEL_PRICES_USD_PER_MTOK: Dict[str, Dict[str, float]] = {
    "claude-opus-4.7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "claude-opus-4.6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "claude-opus-4.5": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "claude-opus-4.1": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
        "cache_read": 1.50,
    },
    "claude-sonnet-4.6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "claude-sonnet-4.5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "claude-haiku-4.5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
    },
    "claude-haiku-3.5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_write_1h": 1.60,
        "cache_read": 0.08,
    },
}


DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "subscription_plan": "pro",
    "plan_agent_sdk_credits_usd": PLAN_AGENT_SDK_CREDITS_USD,
    "agent_sdk_credit_usd": None,
    "automation_credit_remaining_usd": None,
    "automation_credit_spent_usd": 0.0,
    "five_hour_soft_pct": 85,
    "seven_day_soft_pct": 85,
    "context_soft_pct": 88,
    "automation_budget_usd": None,
    "automation_soft_pct": 80,
    "automation_model": "claude-sonnet-4.6",
    "cost_soft_usd": None,
    "handoff_dir": ".agent-handoff",
    "state_dir": "~/.claude/budget-rescue",
    "launch_codex": False,
    "codex_model": "gpt-5.4-mini",
    "codex_effort": "medium",
    "codex_write": True,
    "once_per_session": True,
    "plugin_companion": None,
    "pricing_last_checked": "2026-05-17",
    "pricing_sources": [
        "https://platform.claude.com/docs/en/about-claude/pricing",
        "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
        "https://support.claude.com/en/articles/12429409-manage-extra-usage-for-paid-claude-plans",
    ],
    "model_prices_usd_per_mtok": MODEL_PRICES_USD_PER_MTOK,
    "pricing_multiplier": 1.0,
}


BLOCKING_EVENTS = {"UserPromptSubmit", "PostToolBatch", "Stop"}
NO_DECISION_EVENTS = {"StopFailure", "Notification", "SessionEnd"}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def load_json_file(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json_stdin() -> Dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Expected JSON on stdin: {exc}") from exc


def find_project_root(start: Path) -> Path:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists() or (parent / ".claude").exists():
            return parent
    return current


def load_config(cwd: Optional[Path] = None, explicit: Optional[Path] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    candidates: List[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())
    if cwd:
        root = find_project_root(cwd)
        candidates.append(root / ".claude" / "budget-rescue.json")
    candidates.append(Path("~/.claude/budget-rescue/config.json").expanduser())

    for path in candidates:
        if path.exists():
            loaded = load_json_file(path, {})
            if not isinstance(loaded, dict):
                raise SystemExit(f"Config must be a JSON object: {path}")
            cfg.update(loaded)

    if os.environ.get("CLAUDE_BUDGET_RESCUE_LAUNCH_CODEX") == "1":
        cfg["launch_codex"] = True
    if os.environ.get("CLAUDE_BUDGET_RESCUE_DISABLED") == "1":
        cfg["enabled"] = False
    return cfg


def expand_state_dir(cfg: Dict[str, Any]) -> Path:
    return Path(str(cfg.get("state_dir") or DEFAULT_CONFIG["state_dir"])).expanduser()


def pct(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money(value: Any) -> Optional[float]:
    parsed = pct(value)
    if parsed is None:
        return None
    return max(0.0, parsed)


def plan_key(raw: Any) -> str:
    return str(raw or "pro").strip().lower().replace("-", "_").replace(" ", "_")


def resolve_plan_credit_usd(cfg: Dict[str, Any], plan_override: Optional[str] = None, credit_override: Optional[float] = None) -> float:
    if credit_override is not None:
        return max(0.0, float(credit_override))

    explicit = money(cfg.get("agent_sdk_credit_usd"))
    if explicit is not None:
        return explicit

    plan = plan_key(plan_override or cfg.get("subscription_plan"))
    plan_table = cfg.get("plan_agent_sdk_credits_usd") or PLAN_AGENT_SDK_CREDITS_USD
    if not isinstance(plan_table, dict):
        plan_table = PLAN_AGENT_SDK_CREDITS_USD
    return float(plan_table.get(plan, plan_table.get("pro", 20.0)))


def resolve_automation_budget_usd(
    cfg: Dict[str, Any],
    plan_override: Optional[str] = None,
    budget_override: Optional[float] = None,
    credit_override: Optional[float] = None,
    remaining_override: Optional[float] = None,
    spent_override: Optional[float] = None,
) -> Tuple[float, str]:
    if budget_override is not None:
        return max(0.0, float(budget_override)), "command line --budget-usd"

    cfg_budget = money(cfg.get("automation_budget_usd"))
    if cfg_budget is not None:
        return cfg_budget, "config automation_budget_usd"

    if remaining_override is not None:
        return max(0.0, float(remaining_override)), "command line --remaining-usd"

    cfg_remaining = money(cfg.get("automation_credit_remaining_usd"))
    if cfg_remaining is not None:
        return cfg_remaining, "config automation_credit_remaining_usd"

    monthly_credit = resolve_plan_credit_usd(cfg, plan_override, credit_override)
    spent = spent_override if spent_override is not None else money(cfg.get("automation_credit_spent_usd"))
    spent = float(spent or 0.0)
    return max(0.0, monthly_credit - spent), "plan credit minus spent"


def soft_budget_usd(budget_usd: float, soft_pct_value: Any) -> float:
    return round(max(0.0, float(budget_usd)) * float(soft_pct_value) / 100.0, 2)


def model_prices(cfg: Dict[str, Any], model: str) -> Dict[str, float]:
    table = cfg.get("model_prices_usd_per_mtok") or MODEL_PRICES_USD_PER_MTOK
    if not isinstance(table, dict):
        table = MODEL_PRICES_USD_PER_MTOK
    price = table.get(model)
    if not isinstance(price, dict):
        available = ", ".join(sorted(table))
        raise SystemExit(f"Unknown model price key: {model}. Available: {available}")
    return {key: float(value) for key, value in price.items()}


def estimate_cost_usd(
    cfg: Dict[str, Any],
    model: str,
    input_tokens: float = 0.0,
    output_tokens: float = 0.0,
    cache_write_5m_tokens: float = 0.0,
    cache_write_1h_tokens: float = 0.0,
    cache_read_tokens: float = 0.0,
) -> float:
    price = model_prices(cfg, model)
    multiplier = float(cfg.get("pricing_multiplier") or 1.0)
    total = (
        input_tokens * price.get("input", 0.0)
        + output_tokens * price.get("output", 0.0)
        + cache_write_5m_tokens * price.get("cache_write_5m", 0.0)
        + cache_write_1h_tokens * price.get("cache_write_1h", 0.0)
        + cache_read_tokens * price.get("cache_read", 0.0)
    )
    return round((total / 1_000_000.0) * multiplier, 6)


def budget_summary_lines(cfg: Dict[str, Any], plan_override: Optional[str] = None) -> List[str]:
    plan = plan_key(plan_override or cfg.get("subscription_plan"))
    monthly_credit = resolve_plan_credit_usd(cfg, plan)
    active_budget, source = resolve_automation_budget_usd(cfg, plan_override=plan)
    soft_pct_value = float(cfg.get("automation_soft_pct") or 80)
    soft_budget = soft_budget_usd(active_budget, soft_pct_value)
    model = str(cfg.get("automation_model") or "claude-sonnet-4.6")
    price = model_prices(cfg, model)
    return [
        f"Subscription plan: {plan}",
        f"Agent SDK monthly credit: ${monthly_credit:.2f}",
        f"Automation credit available: ${active_budget:.2f} ({source})",
        f"Automation soft stop: {soft_pct_value:g}% = ${soft_budget:.2f}",
        f"Automation pricing model: {model} (${price['input']:g}/MTok input, ${price['output']:g}/MTok output)",
        f"Pricing defaults checked: {cfg.get('pricing_last_checked')}",
    ]


def nested(data: Dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for key in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def build_status_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": data.get("session_id"),
        "transcript_path": data.get("transcript_path"),
        "cwd": data.get("cwd") or nested(data, "workspace.current_dir"),
        "model": nested(data, "model.display_name") or nested(data, "model.id"),
        "five_hour_used_pct": pct(nested(data, "rate_limits.five_hour.used_percentage")),
        "seven_day_used_pct": pct(nested(data, "rate_limits.seven_day.used_percentage")),
        "context_used_pct": pct(nested(data, "context_window.used_percentage")),
        "cost_usd": pct(nested(data, "cost.total_cost_usd")),
        "total_input_tokens": nested(data, "context_window.total_input_tokens"),
        "total_output_tokens": nested(data, "context_window.total_output_tokens"),
        "raw": data,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def status_state_path(cfg: Dict[str, Any], session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return expand_state_dir(cfg) / f"{safe}.json"


def marker_path(cfg: Dict[str, Any], session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return expand_state_dir(cfg) / f"{safe}.rescued"


def save_status(cfg: Dict[str, Any], status: Dict[str, Any]) -> None:
    session_id = status.get("session_id") or "unknown"
    dump_json(status_state_path(cfg, session_id), status)


def load_status_for_hook(cfg: Dict[str, Any], hook_input: Dict[str, Any]) -> Dict[str, Any]:
    session_id = hook_input.get("session_id") or "unknown"
    saved = load_json_file(status_state_path(cfg, session_id), {})
    if not isinstance(saved, dict):
        saved = {}
    if not saved:
        saved = {
            "session_id": session_id,
            "transcript_path": hook_input.get("transcript_path"),
            "cwd": hook_input.get("cwd"),
            "raw": {},
        }
    saved.setdefault("session_id", session_id)
    saved.setdefault("transcript_path", hook_input.get("transcript_path"))
    saved.setdefault("cwd", hook_input.get("cwd"))
    return saved


def rescue_reasons(status: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    if not cfg.get("enabled", True):
        return []

    reasons: List[str] = []
    checks: List[Tuple[str, Optional[float], Optional[float]]] = [
        ("5h Claude limit", pct(status.get("five_hour_used_pct")), pct(cfg.get("five_hour_soft_pct"))),
        ("7d Claude limit", pct(status.get("seven_day_used_pct")), pct(cfg.get("seven_day_soft_pct"))),
        ("context window", pct(status.get("context_used_pct")), pct(cfg.get("context_soft_pct"))),
    ]

    cost_limit = pct(cfg.get("cost_soft_usd"))
    if cost_limit is not None:
        checks.append(("session cost", pct(status.get("cost_usd")), cost_limit))

    for label, used, limit in checks:
        if used is not None and limit is not None and used >= limit:
            suffix = "%" if "cost" not in label else " USD"
            reasons.append(f"{label} is {used:g}{suffix} >= soft limit {limit:g}{suffix}")
    return reasons


def format_statusline(status: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    parts = []
    model = status.get("model")
    if model:
        parts.append(str(model))
    for label, key in [("5h", "five_hour_used_pct"), ("7d", "seven_day_used_pct"), ("ctx", "context_used_pct")]:
        value = pct(status.get(key))
        parts.append(f"{label} {value:.0f}%" if value is not None else f"{label} n/a")
    cost_value = pct(status.get("cost_usd"))
    if cost_value is not None:
        parts.append(f"${cost_value:.2f}")
    reasons = rescue_reasons(status, cfg)
    if reasons:
        parts.append("RESCUE READY")
    else:
        parts.append(f"rescue at {cfg.get('five_hour_soft_pct')}%")
    return " | ".join(parts)


def run_cmd(args: List[str], cwd: Path, timeout: int = 10) -> str:
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[command failed: {' '.join(args)}: {exc}]"


def write_stdout_bytes(data: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.flush()
        return
    sys.stdout.write(data.decode(errors="replace"))
    sys.stdout.flush()


def run_streaming_process(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a command in a pseudo-terminal so progress is visible and captured."""
    master_fd, slave_fd = pty.openpty()
    chunks: List[bytes] = []
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        while True:
            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            chunks.append(data)
            write_stdout_bytes(data)
        returncode = proc.wait()
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    return subprocess.CompletedProcess(
        args,
        returncode,
        stdout=b"".join(chunks).decode(errors="replace"),
    )


def git_snapshot(cwd: Path) -> Dict[str, str]:
    return {
        "root": run_cmd(["git", "rev-parse", "--show-toplevel"], cwd),
        "branch": run_cmd(["git", "branch", "--show-current"], cwd),
        "status": run_cmd(["git", "status", "--short"], cwd),
        "diff_stat": run_cmd(["git", "diff", "--stat"], cwd, timeout=20),
        "staged_diff_stat": run_cmd(["git", "diff", "--cached", "--stat"], cwd, timeout=20),
        "recent_commits": run_cmd(["git", "log", "--oneline", "-5"], cwd),
    }


def read_transcript_tail(path: Optional[str], max_lines: int = 40) -> str:
    if not path:
        return ""
    transcript = Path(path).expanduser()
    if not transcript.exists():
        return ""
    lines = transcript.read_text(errors="replace").splitlines()[-max_lines:]
    return "\n".join(lines)


def extract_text_fragments(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from extract_text_fragments(item)
    elif isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            yield obj["text"]
        for key in ("content", "message", "messages", "text", "last_assistant_message"):
            if key in obj:
                yield from extract_text_fragments(obj[key])


def summarize_transcript_tail(raw_tail: str, max_chars: int = 6000) -> str:
    snippets: List[str] = []
    for line in raw_tail.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("type") or nested(obj, "message.role") or obj.get("role") or "entry"
        text = " ".join(t.strip() for t in extract_text_fragments(obj) if t.strip())
        if text:
            snippets.append(f"{role}: {text[:1000]}")
    text = "\n\n".join(snippets[-12:])
    return text[-max_chars:]


def make_codex_prompt(handoff_dir: Path, reasons: List[str], cwd: Path, task_prompt: str = "") -> str:
    reason_text = "\n".join(f"- {r}" for r in reasons) or "- Manual rescue requested"
    task_text = task_prompt.strip() or "No original task prompt was captured. Infer the task from the handoff files."
    return f"""Continue this work in Codex from the rescue package below.

Rescue package: {handoff_dir}
Working directory: {cwd}

Original task:
{task_text}

Why this was handed off:
{reason_text}

Read these files first:
1. {handoff_dir / "handoff.md"}
2. {handoff_dir / "git-status.txt"}
3. {handoff_dir / "diff.patch"}
4. {handoff_dir / "transcript-tail.md"}

Goal:
- Continue the original task above.
- Recover any extra task context from the handoff package.
- Inspect the current repo state.
- Continue with the smallest safe next step.
- Run relevant verification before changing broad areas.
- Report exactly what changed and how to continue.
"""


def shell_command(bits: Iterable[Any]) -> str:
    return " ".join(shlex.quote(str(bit)) for bit in bits)


def discover_companion(cfg: Dict[str, Any]) -> Optional[Path]:
    explicit = cfg.get("plugin_companion")
    if explicit:
        path = Path(str(explicit)).expanduser()
        if path.exists():
            return path

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        candidate = Path(env_root) / "scripts" / "codex-companion.mjs"
        if candidate.exists():
            return candidate

    try:
        completed = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        plugins = json.loads(completed.stdout or "[]")
        for plugin in plugins:
            if plugin.get("id") == "codex@openai-codex":
                candidate = Path(plugin["installPath"]) / "scripts" / "codex-companion.mjs"
                if candidate.exists():
                    return candidate
    except Exception:
        pass

    fallback = Path("~/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/codex-companion.mjs").expanduser()
    return fallback if fallback.exists() else None


def extract_codex_job_id(output: str) -> Optional[str]:
    match = re.search(r"\b(task-[A-Za-z0-9][A-Za-z0-9-]*)\b", output or "")
    return match.group(1) if match else None


def launch_failed(output: str) -> bool:
    return output.startswith("Codex plugin companion not found") or output.startswith("Codex launch failed")


def codex_status_command(cfg: Dict[str, Any], job_id: Optional[str] = None) -> str:
    companion = discover_companion(cfg)
    if not companion:
        return ""
    bits = ["node", str(companion), "status"]
    if job_id:
        bits.append(job_id)
    bits.append("--all")
    return shell_command(bits)


def codex_exec_bits(cfg: Dict[str, Any], cwd: Path, prompt: str, output_file: Optional[Path] = None) -> List[str]:
    bits = ["codex", "exec", "--cd", str(cwd), "--sandbox", "workspace-write"]
    model = str(cfg.get("codex_model") or "").strip()
    effort = str(cfg.get("codex_effort") or "").strip()
    if model:
        bits += ["--model", model]
    if effort:
        bits += ["-c", f'model_reasoning_effort="{effort}"']
    if output_file:
        bits += ["--output-last-message", str(output_file)]
    bits.append(prompt)
    return bits


def write_shell_script(path: Path, command: str, fallback_message: str) -> None:
    body = command or f"echo {shlex.quote(fallback_message)}"
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n")
    try:
        path.chmod(0o755)
    except OSError:
        pass


def write_codex_exec_status_script(handoff_dir: Path, pid_file: Path, log_file: Path, result_file: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
PID_FILE={shlex.quote(str(pid_file))}
LOG_FILE={shlex.quote(str(log_file))}
RESULT_FILE={shlex.quote(str(result_file))}

echo "# Codex CLI Background Job"
if [ -s "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "- pid $PID | running"
  else
    echo "- pid $PID | finished"
  fi
else
  echo "- pid unknown"
fi
echo "Log: $LOG_FILE"
if [ -s "$LOG_FILE" ]; then
  echo
  tail -n 80 "$LOG_FILE"
else
  echo "Log is empty so far."
fi
if [ -s "$RESULT_FILE" ]; then
  echo
  echo "Final message:"
  cat "$RESULT_FILE"
fi
"""
    handoff_dir.joinpath("codex-status-command.sh").write_text(script)
    try:
        handoff_dir.joinpath("codex-status-command.sh").chmod(0o755)
    except OSError:
        pass


def companion_ready(companion: Path, cwd: Path) -> Tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["node", str(companion), "setup", "--json"],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return False, (completed.stdout or "Codex plugin setup check did not return JSON.").strip()

    if bool(payload.get("ready")):
        return True, "Codex plugin companion is ready."
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    detail = auth.get("detail") or payload.get("detail") or "Codex plugin companion is not ready."
    return False, str(detail)


def codex_commands(cfg: Dict[str, Any], handoff_dir: Path, cwd: Path) -> Dict[str, str]:
    prompt_file = handoff_dir / "codex-prompt.txt"
    model = str(cfg.get("codex_model") or "").strip()
    effort = str(cfg.get("codex_effort") or "").strip()

    slash_bits = ["/codex:rescue", "--background"]
    if model:
        slash_bits += ["--model", model]
    if effort:
        slash_bits += ["--effort", effort]
    slash_bits.append(f"continue from this handoff folder: {handoff_dir}")

    companion = discover_companion(cfg)
    node_cmd = ""
    status_cmd = ""
    if companion:
        bits = ["node", str(companion), "task", "--background"]
        if cfg.get("codex_write", True):
            bits.append("--write")
        if model:
            bits += ["--model", model]
        if effort:
            bits += ["--effort", effort]
        bits.append(prompt_file.read_text())
        node_cmd = shell_command(bits)
        status_cmd = shell_command(["node", str(companion), "status", "--all"])

    exec_bits = codex_exec_bits(cfg, cwd, prompt_file.read_text())

    return {
        "claude_slash_command": " ".join(slash_bits),
        "plugin_companion_command": node_cmd,
        "codex_status_command": status_cmd,
        "codex_exec_command": shell_command(exec_bits),
    }


def launch_codex(cfg: Dict[str, Any], handoff_dir: Path, cwd: Path) -> str:
    companion = discover_companion(cfg)
    plugin_not_ready = ""

    prompt = (handoff_dir / "codex-prompt.txt").read_text()
    if companion:
        ready, detail = companion_ready(companion, cwd)
        if ready:
            args = ["node", str(companion), "task", "--background"]
            if cfg.get("codex_write", True):
                args.append("--write")
            model = str(cfg.get("codex_model") or "").strip()
            effort = str(cfg.get("codex_effort") or "").strip()
            if model:
                args += ["--model", model]
            if effort:
                args += ["--effort", effort]
            args.append(prompt)

            try:
                completed = subprocess.run(
                    args,
                    cwd=str(cwd),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                    check=False,
                )
                if completed.returncode == 0:
                    return completed.stdout.strip()
                plugin_not_ready = completed.stdout.strip() or f"Codex plugin companion exited {completed.returncode}."
            except (OSError, subprocess.TimeoutExpired) as exc:
                plugin_not_ready = str(exc)
        else:
            plugin_not_ready = detail
    else:
        plugin_not_ready = "Codex plugin companion not found. Run /codex:setup inside Claude Code."

    return launch_codex_exec_background(cfg, handoff_dir, cwd, prompt, plugin_not_ready)


def launch_codex_exec_background(cfg: Dict[str, Any], handoff_dir: Path, cwd: Path, prompt: str, plugin_detail: str) -> str:
    log_file = handoff_dir / "codex-exec-output.log"
    result_file = handoff_dir / "codex-exec-final.txt"
    pid_file = handoff_dir / "codex-exec.pid"
    args = codex_exec_bits(cfg, cwd, prompt, result_file)
    write_shell_script(
        handoff_dir / "codex-exec-command.sh",
        shell_command(args),
        "Codex CLI command could not be created.",
    )
    try:
        log_handle = log_file.open("w")
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
    except OSError as exc:
        return f"Codex launch failed: {exc}"

    pid_file.write_text(f"{proc.pid}\n")
    write_codex_exec_status_script(handoff_dir, pid_file, log_file, result_file)
    return "\n".join(
        [
            f"Codex plugin companion is not ready: {plugin_detail}",
            "Falling back to Codex CLI background launch.",
            f"Codex CLI background task started as pid {proc.pid}.",
            f"Log: {log_file}",
            f"Result: {result_file}",
            f"Status command: bash {handoff_dir / 'codex-status-command.sh'}",
        ]
    )


def print_automation_handoff(
    cfg: Dict[str, Any],
    handoff_dir: Path,
    cwd: Path,
    launch_requested: bool,
    prefix: str = "Budget rescue handoff",
) -> None:
    print(f"\n{prefix}: {handoff_dir}")
    command_file = handoff_dir / "codex-plugin-command.txt"
    if not launch_requested:
        print("Codex was not launched automatically.")
        print(f"Open the command file: {command_file}")
        return

    launch_output = launch_codex(cfg, handoff_dir, cwd)
    (handoff_dir / "codex-launch-output.txt").write_text(launch_output + "\n")
    if launch_failed(launch_output):
        print(f"Codex automatic launch failed: {launch_output}")
        print(f"Fallback command file: {command_file}")
        return

    job_id = extract_codex_job_id(launch_output)
    status_script = handoff_dir / "codex-status-command.sh"
    if job_id or not status_script.exists():
        status_command = codex_status_command(cfg, job_id)
        write_shell_script(
            status_script,
            status_command,
            "Codex plugin companion not found. Run /codex:setup inside Claude Code.",
        )
    if job_id:
        (handoff_dir / "codex-job-id.txt").write_text(job_id + "\n")

    print("Codex launch requested.")
    print("From now, Codex is handling this rescue in the background.")
    if launch_output:
        print(launch_output)
    if job_id:
        print(f"Codex job id: {job_id}")
    print(f"Check status from this terminal: bash {handoff_dir / 'codex-status-command.sh'}")
    print("Fresh Claude Code sessions may not list older background jobs with /codex:status.")


def create_handoff(
    cfg: Dict[str, Any],
    cwd: Path,
    reasons: List[str],
    status: Optional[Dict[str, Any]] = None,
    hook_input: Optional[Dict[str, Any]] = None,
) -> Path:
    status = status or {}
    hook_input = hook_input or {}
    session_id = str(status.get("session_id") or hook_input.get("session_id") or "manual")
    short_session = re.sub(r"[^A-Za-z0-9]", "", session_id)[:8] or "manual"
    base = cwd / str(cfg.get("handoff_dir") or DEFAULT_CONFIG["handoff_dir"])
    handoff_dir = base / f"{now_stamp()}-{short_session}"
    handoff_dir.mkdir(parents=True, exist_ok=False)

    raw_tail = read_transcript_tail(status.get("transcript_path") or hook_input.get("transcript_path"))
    summary_tail = summarize_transcript_tail(raw_tail)
    git = git_snapshot(cwd)
    task_prompt = str(status.get("task_prompt") or hook_input.get("task_prompt") or "").strip()

    dump_json(handoff_dir / "status.json", status)
    dump_json(handoff_dir / "hook-input.json", hook_input)
    if task_prompt:
        (handoff_dir / "original-task.txt").write_text(task_prompt + "\n")
    (handoff_dir / "transcript-tail.jsonl").write_text(raw_tail + ("\n" if raw_tail else ""))
    (handoff_dir / "transcript-tail.md").write_text(summary_tail + ("\n" if summary_tail else ""))
    (handoff_dir / "git-status.txt").write_text(
        "\n".join(
            [
                f"Root: {git['root']}",
                f"Branch: {git['branch']}",
                "",
                "Status:",
                git["status"] or "(clean)",
                "",
                "Diff stat:",
                git["diff_stat"] or "(none)",
                "",
                "Staged diff stat:",
                git["staged_diff_stat"] or "(none)",
                "",
                "Recent commits:",
                git["recent_commits"] or "(none)",
            ]
        )
        + "\n"
    )
    diff = run_cmd(["git", "diff", "--binary"], cwd, timeout=30)
    staged_diff = run_cmd(["git", "diff", "--cached", "--binary"], cwd, timeout=30)
    (handoff_dir / "diff.patch").write_text(diff + ("\n" if diff else ""))
    (handoff_dir / "staged.diff.patch").write_text(staged_diff + ("\n" if staged_diff else ""))

    prompt = make_codex_prompt(handoff_dir, reasons, cwd, task_prompt)
    (handoff_dir / "codex-prompt.txt").write_text(prompt)

    commands = codex_commands(cfg, handoff_dir, cwd)
    (handoff_dir / "codex-plugin-command.txt").write_text(commands["claude_slash_command"] + "\n")
    write_shell_script(
        handoff_dir / "codex-companion-command.sh",
        commands["plugin_companion_command"],
        "Codex plugin companion not found. Run /codex:setup inside Claude Code.",
    )
    write_shell_script(
        handoff_dir / "codex-status-command.sh",
        commands["codex_status_command"],
        "Codex plugin companion not found. Run /codex:setup inside Claude Code.",
    )
    write_shell_script(
        handoff_dir / "codex-exec-command.sh",
        commands["codex_exec_command"],
        "Codex CLI command could not be created.",
    )

    reason_text = "\n".join(f"- {r}" for r in reasons) or "- Manual rescue requested"
    budget_text = "\n".join(f"- {line}" for line in budget_summary_lines(cfg))
    handoff_md = f"""# Claude Budget Rescue Handoff

Created: {dt.datetime.now().isoformat(timespec="seconds")}
Working directory: {cwd}
Session: {session_id}

## Trigger
{reason_text}

## Original Task
{task_prompt or "(not captured)"}

## Budget Settings
{budget_text}

## What To Do In Codex
Use the prompt in `codex-prompt.txt`.

If you are still inside Claude Code, run:

```text
{commands["claude_slash_command"]}
```

If you want to launch Codex directly from a terminal, run:

```bash
./codex-companion-command.sh
```

## Files In This Package
- `status.json`: last statusline snapshot.
- `original-task.txt`: original task prompt, when captured.
- `hook-input.json`: hook event that triggered rescue.
- `transcript-tail.md`: readable tail of the Claude conversation.
- `transcript-tail.jsonl`: raw transcript tail.
- `git-status.txt`: branch, status, and diff stats.
- `diff.patch`: unstaged working tree patch.
- `staged.diff.patch`: staged patch.
- `codex-prompt.txt`: ready-to-send Codex prompt.
- `codex-status-command.sh`: terminal-safe Codex job status command.
"""
    (handoff_dir / "handoff.md").write_text(handoff_md)
    return handoff_dir


def hook_response(event: str, message: str) -> Dict[str, Any]:
    if event == "Stop":
        return {"continue": False, "stopReason": message, "systemMessage": message}
    if event in NO_DECISION_EVENTS:
        return {"systemMessage": message}
    if event in BLOCKING_EVENTS:
        return {"decision": "block", "reason": message, "systemMessage": message}
    return {"systemMessage": message}


def cmd_statusline(args: argparse.Namespace) -> int:
    data = read_json_stdin()
    cfg = load_config(Path(data.get("cwd") or os.getcwd()), args.config)
    status = build_status_snapshot(data)
    save_status(cfg, status)
    print(format_statusline(status, cfg))
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    hook_input = read_json_stdin()
    cwd = Path(hook_input.get("cwd") or os.getcwd()).resolve()
    cfg = load_config(cwd, args.config)
    event = hook_input.get("hook_event_name") or "Manual"
    status = load_status_for_hook(cfg, hook_input)
    reasons = rescue_reasons(status, cfg)

    if args.force and not reasons:
        reasons = ["Manual forced rescue for demo/testing"]

    if not reasons:
        if args.print_empty_json:
            print("{}")
        return 0

    session_id = str(status.get("session_id") or hook_input.get("session_id") or "unknown")
    marker = marker_path(cfg, session_id)
    if cfg.get("once_per_session", True) and marker.exists() and not args.force:
        print("{}")
        return 0

    handoff_dir = create_handoff(cfg, cwd, reasons, status, hook_input)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(handoff_dir) + "\n")

    launch_output = ""
    if bool(cfg.get("launch_codex")) and not args.dry_run:
        launch_output = launch_codex(cfg, handoff_dir, cwd)
        (handoff_dir / "codex-launch-output.txt").write_text(launch_output + "\n")
        if launch_output and not launch_failed(launch_output):
            job_id = extract_codex_job_id(launch_output)
            status_script = handoff_dir / "codex-status-command.sh"
            if job_id or not status_script.exists():
                status_command = codex_status_command(cfg, job_id)
                write_shell_script(
                    status_script,
                    status_command,
                    "Codex plugin companion not found. Run /codex:setup inside Claude Code.",
                )
            if job_id:
                (handoff_dir / "codex-job-id.txt").write_text(job_id + "\n")

    launch_succeeded = bool(launch_output) and not launch_failed(launch_output)
    launch_note = "Codex was launched in background." if launch_succeeded else "Codex was not launched automatically."
    follow_up = (
        f"Check status with: bash {handoff_dir / 'codex-status-command.sh'}."
        if launch_succeeded
        else f"Inspect {handoff_dir / 'codex-plugin-command.txt'}."
    )
    message = (
        "Claude Budget Rescue triggered. "
        f"Handoff folder: {handoff_dir}. {launch_note} "
        f"{follow_up}"
    )
    print(json.dumps(hook_response(event, message), indent=2))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd or os.getcwd()).resolve()
    cfg = load_config(cwd, args.config)
    status = load_json_file(Path(args.status_json).expanduser(), {}) if args.status_json else {}
    if args.transcript:
        status["transcript_path"] = str(Path(args.transcript).expanduser())
    reasons = args.reason or ["Manual handoff requested"]
    handoff_dir = create_handoff(cfg, cwd, reasons, status, {"hook_event_name": "Manual", "cwd": str(cwd)})
    print(str(handoff_dir))
    return 0


def cmd_automation(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd or os.getcwd()).resolve()
    cfg = load_config(cwd, args.config)
    if getattr(args, "codex_model", None):
        cfg["codex_model"] = args.codex_model
    if getattr(args, "codex_effort", None):
        cfg["codex_effort"] = args.codex_effort
    budget, budget_source = resolve_automation_budget_usd(
        cfg,
        plan_override=args.plan,
        budget_override=args.budget_usd,
        credit_override=args.credit_usd,
        remaining_override=args.remaining_usd,
        spent_override=args.spent_usd,
    )
    soft_pct = args.soft_pct if args.soft_pct is not None else float(cfg["automation_soft_pct"])
    soft_budget = soft_budget_usd(budget, soft_pct)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        raise SystemExit("automation requires a prompt")

    if soft_budget <= 0:
        reasons = [f"automation credit is exhausted ({budget_source})"]
        handoff_dir = create_handoff(
            cfg,
            cwd,
            reasons,
            {"session_id": "automation", "cwd": str(cwd), "task_prompt": prompt},
            {"hook_event_name": "AutomationBudgetEmpty", "cwd": str(cwd), "task_prompt": prompt},
        )
        print_automation_handoff(
            cfg,
            handoff_dir,
            cwd,
            bool(getattr(args, "launch_codex", False) or cfg.get("launch_codex")),
        )
        return 2

    claude_cmd = ["claude", "-p", "--max-budget-usd", f"{soft_budget:.2f}"]
    if args.max_turns:
        claude_cmd += ["--max-turns", str(args.max_turns)]
    claude_cmd.append(prompt)

    if args.dry_run:
        print(" ".join(shlex.quote(part) for part in claude_cmd))
        return 0

    if getattr(args, "simulate_budget_exit", False):
        completed = subprocess.CompletedProcess(
            claude_cmd,
            1,
            stdout=(
                "Simulated Claude budget exit for demo.\n"
                f"Claude command would have used --max-budget-usd {soft_budget:.2f}.\n"
            ),
        )
        sys.stdout.write(completed.stdout)
    else:
        completed = run_streaming_process(claude_cmd, cwd)
    if completed.returncode != 0:
        status = {
            "session_id": "automation",
            "cwd": str(cwd),
            "cost_usd": soft_budget,
            "budget_source": budget_source,
            "task_prompt": prompt,
            "transcript_path": None,
        }
        reasons = [f"claude -p exited {completed.returncode} after soft budget ${soft_budget:.2f}"]
        handoff_dir = create_handoff(
            cfg,
            cwd,
            reasons,
            status,
            {"hook_event_name": "AutomationFailure", "cwd": str(cwd), "task_prompt": prompt},
        )
        (handoff_dir / "automation-output.txt").write_text(completed.stdout)
        print_automation_handoff(
            cfg,
            handoff_dir,
            cwd,
            bool(getattr(args, "launch_codex", False) or cfg.get("launch_codex")),
        )
    return completed.returncode


def cmd_estimate(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd or os.getcwd()).resolve()
    cfg = load_config(cwd, args.config)
    model = args.model or str(cfg.get("automation_model") or "claude-sonnet-4.6")
    budget, budget_source = resolve_automation_budget_usd(
        cfg,
        plan_override=args.plan,
        budget_override=args.budget_usd,
        credit_override=args.credit_usd,
        remaining_override=args.remaining_usd,
        spent_override=args.spent_usd,
    )
    soft_pct = args.soft_pct if args.soft_pct is not None else float(cfg["automation_soft_pct"])
    soft_budget = soft_budget_usd(budget, soft_pct)
    estimated = estimate_cost_usd(
        cfg,
        model,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cache_write_5m_tokens=args.cache_write_5m_tokens,
        cache_write_1h_tokens=args.cache_write_1h_tokens,
        cache_read_tokens=args.cache_read_tokens,
    )
    price = model_prices(cfg, model)
    print(f"Model: {model}")
    print(f"Rates: ${price['input']:g}/MTok input, ${price['output']:g}/MTok output")
    print(f"Automation budget: ${budget:.2f} ({budget_source})")
    print(f"Soft handoff budget: ${soft_budget:.2f} ({float(soft_pct):g}%)")
    print(f"Estimated run cost: ${estimated:.6f}")
    print(f"Estimated remaining after run: ${max(0.0, budget - estimated):.6f}")
    print(f"Handoff before/at: ${soft_budget:.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Budget-aware Claude Code to Codex rescue helper")
    parser.add_argument("--config", type=Path, help="Path to budget-rescue config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    statusline = sub.add_parser("statusline", help="Status line command; reads Claude status JSON on stdin")
    statusline.set_defaults(func=cmd_statusline)

    hook = sub.add_parser("hook", help="Claude Code hook command; reads hook JSON on stdin")
    hook.add_argument("--dry-run", action="store_true", help="Create handoff but do not launch Codex")
    hook.add_argument("--force", action="store_true", help="Force rescue even if thresholds are not hit")
    hook.add_argument("--print-empty-json", action="store_true", help="Print {} when no rescue is needed")
    hook.set_defaults(func=cmd_hook)

    handoff = sub.add_parser("handoff", help="Create a handoff folder manually")
    handoff.add_argument("--cwd", help="Working directory to snapshot")
    handoff.add_argument("--reason", action="append", help="Reason line for the handoff")
    handoff.add_argument("--status-json", help="Optional saved status JSON")
    handoff.add_argument("--transcript", help="Optional Claude transcript path")
    handoff.set_defaults(func=cmd_handoff)

    automation = sub.add_parser("automation", help="Budget-limited wrapper around claude -p")
    automation.add_argument("--cwd", help="Working directory")
    automation.add_argument("--plan", choices=sorted(PLAN_AGENT_SDK_CREDITS_USD), help="Subscription plan profile")
    automation.add_argument("--budget-usd", type=float, help="Plan or automation budget in USD")
    automation.add_argument("--credit-usd", type=float, help="Override monthly Agent SDK credit in USD")
    automation.add_argument("--remaining-usd", type=float, help="Override remaining automation credit in USD")
    automation.add_argument("--spent-usd", type=float, help="Subtract already-spent automation credit in USD")
    automation.add_argument("--soft-pct", type=float, help="Soft stop percentage of budget")
    automation.add_argument("--max-turns", type=int, default=8, help="Max Claude turns")
    automation.add_argument("--launch-codex", action="store_true", help="Launch Codex rescue automatically after handoff")
    automation.add_argument("--codex-model", help="Codex model used if this automation run creates a rescue")
    automation.add_argument("--codex-effort", help="Codex reasoning effort used if this automation run creates a rescue")
    automation.add_argument("--simulate-budget-exit", action="store_true", help="Demo mode: create the post-budget handoff without running claude")
    automation.add_argument("--dry-run", action="store_true", help="Print claude command instead of running it")
    automation.add_argument("prompt", nargs=argparse.REMAINDER)
    automation.set_defaults(func=cmd_automation)

    estimate = sub.add_parser("estimate", help="Estimate model cost against the configured automation budget")
    estimate.add_argument("--cwd", help="Working directory")
    estimate.add_argument("--plan", choices=sorted(PLAN_AGENT_SDK_CREDITS_USD), help="Subscription plan profile")
    estimate.add_argument("--model", help="Model price key from config")
    estimate.add_argument("--budget-usd", type=float, help="Override automation budget in USD")
    estimate.add_argument("--credit-usd", type=float, help="Override monthly Agent SDK credit in USD")
    estimate.add_argument("--remaining-usd", type=float, help="Override remaining automation credit in USD")
    estimate.add_argument("--spent-usd", type=float, help="Subtract already-spent automation credit in USD")
    estimate.add_argument("--soft-pct", type=float, help="Soft stop percentage of budget")
    estimate.add_argument("--input-tokens", type=float, default=0.0)
    estimate.add_argument("--output-tokens", type=float, default=0.0)
    estimate.add_argument("--cache-write-5m-tokens", type=float, default=0.0)
    estimate.add_argument("--cache-write-1h-tokens", type=float, default=0.0)
    estimate.add_argument("--cache-read-tokens", type=float, default=0.0)
    estimate.set_defaults(func=cmd_estimate)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

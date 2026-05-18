import importlib.util
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("budget_rescue", ROOT / "budget_rescue.py")
budget_rescue = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(budget_rescue)


class BudgetRescueTests(unittest.TestCase):
    def test_reasons_trigger_on_rate_limit(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        status = {
            "five_hour_used_pct": 89,
            "seven_day_used_pct": 20,
            "context_used_pct": 40,
        }
        reasons = budget_rescue.rescue_reasons(status, cfg)
        self.assertEqual(len(reasons), 1)
        self.assertIn("5h Claude limit", reasons[0])

    def test_reasons_trigger_on_context(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        status = {
            "five_hour_used_pct": 10,
            "seven_day_used_pct": 20,
            "context_used_pct": 91,
        }
        reasons = budget_rescue.rescue_reasons(status, cfg)
        self.assertEqual(len(reasons), 1)
        self.assertIn("context window", reasons[0])

    def test_handoff_writes_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".git").mkdir()
            cfg = dict(budget_rescue.DEFAULT_CONFIG)
            cfg["handoff_dir"] = ".agent-handoff"
            status = {"session_id": "abc123", "cwd": str(cwd), "task_prompt": "build the demo app"}
            handoff = budget_rescue.create_handoff(cfg, cwd, ["demo reason"], status, {"hook_event_name": "Stop"})

            self.assertTrue((handoff / "handoff.md").exists())
            self.assertTrue((handoff / "codex-prompt.txt").exists())
            self.assertTrue((handoff / "codex-plugin-command.txt").exists())
            self.assertTrue((handoff / "original-task.txt").exists())
            self.assertTrue((handoff / "codex-status-command.sh").exists())
            self.assertIn("demo reason", (handoff / "handoff.md").read_text())
            self.assertIn("build the demo app", (handoff / "handoff.md").read_text())
            self.assertIn("build the demo app", (handoff / "codex-prompt.txt").read_text())
            self.assertIn("/codex:rescue", (handoff / "codex-plugin-command.txt").read_text())
            self.assertIn("Agent SDK monthly credit", (handoff / "handoff.md").read_text())

    def test_hook_response_for_stop_does_not_loop(self):
        response = budget_rescue.hook_response("Stop", "handoff created")
        self.assertFalse(response["continue"])
        self.assertIn("handoff created", response["stopReason"])

    def test_automation_dry_run_builds_soft_budget_command(self):
        args = Namespace(
            cwd=None,
            config=None,
            plan=None,
            budget_usd=20.0,
            credit_usd=None,
            remaining_usd=None,
            spent_usd=None,
            soft_pct=80.0,
            max_turns=3,
            launch_codex=False,
            codex_model=None,
            codex_effort=None,
            simulate_budget_exit=False,
            dry_run=True,
            prompt=["fix", "tests"],
        )
        with mock.patch("sys.stdout") as stdout:
            rc = budget_rescue.cmd_automation(args)
        self.assertEqual(rc, 0)
        printed = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("--max-budget-usd 16.00", printed)
        self.assertIn("--max-turns 3", printed)

    def test_automation_simulated_budget_exit_creates_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = Namespace(
                cwd=tmp,
                config=None,
                plan=None,
                budget_usd=20.0,
                credit_usd=None,
                remaining_usd=None,
                spent_usd=None,
                soft_pct=80.0,
                max_turns=3,
                launch_codex=False,
                codex_model="gpt-5.4-mini",
                codex_effort="high",
                simulate_budget_exit=True,
                dry_run=False,
                prompt=["fix", "tests"],
            )
            with mock.patch("sys.stdout") as stdout:
                rc = budget_rescue.cmd_automation(args)
            self.assertEqual(rc, 1)
            printed = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
            self.assertIn("Simulated Claude budget exit", printed)
            self.assertIn("Budget rescue handoff", printed)
            self.assertIn("Codex was not launched automatically", printed)
            handoffs = list((Path(tmp) / ".agent-handoff").glob("*"))
            self.assertEqual(len(handoffs), 1)
            self.assertTrue((handoffs[0] / "codex-plugin-command.txt").exists())
            self.assertTrue((handoffs[0] / "original-task.txt").exists())
            self.assertEqual((handoffs[0] / "original-task.txt").read_text().strip(), "fix tests")
            self.assertIn("Original task:\nfix tests", (handoffs[0] / "codex-prompt.txt").read_text())
            command = (handoffs[0] / "codex-plugin-command.txt").read_text()
            self.assertIn("--model gpt-5.4-mini", command)
            self.assertIn("--effort high", command)

    def test_extracts_codex_job_id_from_launch_output(self):
        output = (
            "Codex Task started in the background as task-mpaj52tv-kbyzr5. "
            "Check /codex:status task-mpaj52tv-kbyzr5 for progress."
        )
        self.assertEqual(budget_rescue.extract_codex_job_id(output), "task-mpaj52tv-kbyzr5")

    def test_codex_exec_command_uses_current_cli_flags(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        cfg["codex_model"] = "gpt-5.4-mini"
        cfg["codex_effort"] = "medium"
        bits = budget_rescue.codex_exec_bits(cfg, Path("/tmp/demo"), "continue")
        self.assertIn("exec", bits)
        self.assertIn("--cd", bits)
        self.assertIn("--sandbox", bits)
        self.assertIn("--model", bits)
        self.assertIn('model_reasoning_effort="medium"', bits)
        self.assertNotIn("--ask-for-approval", bits)

    def test_launch_falls_back_to_codex_exec_background_when_companion_is_not_ready(self):
        class FakeProc:
            pid = 12345

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".git").mkdir()
            cfg = dict(budget_rescue.DEFAULT_CONFIG)
            cfg["handoff_dir"] = ".agent-handoff"
            handoff = budget_rescue.create_handoff(cfg, cwd, ["demo reason"], {"session_id": "abc123"}, {})

            with mock.patch.object(budget_rescue, "discover_companion", return_value=None), mock.patch.object(
                budget_rescue.subprocess,
                "Popen",
                return_value=FakeProc(),
            ) as popen:
                output = budget_rescue.launch_codex(cfg, handoff, cwd)

            self.assertIn("Falling back to Codex CLI background launch", output)
            self.assertTrue((handoff / "codex-exec.pid").exists())
            self.assertTrue((handoff / "codex-status-command.sh").exists())
            self.assertIn("Codex CLI Background Job", (handoff / "codex-status-command.sh").read_text())
            launched_args = popen.call_args.args[0]
            self.assertEqual(launched_args[0:2], ["codex", "exec"])

    def test_streaming_process_echoes_and_captures_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                completed = budget_rescue.run_streaming_process(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('live progress'); sys.stdout.flush()",
                    ],
                    Path(tmp),
                )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("live progress", completed.stdout)
        self.assertIn("live progress", stdout.getvalue())

    def test_plan_credit_drives_automation_budget(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        budget, source = budget_rescue.resolve_automation_budget_usd(cfg, plan_override="max_5x")
        self.assertEqual(budget, 100.0)
        self.assertEqual(source, "plan credit minus spent")
        self.assertEqual(budget_rescue.soft_budget_usd(budget, 80), 80.0)

    def test_spent_credit_reduces_budget(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        cfg["automation_credit_spent_usd"] = 4.25
        budget, _ = budget_rescue.resolve_automation_budget_usd(cfg, plan_override="pro")
        self.assertEqual(budget, 15.75)
        self.assertEqual(budget_rescue.soft_budget_usd(budget, 80), 12.6)

    def test_estimate_uses_model_prices(self):
        cfg = dict(budget_rescue.DEFAULT_CONFIG)
        cost = budget_rescue.estimate_cost_usd(
            cfg,
            "claude-sonnet-4.6",
            input_tokens=50_000,
            output_tokens=15_000,
        )
        self.assertEqual(cost, 0.375)


if __name__ == "__main__":
    unittest.main()

"""Tests for selecting Claude Code as an alternative agent backend."""

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import analyze_papers
import codex_cli
import solve_open_problems
import workbench_tasks
import workbench_usage
import write_paper


FAKE_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    prompt = sys.stdin.read()
    with open("claude-argv.json", "w", encoding="utf-8") as record:
        json.dump({{"argv": sys.argv[1:], "prompt": prompt}}, record)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
    print(json.dumps({{"type": "system", "subtype": "init", "model": "fake"}}))
    if mode == "limit":
        print(json.dumps({{
            "type": "result", "subtype": "error_during_execution",
            "is_error": True, "result": "Claude AI usage limit reached",
        }}))
        sys.exit(1)
    with open("agent-result.json", "w", encoding="utf-8") as result:
        json.dump({{"status": "complete"}}, result)
    print(json.dumps({{
        "type": "result", "subtype": "success", "is_error": False,
        "result": "done",
        "usage": {{"input_tokens": 10, "cache_read_input_tokens": 90,
                   "cache_creation_input_tokens": 5, "output_tokens": 20}},
    }}))
    """
)


def parser_with_model_arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    codex_cli.add_model_arguments(parser)
    codex_cli.add_model_arguments(parser, prefix="review")
    return parser


class ClaudeCommandTests(unittest.TestCase):
    def test_command_is_sandboxed_noninteractive_and_tool_limited(self):
        command = codex_cli.build_claude_command(
            claude="claude",
            options=codex_cli.ModelOptions(
                "opus", "ultra", agent="claude"
            ),
            web_search="disabled",
        )
        self.assertEqual(command[:2], ["claude", "-p"])
        for flag in (
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--verbose",
        ):
            self.assertIn(flag, command)
        self.assertEqual(
            command[command.index("--output-format") + 1], "stream-json"
        )
        self.assertEqual(
            command[command.index("--permission-prompts") + 1], "none"
        )
        self.assertEqual(
            command[command.index("--tools") + 1], "Bash,Read,Edit,Write"
        )
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--effort") + 1], "max")
        settings = json.loads(command[command.index("--settings") + 1])
        sandbox = settings["sandbox"]
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertEqual(sandbox["network"]["allowedDomains"], [])
        # The variadic deny rule must not swallow a later argument.
        self.assertEqual(command[-2:], ["--disallowedTools", "mcp__*"])

    def test_web_search_modes_map_to_web_tools(self):
        def tools(mode):
            command = codex_cli.build_claude_command(
                claude="claude",
                options=codex_cli.ModelOptions(agent="claude"),
                web_search=mode,
            )
            return command[command.index("--tools") + 1].split(",")

        self.assertNotIn("WebSearch", tools("disabled"))
        self.assertIn("WebSearch", tools("indexed"))
        self.assertNotIn("WebFetch", tools("indexed"))
        self.assertIn("WebFetch", tools("live"))

    def test_setting_sources_can_be_overridden(self):
        with patch.dict(os.environ, {codex_cli.CLAUDE_SETTING_SOURCES_ENV: "user"}):
            command = codex_cli.build_claude_command(
                claude="claude", options=codex_cli.ModelOptions(agent="claude")
            )
        self.assertEqual(command[command.index("--setting-sources") + 1], "user")


class OptionTests(unittest.TestCase):
    def test_codex_digest_is_unchanged_by_agent_field(self):
        legacy = codex_cli.semantic_config_digest(
            "prompt", "{}", codex_cli.ModelOptions("gpt-6-astra", "xhigh")
        )
        explicit = codex_cli.semantic_config_digest(
            "prompt", "{}",
            codex_cli.ModelOptions("gpt-6-astra", "xhigh", agent="codex"),
        )
        claude = codex_cli.semantic_config_digest(
            "prompt", "{}",
            codex_cli.ModelOptions("gpt-6-astra", "xhigh", agent="claude"),
        )
        self.assertEqual(legacy, explicit)
        self.assertNotEqual(legacy, claude)

    def test_analysis_digest_is_unchanged_for_codex(self):
        self.assertEqual(
            analyze_papers.analysis_config_digest("p", "{}", "m", "high", False),
            analyze_papers.analysis_config_digest(
                "p", "{}", "m", "high", False, False, "codex"
            ),
        )

    def test_claude_agent_drops_the_codex_default_model(self):
        args = parser_with_model_arguments().parse_args(["--agent", "claude"])
        options = codex_cli.model_options_from_args(args)
        self.assertEqual(options.agent, "claude")
        self.assertIsNone(options.model)
        self.assertEqual(options.reasoning_effort, codex_cli.DEFAULT_REASONING_EFFORT)

    def test_explicit_claude_model_is_kept(self):
        args = parser_with_model_arguments().parse_args(
            ["--agent", "claude", "--model", "opus"]
        )
        self.assertEqual(codex_cli.model_options_from_args(args).model, "opus")

    def test_fast_mode_is_rejected_for_claude(self):
        args = parser_with_model_arguments().parse_args(
            ["--agent", "claude", "--fast"]
        )
        with self.assertRaises(codex_cli.CodexError):
            codex_cli.model_options_from_args(args)

    def test_default_agent_is_codex(self):
        args = parser_with_model_arguments().parse_args([])
        options = codex_cli.model_options_from_args(args)
        self.assertEqual(options.agent_name, "codex")
        self.assertEqual(options.model, codex_cli.DEFAULT_MODEL)
        review = codex_cli.model_options_from_args(args, prefix="review")
        self.assertIsNone(review.agent)

    def test_critic_with_same_agent_inherits_model(self):
        primary = codex_cli.ModelOptions("gpt-6-astra", "xhigh", fast=True)
        inherited = codex_cli.inherit_model_options(
            primary, codex_cli.ModelOptions()
        )
        self.assertEqual(inherited, primary)
        # The solver and writer helpers keep their behavior.
        self.assertEqual(
            solve_open_problems._inherit_review_options(
                primary, codex_cli.ModelOptions()
            ),
            primary,
        )
        self.assertEqual(
            write_paper._inherit_options(primary, codex_cli.ModelOptions()),
            primary,
        )

    def test_critic_switching_agent_does_not_inherit_model_or_tier(self):
        primary = codex_cli.ModelOptions("gpt-6-astra", "xhigh", fast=True)
        claude_critic = codex_cli.inherit_model_options(
            primary, codex_cli.ModelOptions(agent="claude")
        )
        self.assertEqual(claude_critic.agent, "claude")
        self.assertIsNone(claude_critic.model)
        self.assertEqual(claude_critic.reasoning_effort, "xhigh")
        self.assertFalse(claude_critic.fast)
        codex_critic = codex_cli.inherit_model_options(
            codex_cli.ModelOptions("opus", "high", agent="claude"),
            codex_cli.ModelOptions(agent="codex"),
        )
        self.assertEqual(codex_critic.model, codex_cli.DEFAULT_MODEL)

    def test_provenance_only_for_non_default_agent(self):
        self.assertEqual(codex_cli.agent_provenance(codex_cli.ModelOptions()), {})
        with patch.object(codex_cli, "agent_version", return_value="2.1.300"):
            provenance = codex_cli.agent_provenance(
                codex_cli.ModelOptions(agent="claude")
            )
        self.assertEqual(
            provenance, {"requested_agent": "claude", "agent_version": "2.1.300"}
        )


class CompletionTests(unittest.TestCase):
    def write_events(self, directory: Path, *events) -> Path:
        path = directory / "events.jsonl"
        path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        return path

    def test_successful_result_with_valid_json_completes(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = directory / "agent-result.json"
            result.write_text("{}", encoding="utf-8")
            events = self.write_events(
                directory,
                {"type": "system", "subtype": "init"},
                {"type": "result", "subtype": "success", "is_error": False},
            )
            self.assertTrue(codex_cli.claude_turn_is_complete(events, result))

    def test_error_result_or_missing_json_does_not_complete(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = directory / "agent-result.json"
            events = self.write_events(
                directory,
                {"type": "result", "subtype": "success", "is_error": False},
            )
            self.assertFalse(codex_cli.claude_turn_is_complete(events, result))
            result.write_text("{}", encoding="utf-8")
            events = self.write_events(
                directory,
                {"type": "result", "subtype": "error_max_turns", "is_error": True},
            )
            self.assertFalse(codex_cli.claude_turn_is_complete(events, result))

    def test_usage_limit_is_recognized(self):
        with TemporaryDirectory() as temporary:
            events = self.write_events(
                Path(temporary),
                {"type": "result", "subtype": "success", "is_error": True,
                 "result": "Claude AI usage limit reached|1767225600"},
            )
            self.assertIn("usage limit", codex_cli.claude_limit_error(events))


class ClaudeRunTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        executable = root / "bin" / "claude"
        executable.parent.mkdir()
        executable.write_text(
            FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        environment = patch.dict(
            os.environ, {codex_cli.CLAUDE_EXECUTABLE_ENV: str(executable)}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def run_claude(self, **kwargs):
        return codex_cli.run_structured_codex(
            codex="codex-not-used",
            workspace=self.workspace,
            prompt="Solve the problem.",
            schema_path=self.workspace / "schema.json",
            options=codex_cli.ModelOptions("opus", "high", agent="claude"),
            web_search="live",
            launch_interval=0,
            model_writes_result=True,
            **kwargs,
        )

    @unittest.skipIf(os.name == "nt", "fake executable uses a POSIX shebang")
    def test_successful_turn_uses_claude_and_returns_result(self):
        result = self.run_claude()
        self.assertEqual(
            json.loads(result.read_text(encoding="utf-8")), {"status": "complete"}
        )
        record = json.loads(
            (self.workspace / "claude-argv.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["prompt"], "Solve the problem.")
        self.assertEqual(record["argv"][0], "-p")
        self.assertIn("WebFetch", record["argv"][record["argv"].index("--tools") + 1])
        events = (self.workspace / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"type": "result"', events)

    @unittest.skipIf(os.name == "nt", "fake executable uses a POSIX shebang")
    def test_completion_detection_path_also_succeeds(self):
        result = self.run_claude(timeout_seconds=60, completion_grace_seconds=5)
        self.assertTrue(result.is_file())

    @unittest.skipIf(os.name == "nt", "fake executable uses a POSIX shebang")
    def test_usage_limit_pauses_queue_and_fails(self):
        with (
            patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "limit"}),
            patch.object(codex_cli, "pause_queue_for_credit_error") as pause,
        ):
            with self.assertRaisesRegex(codex_cli.CodexError, "usage limit"):
                self.run_claude()
        pause.assert_called_once()

    def test_windows_host_is_refused(self):
        with patch.object(codex_cli, "is_windows_host", return_value=True):
            with self.assertRaisesRegex(codex_cli.CodexError, "WSL2"):
                self.run_claude()

    def test_codex_style_output_file_contract_is_refused(self):
        with self.assertRaisesRegex(codex_cli.CodexError, "model-written"):
            codex_cli.run_structured_codex(
                codex="codex",
                workspace=self.workspace,
                prompt="x",
                schema_path=self.workspace / "schema.json",
                options=codex_cli.ModelOptions(agent="claude"),
                launch_interval=0,
            )


class WorkbenchAndUsageTests(unittest.TestCase):
    def test_workbench_passes_agents_to_commands(self):
        argv: list[str] = []
        workbench_tasks._common_arguments(argv, {"agent": "claude"})
        workbench_tasks._review_arguments(argv, {"reviewAgent": "codex"})
        self.assertEqual(argv, ["--agent", "claude", "--review-agent", "codex"])
        with self.assertRaises(workbench_tasks.PlanError):
            workbench_tasks._common_arguments([], {"agent": "gemini"})

    def test_claude_usage_maps_to_codex_counters(self):
        self.assertEqual(
            workbench_usage.claude_token_usage({
                "input_tokens": 10,
                "cache_read_input_tokens": 90,
                "cache_creation_input_tokens": 5,
                "output_tokens": 20,
            }),
            {
                "input_tokens": 105,
                "cached_input_tokens": 90,
                "cache_write_input_tokens": 5,
                "output_tokens": 20,
            },
        )
        self.assertIsNone(workbench_usage.claude_token_usage({"input_tokens": -1}))


if __name__ == "__main__":
    unittest.main()

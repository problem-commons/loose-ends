"""Exporting solver context and importing attempts made outside the solver."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_problem_context
import human_review
import import_attempt
import open_problem_common as common
import review_solutions
from test_open_problem_pipeline import make_analyzed_paper


def claim(claim_id: str, statement: str = "A bound holds.") -> dict:
    return {
        "id": claim_id,
        "type": "lemma",
        "statement": statement,
        "support": "Section 2 of attempt.md.",
        "remaining_gap": "",
    }


def solver_result(claims: list[dict], dispositions: list[dict] | None = None) -> dict:
    return {
        "claimed_result_type": "partial_result" if claims else "none",
        "summary": "Progress on the conjecture.",
        "external_sources": [],
        "checkable_claims": claims,
        "prior_claim_dispositions": dispositions or [],
        "artifacts": [],
        "warnings": [],
    }


class OutsideAttemptTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paper = make_analyzed_paper(self.root / "papers")
        first = self.paper / "OP-001" / "attempt-001"
        first.mkdir(parents=True)
        (first / "attempt.md").write_text("# Attempt\n\nC-001: a bound.\n", encoding="utf-8")
        common.write_json(first / "solver-result.json", solver_result([claim("C-001")]))
        (first / "critique.md").write_text("Fine.\n", encoding="utf-8")
        self.problem_path = self.paper / "OP-001"

    def run_main(self, main, *argv):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            try:
                code = main([str(value) for value in argv])
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue() + error.getvalue()

    def export(self, name: str = "context", *extra: str) -> Path:
        output = self.root / name
        code, text = self.run_main(
            export_problem_context.main, self.problem_path, "--output", output, *extra
        )
        self.assertEqual(code, 0, text)
        return output

    def write_session(self, result: dict, markdown: str) -> Path:
        session = self.root / "session"
        session.mkdir(exist_ok=True)
        (session / "attempt.md").write_text(markdown, encoding="utf-8")
        common.write_json(session / "agent-result.json", result)
        return session

    def good_session(self) -> Path:
        return self.write_session(
            solver_result(
                [claim("C-001", "A stronger bound holds.")],
                [{
                    "source_attempt": "attempt-001",
                    "source_claim_id": "C-001",
                    "disposition": "strengthened",
                    "current_claim_id": "C-001",
                    "explanation": "The constant improves.",
                }],
            ),
            "# Attempt\n\n## History reconciliation\n\nStrengthens attempt-001/C-001.\n\n"
            "## Checkable claims\n\nC-001: a stronger bound.\n",
        )

    def import_args(self, session: Path, *extra: str) -> list:
        return [
            self.problem_path, "--from", session, "--origin", "interactive",
            "--person", "kbuchin", "--agent", "claude", "--model", "opus", *extra,
        ]

    def test_export_stages_what_the_solver_sees(self):
        output = self.export()
        self.assertTrue((output / "inputs" / "paper" / "paper.pdf").is_file())
        self.assertTrue((output / "inputs" / "analysis" / "open-problems.md").is_file())
        self.assertTrue(
            (output / "inputs" / "history" / "OP-001" / "attempt-001" / "critique.md").is_file()
        )
        task = (output / "TASK.md").read_text(encoding="utf-8")
        self.assertIn("OP-001", task)
        self.assertIn("agent-result.json", task)
        self.assertNotIn("{{", task)
        record = common.read_json(output / "context.json")
        self.assertEqual(record["next_attempt"], "attempt-002")
        self.assertEqual(record["prior_claim_refs"], ["attempt-001/C-001"])
        self.assertEqual(record["research_guidance"]["source"], "explicit_selection")
        self.assertTrue((output / "agent-result.schema.json").is_file())

    def test_export_refuses_to_overwrite_without_replace(self):
        self.export()
        code, text = self.run_main(
            export_problem_context.main, self.problem_path, "--output", self.root / "context"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("--replace", text)
        self.export("context", "--replace")

    def test_import_installs_the_next_attempt_with_its_origin(self):
        context = self.export()
        code, text = self.run_main(
            import_attempt.main,
            *self.import_args(self.good_session(), "--context", context / "context.json",
                              "--session", "sessions/2026-11-02-kbuchin-bound"),
        )
        self.assertEqual(code, 0, text)
        attempt = self.paper / "OP-001" / "attempt-002"
        manifest = common.read_json(attempt / "manifest.json")
        self.assertEqual(manifest["origin"]["kind"], "interactive")
        self.assertEqual(manifest["origin"]["people"], ["kbuchin"])
        self.assertEqual(manifest["requested_agent"], "claude")
        self.assertEqual(manifest["requested_model"], "opus")
        self.assertEqual(
            manifest["prior_attempt_history_digest"],
            common.read_json(context / "context.json")["attempt_history_digest"],
        )
        self.assertEqual(
            common.read_json(attempt / "solver-result.json")["claimed_result_type"],
            "partial_result",
        )
        self.assertEqual(
            human_review.attempt_origin(attempt)["session"],
            "sessions/2026-11-02-kbuchin-bound",
        )
        problem = export_problem_context.resolve_problem(self.problem_path)
        names = [ref.name for ref in review_solutions.discover_attempt_refs([problem])]
        self.assertEqual(names, ["attempt-001", "attempt-002"])
        self.assertFalse(any(path.name.startswith(".attempt-install-")
                             for path in (self.paper / "OP-001").iterdir()))

    def test_invalid_results_are_reported_and_not_installed(self):
        session = self.write_session(
            solver_result(
                [claim("C-001")],
                [{
                    "source_attempt": "attempt-007",
                    "source_claim_id": "C-001",
                    "disposition": "retained",
                    "current_claim_id": "C-001",
                    "explanation": "Still holds.",
                }],
            ),
            "# Attempt without the claim id\n",
        )
        code, text = self.run_main(import_attempt.main, *self.import_args(session))
        self.assertEqual(code, 1)
        self.assertIn("attempt.md omits C-001", text)
        self.assertIn("unknown prior claim attempt-007/C-001", text)
        self.assertFalse((self.paper / "OP-001" / "attempt-002").exists())

    def test_new_history_after_the_export_stops_the_import(self):
        context = self.export()
        (self.paper / "OP-001" / "attempt-001" / "critique.md").write_text(
            "Actually, a gap.\n", encoding="utf-8"
        )
        args = self.import_args(self.good_session(), "--context", context / "context.json")
        code, text = self.run_main(import_attempt.main, *args)
        self.assertEqual(code, 1)
        self.assertIn("reviewed or changed since the export", text)
        code, text = self.run_main(import_attempt.main, *args, "--allow-new-history")
        self.assertEqual(code, 0, text)

    def test_first_attempt_on_an_untouched_problem(self):
        problem = self.paper / "OP-002"
        self.assertFalse(problem.exists())
        session = self.write_session(solver_result([claim("C-001")]), "# Attempt\n\nC-001\n")
        code, text = self.run_main(
            import_attempt.main, problem, "--from", session, "--origin", "interactive",
            "--person", "kbuchin", "--agent", "claude",
        )
        self.assertEqual(code, 0, text)
        self.assertTrue((problem / "attempt-001" / "attempt.md").is_file())

    def test_dry_run_and_external_source(self):
        code, text = self.run_main(
            import_attempt.main, *self.import_args(self.good_session(), "--dry-run")
        )
        self.assertEqual(code, 0, text)
        self.assertIn("would install OP-001/attempt-002", text)
        self.assertIn("no --context", text)
        self.assertFalse((self.paper / "OP-001" / "attempt-002").exists())
        args = [
            self.problem_path, "--from", self.good_session(), "--origin", "external",
            "--person", "kbuchin", "--agent", "claude", "--agent", "codex",
        ]
        code, text = self.run_main(import_attempt.main, *args)
        self.assertNotEqual(code, 0)
        self.assertIn("--source", text)
        code, text = self.run_main(
            import_attempt.main, *args, "--source", "https://example.org/spanners@abc123"
        )
        self.assertEqual(code, 0, text)
        manifest = common.read_json(self.paper / "OP-001" / "attempt-002" / "manifest.json")
        self.assertEqual(manifest["requested_agent"], "mixed")
        self.assertEqual(manifest["origin"]["source"], "https://example.org/spanners@abc123")


if __name__ == "__main__":
    unittest.main()

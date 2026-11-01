"""Extra history, skip labels, and reviews or literature reports made outside the pipeline."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_problem_context
import import_literature
import import_review
import open_problem_common as common
import triage_open_problems
from test_open_problem_pipeline import make_analyzed_paper
from test_outside_attempts import claim, solver_result


def review_result(claim_ids: list[str]) -> dict:
    return {
        "correctness": "minor_gaps", "reviewed_coverage": "partial", "importance": "moderate",
        "verification_confidence": "medium", "summary": "Mostly right.",
        "claim_reviews": [
            {"claim_id": claim_id, "assessment": "partially_supported", "explanation": "Gap."}
            for claim_id in claim_ids
        ],
        "blocking_gaps": [], "recommended_next_steps": [], "warnings": [],
    }


def literature_result(problem_id: str) -> dict:
    return {
        "status": "complete", "warnings": [],
        "literature": [{
            "problem_id": problem_id, "resolution_status": "partially_resolved",
            "confidence": "medium", "status_summary": "A special case is known.",
            "exact_match_analysis": "Same statement.", "residual_problem": "The general case.",
            "solver_briefing": "Start from the special case.",
            "sources": [{
                "id": "S1", "role": "special_case", "priority": "high", "title": "A note",
                "authors": ["B. Author"], "publication_year": "2025",
                "url": "https://example.org/note", "source_type": "primary_source",
                "result_statement": "Holds for trees.", "relevance": "Special case.",
                "limitations": "Trees only.",
            }],
            "search_queries": ["test conjecture trees"], "warnings": [],
        }],
    }


class HumanLayerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paper = make_analyzed_paper(self.root / "papers" / "own")
        self.attempt = self.paper / "OP-001" / "attempt-001"
        self.attempt.mkdir(parents=True)
        (self.attempt / "attempt.md").write_text("# Attempt\n\nC-001: a bound.\n", encoding="utf-8")
        common.write_json(self.attempt / "solver-result.json", solver_result([claim("C-001")]))
        (self.attempt / "critique.md").write_text("Critic says fine.\n", encoding="utf-8")
        common.write_json(self.attempt / "review-result.json", review_result(["C-001"]))
        extra = self.paper / common.EXTRA_HISTORY_DIRECTORY / "OP-001"
        (extra / "attempt-001").mkdir(parents=True)
        (extra / "extra-notes.md").write_text("Verdict: imprecise.\n", encoding="utf-8")
        (extra / "attempt-001" / "extra-review-claude.md").write_text("Claude: gap.\n", encoding="utf-8")
        (extra / "attempt-001" / "critique.md").write_text("must not shadow\n", encoding="utf-8")
        (extra / "attempt-009").mkdir()
        (extra / "attempt-009" / "extra-review-x.md").write_text("orphan\n", encoding="utf-8")

    def run_main(self, main, *argv):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            try:
                code = main([str(value) for value in argv])
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue() + error.getvalue()

    def problem(self):
        return export_problem_context.resolve_problem(self.paper / "OP-001")

    def test_extra_history_is_staged_with_the_attempts(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        inputs = common.stage_context(workspace, [self.problem()], include_paper=False)
        history = inputs / "history" / "OP-001"
        self.assertEqual((history / "extra-notes.md").read_text(), "Verdict: imprecise.\n")
        self.assertTrue((history / "attempt-001" / "extra-review-claude.md").is_file())
        self.assertEqual((history / "attempt-001" / "critique.md").read_text(), "Critic says fine.\n")
        self.assertFalse((history / "attempt-009").exists())
        workspace = self.root / "critic"
        workspace.mkdir()
        inputs = common.stage_context(workspace, [self.problem()], include_paper=False,
                                      exclude_review_for=self.attempt)
        self.assertFalse((inputs / "history" / "OP-001" / "attempt-001" / "extra-review-claude.md").exists())

    def test_skip_labels_keep_the_pipeline_away(self):
        labels = self.root / "labels.json"
        labels.write_text(json.dumps({"schema_version": 1, "labels": [
            {"target": f"own/{self.paper.name}/OP-002", "label": "excluded", "hidden": True,
             "skip": True, "title": "Ongoing work"},
        ]}), encoding="utf-8")
        with patch.dict(os.environ, {"LOOSE_ENDS_LABELS": str(labels)}):
            code, output = self.run_main(triage_open_problems.main, self.paper, "--dry-run")
            self.assertEqual(code, 0, output)
            self.assertIn("Leaving out 1 excluded problem(s)", output)
            self.assertNotIn(f"{self.paper} OP-002", output)
            code, output = self.run_main(triage_open_problems.main, self.paper / "OP-002", "--dry-run")
            self.assertNotEqual(code, 0)
            self.assertIn("is excluded (Ongoing work)", output)
        code, output = self.run_main(triage_open_problems.main, self.paper / "OP-002", "--dry-run")
        self.assertEqual(code, 0, output)

    def test_review_export_and_import(self):
        context = self.root / "review-context"
        code, output = self.run_main(export_problem_context.main, self.attempt, "--task", "review",
                                     "--output", context)
        self.assertEqual(code, 0, output)
        task = (context / "TASK.md").read_text(encoding="utf-8")
        self.assertIn("`inputs/history/OP-001/attempt-001`", task)
        self.assertNotIn(str(self.root), task)
        self.assertFalse((context / "inputs" / "history" / "OP-001" / "attempt-001" / "critique.md").exists())
        record = common.read_json(context / "context.json")
        self.assertEqual((record["task"], record["claim_ids"]), ("review", ["C-001"]))
        blind = self.root / "blind"
        self.run_main(export_problem_context.main, self.attempt, "--task", "review",
                      "--output", blind, "--blind")
        self.assertFalse(list((blind / "inputs").rglob("extra-review-*")))

        work = self.root / "review-work"
        work.mkdir()
        (work / "critique.md").write_text("# Critique\n\nC-001 has a gap.\n", encoding="utf-8")
        common.write_json(work / "agent-result.json", review_result(["C-002"]))
        args = [self.attempt, "--from", work, "--output", self.root / "filed",
                "--context", context / "context.json", "--person", "kbuchin", "--agent", "claude"]
        code, output = self.run_main(import_review.main, *args)
        self.assertEqual(code, 1)
        self.assertIn("C-002", output)
        common.write_json(work / "agent-result.json", review_result(["C-001"]))
        code, output = self.run_main(import_review.main, *args)
        self.assertEqual(code, 0, output)
        manifest = common.read_json(self.root / "filed" / "review-manifest.json")
        self.assertEqual((manifest["origin"]["kind"], manifest["requested_agent"]), ("interactive", "claude"))
        self.assertEqual(common.read_json(self.root / "filed" / "review-result.json")["human_priority"], "medium")
        self.assertEqual((self.attempt / "critique.md").read_text(), "Critic says fine.\n")
        (self.attempt / "attempt.md").write_text("# Changed\n\nC-001\n", encoding="utf-8")
        args[4] = self.root / "filed-again"
        code, output = self.run_main(import_review.main, *args)
        self.assertEqual(code, 1)
        self.assertIn("the attempt changed after the export", output)

    def test_literature_export_and_import(self):
        context = self.root / "literature-context"
        code, output = self.run_main(export_problem_context.main, self.paper / "OP-001", "--task",
                                     "literature", "--output", context)
        self.assertEqual(code, 0, output)
        self.assertIn("OP-001", (context / "TASK.md").read_text(encoding="utf-8"))
        work = self.root / "literature-work"
        work.mkdir()
        common.write_json(work / "agent-result.json", literature_result("OP-001"))
        args = [self.paper / "OP-001", "--from", work, "--context", context / "context.json",
                "--person", "kbuchin", "--agent", "claude", "--model", "opus"]
        code, output = self.run_main(import_literature.main, *args)
        self.assertEqual(code, 1)
        self.assertIn("literature-OP-001.md", output)
        (work / "literature-OP-001.md").write_text("# Literature OP-001\n\nA special case.\n", encoding="utf-8")
        code, output = self.run_main(import_literature.main, *args)
        self.assertEqual(code, 0, output)
        problem = self.problem()
        self.assertTrue(common.literature_is_current(problem))
        self.assertEqual(common.literature_result(problem)["resolution_status"], "partially_resolved")
        self.assertEqual(common.literature_manifest(problem)["origin"]["models"], ["opus"])
        code, output = self.run_main(import_literature.main, *args)
        self.assertEqual(code, 1)
        self.assertIn("--force", output)
        code, output = self.run_main(import_literature.main, *args, "--force")
        self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()

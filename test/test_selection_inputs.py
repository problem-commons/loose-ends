"""Paper or folder paths next to PAPER/OP-ID paths must not be dropped."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import literature_review
import open_problem_common as common
import review_solutions
import solve_open_problems
import triage_open_problems
from test_open_problem_pipeline import make_analyzed_paper


class MixedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.first = make_analyzed_paper(root / "first")
        self.second = make_analyzed_paper(root / "second")

    def run_main(self, main, argv):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            try:
                code = main([str(value) for value in argv])
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue(), error.getvalue()

    def test_triage_selects_the_union(self):
        code, output, error = self.run_main(
            triage_open_problems.main,
            [self.first, self.second / "OP-002", "--dry-run"],
        )
        self.assertEqual(code, 0, error)
        self.assertIn(f"{self.first} OP-001", output)
        self.assertIn(f"{self.first} OP-002", output)
        self.assertIn(f"{self.second} OP-002", output)
        self.assertNotIn(f"{self.second} OP-001", output)

    def test_folder_next_to_problem_path_also_counts(self):
        problems = common.discover_problem_refs([self.first.parent, self.second])
        selected = common.filter_exact_problems(
            problems,
            {(os.path.normcase(str(self.second.resolve())), "OP-001")},
            [self.first.parent.resolve()],
        )
        self.assertEqual(
            sorted((problem.paper_directory.parent.name, problem.id) for problem in selected),
            [("first", "OP-001"), ("first", "OP-002"), ("second", "OP-001")],
        )

    def test_literature_and_solve_refuse_mixed_paths(self):
        for main, extra in (
            (literature_review.main, ["--dry-run"]),
            (solve_open_problems.main, ["--dry-run"]),
        ):
            with self.subTest(command=main.__module__):
                code, _output, error = self.run_main(
                    main, [self.first, self.second / "OP-002", *extra]
                )
                self.assertNotEqual(code, 0)
                self.assertIn("cannot mix paper or folder paths", error)

    def test_review_selects_the_union(self):
        for paper, problem in ((self.first, "OP-001"), (self.second, "OP-002")):
            attempt = paper / problem / "attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "attempt.md").write_text("# Attempt", encoding="utf-8")
            common.write_json(attempt / "solver-result.json", {
                "claimed_result_type": "partial_result", "summary": "Progress.",
                "checkable_claims": [], "prior_claim_dispositions": [],
                "external_sources": [], "artifacts": [], "warnings": [],
            })
        code, output, error = self.run_main(
            review_solutions.main,
            [self.first, self.second / "OP-002" / "attempt-001", "--mode", "all", "--dry-run"],
        )
        self.assertEqual(code, 0, error)
        self.assertIn(f"{self.first} OP-001", output)
        self.assertIn(f"{self.second} OP-002", output)

    def test_problem_paths_alone_still_work(self):
        code, output, error = self.run_main(
            triage_open_problems.main,
            [self.second / "OP-002", "--dry-run"],
        )
        self.assertEqual(code, 0, error)
        self.assertIn(f"{self.second} OP-002", output)
        self.assertNotIn(f"{self.first}", output)


if __name__ == "__main__":
    unittest.main()

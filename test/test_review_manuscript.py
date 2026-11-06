"""Reviews of existing manuscripts in their own repositories."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_cli
import open_problem_common as common
import review_manuscript as review
from validation import manuscript_review as math_validation
from test_write_paper import write_run_files
from test_write_paper_exposition import PROFILE_FILES, reader_finding, reader_result, write_reader_output


LATEXMK = shutil.which("latexmk") is not None and shutil.which("pdflatex") is not None

MAIN = r"""\documentclass{article}
\usepackage{graphicx}
\usepackage{macros}
\begin{document}
\title{Oriented spanners}\author{A. Author}\maketitle
\input{sections/intro}
% \input{sections/unused}
\includegraphics[width=1cm]{figures/square}
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""


def png() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=T", "-c", "user.email=t@example.org", *arguments],
        capture_output=True, text=True, check=True,
    ).stdout


def make_repository(root: Path) -> Path:
    """A paper repository in its own layout, with things that are not the paper."""
    repository = root / "spanners"
    files = {
        "socg/main.tex": MAIN,
        "socg/macros.sty": "\\RequirePackage{amssymb}\n\\newcommand{\\R}{\\mathbb{R}}\n",
        "socg/sections/intro.tex": "\\section{Introduction}\nSee~\\cite{knuth}; let \\(x \\in \\R\\).\n",
        "socg/sections/unused.tex": "Not part of the paper.\n",
        "socg/refs.bib": "@book{knuth, author={D. Knuth}, title={TAOCP}, year={1968}, publisher={AW}}\n",
        "notes/ideas.md": "# Private notes\n",
        "writing/cg/SKILL.md": PROFILE_FILES["SKILL.md"],
        "writing/cg/references/notation.md": PROFILE_FILES["references/notation.md"],
    }
    for relative, text in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (repository / "socg" / "figures").mkdir()
    (repository / "socg" / "figures" / "square.png").write_bytes(png())
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "paper")
    return repository


def math_result(verdict: str = "needs_major_revision", severity: str = "major") -> dict:
    return {
        "verdict": verdict,
        "summary": "Checked.",
        "statements": [{"label": "Theorem 1", "location": "Section 1", "assessment": "gap",
                        "explanation": "The case k = 2 is not covered."}],
        "findings": [{"id": "M-001", "severity": severity, "category": "proof", "location": "Proof of Theorem 1",
                      "explanation": "The case k = 2 is missing.", "suggested_repair": "Treat k = 2."}],
        "warnings": [],
    }


def write_math_output(workspace: Path, result: dict) -> None:
    (workspace / "math-review.md").write_text(
        "# Mathematical review\n\n" + "".join(f"## {item['id']}\n\n{item['explanation']}\n\n"
                                              for item in result["findings"]),
        encoding="utf-8",
    )
    common.write_json(workspace / "agent-result.json", result)
    write_run_files(workspace)


def staged(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


class ReviewManuscriptTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(codex_cli, "grant_sandbox_read_access")
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repository = make_repository(self.root)
        self.entry = self.repository / "socg" / "main.tex"

    def run_main(self, *arguments: str, fake=None) -> tuple[int, str]:
        out, err = StringIO(), StringIO()
        with (
            patch.object(codex_cli, "run_structured_codex", side_effect=fake),
            patch.object(codex_cli, "codex_runtime", return_value=("codex", "test")),
            redirect_stdout(out), redirect_stderr(err),
        ):
            code = review.main([str(self.entry), *arguments])
        return code, out.getvalue() + err.getvalue()

    def recording_fake(self, seen: dict):
        def fake(**kwargs):
            workspace = kwargs["workspace"]
            schema = Path(kwargs["schema_path"]).name
            kind = "math" if schema.startswith("manuscript-math") else "exposition"
            seen[kind] = {
                "inputs": staged(workspace / "inputs"),
                "prompt": kwargs["prompt"],
                "web_search": kwargs["web_search"],
                "reader": common.load_json(workspace / "inputs" / "reader.json"),
            }
            if kind == "math":
                write_math_output(workspace, math_result())
            else:
                write_reader_output(workspace, reader_result("needs_major_revision", [reader_finding()]))
            return workspace / "agent-result.json"
        return fake

    # -- building and collecting ----------------------------------------------------

    @unittest.skipUnless(LATEXMK, "latexmk and pdflatex are not installed")
    def test_build_reads_exactly_the_manuscript_and_leaves_the_repository_alone(self):
        workspace = self.root / "build"
        build = review.build_manuscript(self.entry, workspace=workspace)
        self.assertEqual(build.status, "ok", build.log_tail)
        self.assertTrue(build.pdf.is_file())
        files = review.manuscript_files(self.entry, self.repository, build, workspace)
        self.assertEqual([path.relative_to(self.repository).as_posix() for path in files], [
            "socg/figures/square.png", "socg/macros.sty", "socg/main.tex",
            "socg/refs.bib", "socg/sections/intro.tex",
        ])
        self.assertEqual(git(self.repository, "status", "--porcelain", "--ignored"), "")

    @unittest.skipUnless(LATEXMK, "latexmk and pdflatex are not installed")
    def test_a_failed_build_falls_back_to_the_existing_pdf(self):
        (self.entry.parent / "sections" / "intro.tex").write_text("\\undefinedcommand\n", encoding="utf-8")
        (self.entry.parent / "main.pdf").write_bytes(b"%PDF-old")
        build = review.build_manuscript(self.entry, pdf=self.entry.with_suffix(".pdf"),
                                        workspace=self.root / "build")
        self.assertEqual(build.status, "failed")
        self.assertEqual(build.pdf, self.entry.with_suffix(".pdf"))
        self.assertIn("may not match", build.note)

    def test_missing_tools_and_project_builds(self):
        pdf = self.entry.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-old")
        missing = review.build_manuscript(self.entry, pdf=pdf, latexmk="no-such-latexmk",
                                          workspace=self.root / "build")
        self.assertEqual((missing.status, missing.pdf), ("missing-tool", pdf))
        self.assertIn("not installed", missing.note)
        failing = review.build_manuscript(
            self.entry, command=[sys.executable, "-c", "raise SystemExit(2)"], pdf=pdf,
            workspace=self.root / "build",
        )
        self.assertEqual(failing.status, "failed")
        project = review.build_manuscript(
            self.entry, command=[sys.executable, "-c", "open('main.pdf', 'wb').write(b'%PDF-new')"],
            pdf=pdf, workspace=self.root / "build",
        )
        self.assertEqual((project.status, pdf.read_bytes()), ("ok", b"%PDF-new"))  # runs next to the entry
        skipped = review.build_manuscript(self.entry, pdf=pdf, skip=True, workspace=self.root / "build")
        self.assertEqual((skipped.status, skipped.pdf), ("skipped", pdf))

    def test_static_collection_without_a_build(self):
        build = review.Build("skipped")
        files = review.manuscript_files(self.entry, self.repository, build, self.root / "build")
        self.assertEqual([path.relative_to(self.repository).as_posix() for path in files], [
            "socg/figures/square.png", "socg/macros.sty", "socg/main.tex",
            "socg/refs.bib", "socg/sections/intro.tex",
        ])

    # -- the reviews --------------------------------------------------------------

    def test_both_reviews_see_their_own_inputs_and_record_the_version(self):
        (self.entry.parent / "main.pdf").write_bytes(b"%PDF-paper")
        (self.repository / "loose-ends.toml").write_text(
            'schema_version = 1\nid = "oriented-spanners"\naudience = "computational geometers"\n'
            'writing_profile = "writing/cg"\n\n[[manuscript]]\nname = "socg"\nentry = "socg/main.tex"\n',
            encoding="utf-8",
        )
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-qm", "describe")
        commit = git(self.repository, "rev-parse", "HEAD").strip()
        intro = self.entry.parent / "sections" / "intro.tex"
        intro.write_text(intro.read_text(encoding="utf-8") + "More.\n", encoding="utf-8")
        notes = self.root / "attempt.md"
        notes.write_text("# Attempt\n\nThe k = 2 case.\n", encoding="utf-8")
        seen: dict = {}
        code, output = self.run_main("--no-build", "--context", str(notes), fake=self.recording_fake(seen))
        self.assertEqual(code, 0, output)

        paper = {f"manuscript/socg/{name}" for name in (
            "main.tex", "macros.sty", "refs.bib", "sections/intro.tex", "figures/square.png", "main.pdf",
        )}
        self.assertEqual(seen["math"]["inputs"], paper | {"reviewer.json", "context/01-attempt.md"})
        self.assertEqual(seen["exposition"]["inputs"], paper | {
            "reader.json", *(f"writing-profile/{path}" for path in PROFILE_FILES),
        })
        self.assertEqual((seen["math"]["web_search"], seen["exposition"]["web_search"]), ("live", "disabled"))
        self.assertIn("manuscript/socg/main.tex", seen["math"]["prompt"])
        self.assertIn("context/", seen["math"]["prompt"])
        self.assertIn("computational geometers", seen["exposition"]["prompt"])
        self.assertEqual(seen["exposition"]["reader"]["entry"], "manuscript/socg/main.tex")
        for kind in ("math", "exposition"):
            self.assertNotIn("{{", seen[kind]["prompt"])

        directories = sorted((self.repository / "manuscript-reviews").iterdir())
        self.assertEqual([path.name.split("-")[3:5] for path in directories],
                         [["socg", "exposition"], ["socg", "math"]])
        math = next(path for path in directories if "-math-" in path.name)
        manifest = common.read_json(math / "manifest.json")
        self.assertEqual(manifest["revision"]["commit"], commit)
        self.assertEqual(manifest["revision"]["uncommitted"], ["socg/sections/intro.tex"])
        self.assertEqual(manifest["project"], "oriented-spanners")
        self.assertEqual(manifest["manuscript"], {"name": "socg", "entry": "socg/main.tex"})
        self.assertEqual(manifest["build"]["status"], "skipped")
        self.assertTrue(manifest["pdf_reviewed"])
        self.assertEqual(manifest["content_digest"], review.content_digest(manifest["files"]))
        self.assertEqual(manifest["context"][0]["path"], None)  # outside the repository
        self.assertEqual(manifest["context"][0]["staged"], "context/01-attempt.md")
        self.assertIsNone(manifest["writing_profile"])
        exposition = common.read_json(directories[0] / "manifest.json")
        self.assertEqual(exposition["context"], [])
        self.assertEqual(exposition["audience"], "computational geometers")
        self.assertEqual(sorted(item["path"] for item in exposition["writing_profile"]["files"]), sorted(PROFILE_FILES))
        self.assertTrue((math / "math-review.md").is_file())
        self.assertEqual(common.read_json(math / "review.json")["verdict"], "needs_major_revision")
        # Only the reviews are new; their run logs are ignored, no profile copy.
        status = git(self.repository, "status", "--porcelain", "--untracked-files=all")
        new = sorted(line[3:] for line in status.splitlines() if line.startswith("??"))
        self.assertTrue(all(path.startswith("manuscript-reviews/") for path in new), new)
        self.assertFalse(any("/logs/" in path for path in new))
        self.assertFalse(any("writing-profile" in path for path in new))
        self.assertTrue((math / "logs" / "events.jsonl").is_file())

    def test_status_follows_the_files(self):
        (self.entry.parent / "main.pdf").write_bytes(b"%PDF-paper")
        code, output = self.run_main("--no-build", "--math", fake=self.recording_fake({}))
        self.assertEqual(code, 0, output)
        [row] = review.review_status(self.repository, self.entry)
        self.assertTrue(row["current"])
        self.assertEqual((row["kind"], row["verdict"], row["findings"]), ("math", "needs_major_revision", 1))
        (self.entry.parent / "refs.bib").write_text("@misc{x}\n", encoding="utf-8")
        [row] = review.review_status(self.repository, self.entry)
        self.assertEqual((row["current"], row["changed"]), (False, ["socg/refs.bib"]))
        code, output = self.run_main("--status")
        self.assertEqual(code, 0)
        self.assertIn("outdated: socg/refs.bib", output)
        self.assertEqual(review.review_status(self.repository, self.repository / "other.tex"), [])

    def test_invalid_reviews_are_not_installed(self):
        (self.entry.parent / "main.pdf").write_bytes(b"%PDF-paper")

        def fake(**kwargs):
            result = math_result()
            result["findings"][0]["id"] = "X-001"
            write_math_output(kwargs["workspace"], result)
            return kwargs["workspace"] / "agent-result.json"

        code, output = self.run_main("--no-build", "--math", fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("M-001", output)
        self.assertFalse((self.repository / "manuscript-reviews").exists())

    def test_math_validator_rules(self):
        workspace = self.root / "workspace"
        workspace.mkdir()

        def issues(result: dict, markdown: bool = True) -> set[str]:
            write_math_output(workspace, result)
            if not markdown:
                (workspace / "math-review.md").write_text("# Review\n", encoding="utf-8")
            return {issue.code for issue in math_validation.validate(workspace=workspace, expectations={}).issues}

        self.assertEqual(issues(math_result()), set())
        self.assertIn("E_CLEAN_CONSISTENCY", issues(math_result("no_issues_found")))
        self.assertIn("E_MAJOR_CONSISTENCY", issues(math_result(severity="minor")))
        self.assertIn("E_INCORRECT_CONSISTENCY", issues(math_result("incorrect")))
        self.assertIn("E_FINDING_MARKDOWN", issues(math_result(), markdown=False))

    def test_review_directories_never_collide(self):
        reviews = self.root / "reviews"
        with patch.object(review.secrets, "token_hex", side_effect=["aaaaaa", "aaaaaa", "bbbbbb"]):
            first = review.review_directory(reviews, "socg", "math", "codex")
            first.mkdir(parents=True)
            second = review.review_directory(reviews, "socg", "math", "claude")
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("-socg-math-claude-aaaaaa"))
            second.mkdir()
            third = review.review_directory(reviews, "socg", "math", "claude")
            self.assertTrue(third.name.endswith("-bbbbbb"))

    def test_dry_run_and_errors(self):
        code, output = self.run_main("--no-build", "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("5 file(s)", output)
        self.assertIn("Would run: math, exposition", output)
        code, output = self.run_main("--build", "make", "--no-build")
        self.assertEqual(code, 1)
        self.assertIn("exclude each other", output)
        code, output = self.run_main("--context", str(self.root / "missing.md"))
        self.assertEqual(code, 1)
        self.assertIn("context not found", output)


if __name__ == "__main__":
    unittest.main()

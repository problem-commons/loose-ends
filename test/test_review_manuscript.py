"""Reviews of existing manuscripts in their own repositories."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
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
import source_references
import workbench_projects
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


def make_pool(root: Path) -> Path:
    """A pool checkout with a hosted remote whose main branch has the commit."""
    pool = root / "pool"
    attempt = pool / "papers" / "own" / "arXiv-1" / "OP-001" / "attempt-002"
    attempt.mkdir(parents=True)
    (attempt / "solution.md").write_text("# Solution\n\nCase k = 2.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(pool)], check=True)
    git(pool, "add", ".")
    git(pool, "commit", "-qm", "attempt")
    git(pool, "remote", "add", "origin", "https://x-access-token:secret@github.com/problem-commons/pool.git")
    git(pool, "update-ref", "refs/remotes/origin/main", "HEAD")
    return pool


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
        pool = make_pool(self.root)
        attempt = pool / "papers" / "own" / "arXiv-1" / "OP-001" / "attempt-002"
        notes = self.root / "attempt.md"
        notes.write_text("# Attempt\n\nThe k = 2 case.\n", encoding="utf-8")
        seen: dict = {}
        code, output = self.run_main("--no-build", "--context", str(attempt), "--context", str(notes),
                                     fake=self.recording_fake(seen))
        self.assertEqual(code, 0, output)
        self.assertIn("context attempt-002: github.com/problem-commons/pool:papers/own/arXiv-1/OP-001/attempt-002@", output)
        self.assertIn("context attempt.md is not in a git checkout", output)
        self.assertIn("the writing profile is in a checkout without a hosted remote", output)

        paper = {f"manuscript/socg/{name}" for name in (
            "main.tex", "macros.sty", "refs.bib", "sections/intro.tex", "figures/square.png", "main.pdf",
        )}
        self.assertEqual(seen["math"]["inputs"], paper | {
            "reviewer.json", "context/01-attempt-002/solution.md", "context/02-attempt.md",
        })
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
        pool_commit = git(pool, "rev-parse", "HEAD").strip()
        from_pool, loose = manifest["context"]
        self.assertEqual(from_pool["source"], {
            "repository": "github.com/problem-commons/pool",
            "path": "papers/own/arXiv-1/OP-001/attempt-002",
            "commit": pool_commit, "committed": True, "pushed": True,
        })
        [solution] = from_pool["files"]
        self.assertEqual((solution["staged"], solution["path"], solution["committed"]),
                         ("context/01-attempt-002/solution.md",
                          "papers/own/arXiv-1/OP-001/attempt-002/solution.md", True))
        # No path of this machine: a file outside any checkout has its hash only.
        self.assertEqual(loose["source"]["path"], None)
        self.assertEqual(loose["files"][0]["path"], None)
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual(manifest["revision"]["path"], ".")
        self.assertIsNone(manifest["writing_profile"])
        exposition = common.read_json(directories[0] / "manifest.json")
        self.assertEqual(exposition["context"], [])
        self.assertEqual(exposition["audience"], "computational geometers")
        self.assertEqual(sorted(item["path"] for item in exposition["writing_profile"]["files"]), sorted(PROFILE_FILES))
        self.assertEqual(exposition["writing_profile"]["origin"]["path"], "writing/cg")
        self.assertEqual(exposition["writing_profile"]["origin"]["commit"], commit)
        self.assertNotIn(str(self.root), json.dumps(exposition))
        rows = {row["kind"]: row for row in review.review_status(self.repository, self.entry)}
        self.assertTrue(rows["math"]["context"][0].startswith("github.com/problem-commons/pool:papers/"))
        self.assertEqual(rows["exposition"]["context"], [])
        self.assertTrue(rows["exposition"]["writing_profile"].startswith("cg "))
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

    def test_writing_profile_precedence(self):
        default = self.root / "personal"
        default.mkdir()
        (default / "SKILL.md").write_text("# Personal\n", encoding="utf-8")
        code, output = self.run_main("--no-build", "--dry-run", "--default-writing-profile", str(default),
                                     "--default-writing-profile-exclude", "agents/*")
        self.assertEqual(code, 0, output)
        self.assertIn(f"writing profile: {default}", output)
        self.assertIn("(leaving out agents/*)", output)
        # The project's loose-ends.toml outranks the personal default.
        (self.repository / "loose-ends.toml").write_text(
            'schema_version = 1\nwriting_profile = "writing/cg"\n', encoding="utf-8")
        code, output = self.run_main("--no-build", "--dry-run", "--default-writing-profile", str(default))
        self.assertIn("writing/cg", output)
        self.assertNotIn(str(default), output)
        code, output = self.run_main("--no-build", "--dry-run", "--no-writing-profile")
        self.assertNotIn("writing profile:", output)
        code, output = self.run_main("--no-build", "--dry-run", "--no-writing-profile",
                                     "--writing-profile", str(default))
        self.assertEqual(code, 1)
        self.assertIn("exclude each other", output)

    # -- references and project folders ------------------------------------------

    def test_references_name_repository_path_and_commit(self):
        for url, expected in [
            ("https://github.com/problem-commons/pool.git", "github.com/problem-commons/pool"),
            ("https://x-access-token:secret@github.com/o/r", "github.com/o/r"),
            ("git@github.com:o/r.git", "github.com/o/r"),
            ("ssh://git@GitHub.com:22/o/r.git/", "github.com/o/r"),
            ("/home/me/r", None), ("../r", None), ("file:///home/me/r", None), ("", None),
        ]:
            self.assertEqual(source_references.normalize_remote(url), expected, url)
        pool = make_pool(self.root)
        attempt = pool / "papers" / "own" / "arXiv-1" / "OP-001" / "attempt-002"
        self.assertEqual(source_references.reference(attempt)["committed"], True)
        self.assertEqual(source_references.warnings("x", source_references.reference(attempt)), [])
        (attempt / "draft.md").write_text("new\n", encoding="utf-8")
        (pool / ".gitignore").write_text("papers/**/inputs/\n", encoding="utf-8")
        (attempt / "inputs").mkdir()
        (attempt / "inputs" / "paper.tex").write_text("x\n", encoding="utf-8")
        origin, states = source_references.describe(
            attempt, [attempt / "solution.md", attempt / "draft.md", attempt / "inputs" / "paper.tex"]
        )
        self.assertFalse(origin["committed"])
        self.assertEqual([state["committed"] for state in states], [True, False, False])
        self.assertIn("uncommitted", " ".join(source_references.warnings("x", origin)))
        git(pool, "add", "papers")
        git(pool, "commit", "-qm", "more")
        later = source_references.reference(attempt / "draft.md")
        self.assertEqual((later["committed"], later["pushed"]), (True, False))
        self.assertIn("not pushed", " ".join(source_references.warnings("x", later)))
        # Windows line ends in the checkout still match the commit.
        (attempt / "solution.md").write_bytes((attempt / "solution.md").read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(source_references.reference(attempt / "solution.md")["committed"])
        # A deleted committed file makes its folder uncommitted.
        (attempt / "draft.md").unlink()
        self.assertFalse(source_references.reference(attempt)["committed"])
        quick = source_references.reference(pool, status=False)
        self.assertEqual((quick["committed"], quick["repository"]), (None, "github.com/problem-commons/pool"))

    def test_project_folders_inside_and_outside_checkouts(self):
        # A notes folder with its own loose-ends.toml inside a larger checkout.
        notes = self.repository / "lemmas"
        notes.mkdir()
        (notes / "loose-ends.toml").write_text("schema_version = 1\n", encoding="utf-8")
        (notes / "lemma.tex").write_text("\\documentclass{article}\n\\begin{document}x\\end{document}\n",
                                         encoding="utf-8")
        self.assertEqual(review.find_repository(notes / "lemma.tex"), notes)
        self.assertEqual(review.find_repository(self.entry), self.repository)
        state = review.git_state(notes, [notes / "lemma.tex"])
        self.assertEqual((state["path"], state["uncommitted"]), ("lemmas", ["lemma.tex"]))
        # A folder that is no git checkout.
        loose = self.root / "math-notes"
        loose.mkdir()
        (loose / "a.tex").write_text("x\n", encoding="utf-8")
        self.assertEqual(review.find_repository(loose / "a.tex"), loose)
        self.assertFalse(review.git_state(loose, [loose / "a.tex"])["git"])
        code, output = self.run_main("--repository", str(notes), "--status")
        self.assertEqual(code, 1)
        self.assertIn("is not inside", output)

    def test_scanning_folders(self):
        import scan_folders

        notes = self.root / "math-notes"
        (notes / "2024" / "spanners" / "drafts").mkdir(parents=True)
        (notes / "2024" / "spanners" / "drafts" / "lemma.tex").write_text(
            "\\documentclass{article}\n\\begin{document}x\\end{document}\n", encoding="utf-8")
        (notes / "ideas.md").write_text("# Ideas\n", encoding="utf-8")
        out = StringIO()
        with redirect_stdout(out):
            code = scan_folders.main([str(self.repository), str(notes)])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("manuscript socg: socg/main.tex, no PDF, 0 review(s)", text)
        self.assertIn("1 more LaTeX document(s) too deep to be found: 2024/spanners/drafts/lemma.tex", text)
        self.assertIn("its notes can also be --context", text)
        self.assertIn(f'papers = ["{self.repository}"]', text)
        out = StringIO()
        with redirect_stdout(out):
            scan_folders.main([str(notes), "--toml"])
        description = out.getvalue()
        (notes / "loose-ends.toml").write_text(description, encoding="utf-8")
        [manuscript] = review.workbench_projects.read_paper_project(notes)["manuscripts"]
        self.assertEqual(manuscript.entry, "2024/spanners/drafts/lemma.tex")
        # A folder of projects: the projects inside are registered, not the folder.
        collection = self.root / "collection"
        (collection / "Two watchmen" / "paper").mkdir(parents=True)
        (collection / "Two watchmen" / "paper" / "main.tex").write_text(
            "\\documentclass{article}\n", encoding="utf-8")
        (collection / "Two watchmen" / "loose-ends.toml").write_text("schema_version = 1\n", encoding="utf-8")
        out = StringIO()
        with redirect_stdout(out):
            scan_folders.main([str(collection), str(collection / "Two watchmen")])
        text = out.getvalue()
        self.assertIn("projects inside (own loose-ends.toml): Two watchmen", text)
        self.assertIn("folder of projects", text)
        self.assertIn(f'papers = ["{collection / "Two watchmen"}"]', text)

    @unittest.skipUnless(LATEXMK, "latexmk and pdflatex are not installed")
    def test_build_resolves_paths_as_in_the_project(self):
        # A bibliography in a sibling folder and \\include of a subfolder fail
        # when latexmk writes to an output directory; the copy builds them.
        project = self.root / "Unlabeled motion planning"
        (project / "paper" / "sections").mkdir(parents=True)
        (project / "related-work").mkdir()
        (project / "paper" / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n\\include{sections/intro}\n"
            "\\bibliographystyle{plain}\n\\bibliography{../related-work/references}\n\\end{document}\n",
            encoding="utf-8")
        (project / "paper" / "sections" / "intro.tex").write_text("See~\\cite{knuth}.\n", encoding="utf-8")
        (project / "related-work" / "references.bib").write_text(
            "@book{knuth, author={D. Knuth}, title={TAOCP}, year={1968}, publisher={AW}}\n", encoding="utf-8")
        (project / "data.npz").write_bytes(b"not copied")
        entry = project / "paper" / "main.tex"
        build = review.build_manuscript(entry, workspace=self.root / "build", repository=project)
        self.assertEqual(build.status, "ok", build.log_tail)
        self.assertFalse((self.root / "build" / "build" / "data.npz").exists())
        files = review.manuscript_files(entry, project, build, self.root / "build")
        self.assertEqual([path.relative_to(project).as_posix() for path in files],
                         ["paper/main.tex", "paper/sections/intro.tex", "related-work/references.bib"])
        self.assertEqual(sorted(path.name for path in project.rglob("*")),
                         sorted(["paper", "sections", "related-work", "main.tex", "intro.tex",
                                 "references.bib", "data.npz"]))

    def test_reviews_folder_and_context_from_the_description(self):
        (self.entry.parent / "main.pdf").write_bytes(b"%PDF-paper")
        (self.repository / "STATUS.md").write_text("# Status\n\nTheorem 1 awaits review.\n", encoding="utf-8")
        (self.repository / "proofs").mkdir()
        (self.repository / "proofs" / "theorem.md").write_text("# Proof\n", encoding="utf-8")
        (self.repository / "reviews").mkdir()
        (self.repository / "reviews" / "2026-11-04-audit-claude.md").write_text("# Audit\n", encoding="utf-8")
        (self.repository / "reviews" / "older").mkdir()
        common.write_json(self.repository / "reviews" / "older" / "manifest.json", {"schema_version": 1})
        (self.repository / "loose-ends.toml").write_text(
            'schema_version = 1\nreviews = "reviews"\ncontext = ["STATUS.md", "proofs"]\n\n'
            '[[manuscript]]\nentry = "socg/main.tex"\n', encoding="utf-8")
        seen: dict = {}
        code, output = self.run_main("--no-build", "--math", fake=self.recording_fake(seen))
        self.assertEqual(code, 0, output)
        self.assertTrue({"context/01-STATUS.md", "context/02-proofs/theorem.md"} <= seen["math"]["inputs"])
        [directory] = [path for path in (self.repository / "reviews").iterdir() if "-socg-math-" in path.name]
        [row] = review.review_status(self.repository, self.entry, "reviews")
        self.assertEqual(row["directory"], f"reviews/{directory.name}")
        self.assertFalse((self.repository / "manuscript-reviews").exists())
        # --context replaces the described context.
        notes = self.root / "notes.md"
        notes.write_text("# Notes\n", encoding="utf-8")
        code, output = self.run_main("--no-build", "--math", "--context", str(notes),
                                     fake=self.recording_fake(seen))
        self.assertEqual(code, 0, output)
        self.assertEqual({path for path in seen["math"]["inputs"] if path.startswith("context/")},
                         {"context/01-notes.md"})
        (self.repository / "loose-ends.toml").write_text(
            'schema_version = 1\ncontext = ["gone.md"]\n', encoding="utf-8")
        code, output = self.run_main("--no-build", "--math", fake=self.recording_fake(seen))
        self.assertEqual(code, 1)
        self.assertIn("context not found", output)

    def test_names_from_folders_with_spaces(self):
        self.assertEqual(workbench_projects.default_name("Uncertain Polygons/paper/main.tex"),
                         "Uncertain-Polygons-paper")
        self.assertEqual(workbench_projects.slug("KB_ min-sum motion planning"), "KB_-min-sum-motion-planning")


if __name__ == "__main__":
    unittest.main()

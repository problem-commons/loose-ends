"""Writing profiles, the manuscript-only exposition reviewer, and the editor round."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_cli
import open_problem_common as common
from validation import exposition_review as exposition_validation
from validation import common as validation_common
import write_paper
from test_write_paper import (
    make_paper,
    make_ready_attempt,
    paper_result,
    write_manuscript_files,
    write_run_files,
)


OPTIONS = codex_cli.ModelOptions("test-model", "high", False)
PROFILE_FILES = {
    "SKILL.md": "# Writing\n\nDefine every symbol before use.\n",
    "references/notation.md": "# Notation\n\nUse \\(n\\) for the number of points.\n",
}


def make_profile(root: Path) -> Path:
    profile = root / "computational-geometry-writing"
    for relative, text in PROFILE_FILES.items():
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # Not writing criteria: scripts, binaries, version control.
    (profile / "scripts").mkdir()
    (profile / "scripts" / "check.py").write_text("print('x')\n", encoding="utf-8")
    (profile / "figure.png").write_bytes(b"\x89PNG\r\n")
    (profile / ".git").mkdir()
    (profile / ".git" / "config.md").write_text("[core]\n", encoding="utf-8")
    return profile


def critic_result(verdict: str, findings: list[dict]) -> dict:
    return {
        "verdict": verdict,
        "summary": "Reviewed.",
        "result_reviews": [
            {"result_id": "R-001", "assessment": "supported", "explanation": "Correct."}
        ],
        "findings": findings,
        "recommended_action": "revise" if findings and verdict != "ready_for_expert_review" else "human_review",
        "warnings": [],
    }


def critic_finding(finding_id: str = "P-001") -> dict:
    return {
        "id": finding_id,
        "severity": "minor",
        "category": "exposition",
        "location": "Proof of the main theorem",
        "result_ids": ["R-001"],
        "source_claim_ids": ["C-001"],
        "explanation": "One step is terse.",
        "suggested_repair": "Expand the step.",
        "requires_new_research": False,
    }


def reader_finding(finding_id: str = "X-001", severity: str = "major") -> dict:
    return {
        "id": finding_id,
        "severity": severity,
        "category": "definitions",
        "location": "Section 2",
        "explanation": "The term 'oriented spanner' is used before it is defined.",
        "suggested_repair": "Define it in the introduction.",
        "criterion": "SKILL.md: define every symbol before use",
    }


def reader_result(verdict: str, findings: list[dict]) -> dict:
    return {"verdict": verdict, "summary": "Read.", "findings": findings, "warnings": []}


def write_reader_output(workspace: Path, result: dict) -> None:
    (workspace / "exposition-review.md").write_text(
        "# Exposition review\n\n"
        + "".join(f"## {finding['id']}\n\n{finding['explanation']}\n\n" for finding in result["findings"])
        + ("No findings.\n" if not result["findings"] else ""),
        encoding="utf-8",
    )
    common.write_json(workspace / "agent-result.json", result)
    write_run_files(workspace)


def fake_compile(workspace, latexmk):
    (workspace / "build.log").write_text("Build complete.\n", encoding="utf-8")
    (workspace / "main.pdf").write_bytes(b"%PDF-paper")
    return workspace / "main.pdf"


def staged_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


class ExpositionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(codex_cli, "grant_sandbox_read_access")
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def settings(self) -> write_paper.ExpositionSettings:
        return write_paper.ExpositionSettings(
            prompt_template=write_paper.DEFAULT_EXPOSITION_PROMPT_PATH.read_text(encoding="utf-8"),
            schema_path=write_paper.DEFAULT_EXPOSITION_SCHEMA_PATH,
            config_digest="exposition-config",
            options=OPTIONS,
        )

    def run_pipeline(self, fake, *, max_rounds: int, profile=None, exposition=True, previous=None):
        paper = make_paper(self.root / "papers")
        attempt = make_ready_attempt(paper, "OP-001")
        inputs = write_paper.load_paper_inputs([attempt])
        manuscript = self.root / "manuscripts" / "test"
        manuscript.mkdir(parents=True, exist_ok=True)
        if profile is not None:
            profile = write_paper.load_writing_profile(profile, manuscript)
        with (
            patch.object(codex_cli, "run_structured_codex", side_effect=fake),
            patch.object(write_paper, "compile_latex", side_effect=fake_compile),
            redirect_stdout(StringIO()),
        ):
            outcome = write_paper.run_pipeline(
                manuscript,
                inputs,
                selectors=write_paper.input_selectors([attempt]),
                previous=previous,
                authors=["Test Author"],
                title_hint=None,
                max_rounds=max_rounds,
                codex="codex",
                codex_version="test",
                latexmk="latexmk",
                prompt_template=write_paper.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"),
                schema_path=write_paper.DEFAULT_SCHEMA_PATH,
                config_digest="writer-config",
                options=OPTIONS,
                review_prompt_template=write_paper.DEFAULT_REVIEW_PROMPT_PATH.read_text(encoding="utf-8"),
                review_schema_path=write_paper.DEFAULT_REVIEW_SCHEMA_PATH,
                review_config_digest="review-config",
                review_options=OPTIONS,
                writing_profile=profile,
                audience="computational geometers" if exposition or profile else None,
                exposition=self.settings() if exposition else None,
            )
        return outcome, manuscript, inputs

    # -- profile loading --------------------------------------------------------

    def test_profile_is_loaded_as_a_versioned_copy(self):
        source = make_profile(self.root)
        manuscript = self.root / "manuscripts" / "test"
        manuscript.mkdir(parents=True)
        profile = write_paper.load_writing_profile(source, manuscript)
        self.assertEqual([path for path, _, _ in profile.files], sorted(PROFILE_FILES))
        self.assertEqual(profile.name, "computational-geometry-writing")
        self.assertEqual(profile.directory.parent, manuscript / "writing-profiles")
        self.assertTrue(profile.directory.name.endswith(profile.digest[:12]))
        self.assertEqual(staged_files(profile.directory), set(PROFILE_FILES))
        self.assertEqual(write_paper.read_writing_profile(source)[1], profile.digest)
        # Loading the same profile again reuses its copy.
        self.assertEqual(write_paper.load_writing_profile(source, manuscript).directory, profile.directory)
        record = profile.record(manuscript)
        self.assertEqual(record["copy"], f"writing-profiles/{profile.directory.name}")
        self.assertEqual(len(record["files"][0]["sha256"]), 64)
        self.assertEqual(write_paper.stored_writing_profile(record, manuscript).digest, profile.digest)

        # A changed profile is a new version; the old copy stays.
        (source / "SKILL.md").write_text("# Writing\n\nShorter sentences.\n", encoding="utf-8")
        changed = write_paper.load_writing_profile(source, manuscript)
        self.assertNotEqual(changed.digest, profile.digest)
        self.assertTrue(profile.directory.is_dir())
        # A stored copy that no longer matches its digest is refused.
        (profile.directory / "SKILL.md").write_text("edited\n", encoding="utf-8")
        with self.assertRaisesRegex(common.CodexError, "no longer matches"):
            write_paper.stored_writing_profile(record, manuscript)
        with self.assertRaisesRegex(common.CodexError, "invalid writing-profile copy"):
            write_paper.stored_writing_profile({**record, "copy": "../elsewhere"}, manuscript)

    def test_single_file_and_empty_profiles(self):
        single = self.root / "style.md"
        single.write_text("# Style\n", encoding="utf-8")
        self.assertEqual(write_paper.read_writing_profile(single)[2][0][0], "style.md")
        (self.root / "empty").mkdir()
        with self.assertRaisesRegex(common.CodexError, "no text files"):
            write_paper.read_writing_profile(self.root / "empty")
        with self.assertRaisesRegex(common.CodexError, "not found"):
            write_paper.read_writing_profile(self.root / "absent")

    def test_profile_reaches_writer_and_critic_and_nothing_changes_without_it(self):
        manuscript = self.root / "manuscripts" / "test"
        manuscript.mkdir(parents=True)
        profile = write_paper.load_writing_profile(make_profile(self.root), manuscript)
        paper = make_paper(self.root / "papers")
        inputs = write_paper.load_paper_inputs([make_ready_attempt(paper, "OP-001")])
        with_profile = write_paper.stage_paper_context(self.root / "a", inputs, writing_profile=profile)
        self.assertTrue((with_profile / "writing-profile" / "SKILL.md").is_file())
        self.assertEqual(common.read_json(with_profile / "index.json")["writing_profile"]["digest"], profile.digest)
        plain = write_paper.stage_paper_context(self.root / "b", inputs)
        self.assertNotIn("writing_profile", common.read_json(plain / "index.json"))
        self.assertFalse((plain / "writing-profile").exists())

        template = write_paper.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
        arguments = {"context": plain, "authors": [], "title_hint": None, "previous": None}
        default = write_paper.render_writer_prompt(template, **arguments)
        self.assertNotIn("writing profile", default)
        self.assertNotIn("audience", default)
        styled = write_paper.render_writer_prompt(
            template, **arguments, writing_profile=profile, audience="computational geometers"
        )
        self.assertIn("`inputs/writing-profile/`", styled)
        self.assertIn("`references/notation.md`", styled)
        self.assertIn("Start with `inputs/writing-profile/SKILL.md`", styled)
        self.assertIn("cannot ask the user", styled)
        self.assertIn("Write for this audience: computational geometers", styled)
        review_template = write_paper.DEFAULT_REVIEW_PROMPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("writing profile", write_paper.render_reviewer_prompt(review_template, context=plain))
        self.assertIn(
            "inputs/writing-profile/",
            write_paper.render_reviewer_prompt(review_template, context=plain, writing_profile=profile),
        )
        self.assertEqual(write_paper._with_extras("digest", writing_profile=None, audience=None), "digest")
        self.assertNotEqual(write_paper._with_extras("digest", writing_profile=profile.digest), "digest")

    # -- reviewer input separation ----------------------------------------------

    def test_exposition_reviewer_sees_only_the_manuscript(self):
        manuscript = self.root / "manuscripts" / "test"
        draft_directory = manuscript / "draft-001"
        draft_directory.mkdir(parents=True)
        write_manuscript_files(draft_directory, ["R-001"])
        (draft_directory / "figures").mkdir()
        (draft_directory / "figures" / "construction.pdf").write_bytes(b"%PDF-figure")
        (draft_directory / "code").mkdir()
        (draft_directory / "code" / "verify.py").write_text("print(1)\n", encoding="utf-8")
        for name in ("paper-result.json", "manifest.json", "paper-review.json"):
            common.write_json(draft_directory / name, {"private": name})
        (draft_directory / "paper-critique.md").write_text("# Critique\n\nP-001\n", encoding="utf-8")
        draft = write_paper.DraftRef(draft_directory, 1, {})
        profile = write_paper.load_writing_profile(make_profile(self.root), manuscript)
        seen = {}

        def fake(**kwargs):
            workspace = kwargs["workspace"]
            seen["workspace"] = workspace
            seen["inputs"] = staged_files(workspace / "inputs")
            seen["prompt"] = kwargs["prompt"]
            seen["web_search"] = kwargs["web_search"]
            write_reader_output(workspace, reader_result("needs_major_revision", [reader_finding()]))
            return workspace / "agent-result.json"

        with patch.object(codex_cli, "run_structured_codex", side_effect=fake):
            review = write_paper.run_exposition_review(
                manuscript, draft, codex="codex", codex_version="test",
                settings=self.settings(), audience="computational geometers",
                writing_profile=profile,
            )
        self.assertEqual(seen["inputs"], {
            "manuscript/main.tex", "manuscript/references.bib", "manuscript/main.pdf",
            "manuscript/figures/construction.pdf", "reader.json",
            *(f"writing-profile/{path}" for path in PROFILE_FILES),
        })
        self.assertFalse(seen["workspace"].is_relative_to(manuscript))
        self.assertFalse(seen["workspace"].exists())  # cleaned up
        self.assertIn("computational geometers", seen["prompt"])
        self.assertIn("`writing-profile/SKILL.md`", seen["prompt"])
        self.assertNotIn("{{", seen["prompt"])
        self.assertEqual(seen["web_search"], "disabled")
        self.assertEqual(review.result["verdict"], "needs_major_revision")
        for name in ("exposition-review.md", "exposition-review.json", "exposition-review-manifest.json",
                     "exposition-events.jsonl", "exposition-run.log"):
            self.assertTrue((draft_directory / name).is_file(), name)
        manifest = common.read_json(draft_directory / "exposition-review-manifest.json")
        self.assertEqual(manifest["writing_profile"]["digest"], profile.digest)
        self.assertEqual(manifest["staged_files"], sorted(seen["inputs"]))
        self.assertTrue(write_paper.exposition_review_is_current(draft, config_digest="exposition-config"))
        self.assertFalse(write_paper.exposition_review_is_current(draft, config_digest="other"))
        (draft_directory / "main.tex").write_text("changed\n", encoding="utf-8")
        self.assertFalse(write_paper.exposition_review_is_current(draft, config_digest="exposition-config"))

    def test_exposition_validator_checks_verdicts_and_report(self):
        cases = [
            (reader_result("clear", [reader_finding()]), "E_CLEAR_CONSISTENCY"),
            (reader_result("needs_major_revision", [reader_finding(severity="minor")]), "E_MAJOR_CONSISTENCY"),
            (reader_result("needs_minor_revision", []), "E_MINOR_CONSISTENCY"),
            (reader_result("clear", [reader_finding("P-001", "minor")]), "E_FINDING_ID"),
        ]
        for index, (result, code) in enumerate(cases):
            workspace = self.root / f"case-{index}"
            workspace.mkdir()
            write_reader_output(workspace, result)
            report = exposition_validation.validate(workspace=workspace, expectations={})
            self.assertIn(code, {issue.code for issue in report.issues}, code)
        workspace = self.root / "valid"
        workspace.mkdir()
        write_reader_output(workspace, reader_result("needs_minor_revision", [reader_finding(severity="minor")]))
        (workspace / "exposition-review.md").write_text("# Review\n\nNothing numbered.\n", encoding="utf-8")
        report = exposition_validation.validate(workspace=workspace, expectations={})
        self.assertEqual({issue.code for issue in report.issues}, {"E_FINDING_MARKDOWN"})
        self.assertTrue(issubclass(type(report), validation_common.ValidationReport))

    # -- revision handoff ---------------------------------------------------------

    def test_editor_revision_addresses_both_reports_and_minor_reader_findings_do_not_block(self):
        calls = {"writer": 0, "critic": 0, "reader": 0}
        source = make_profile(self.root)

        def fake(**kwargs):
            workspace = kwargs["workspace"]
            name = kwargs["schema_path"].name
            if name == "open-problem-paper.schema.json":
                calls["writer"] += 1
                write_manuscript_files(workspace, ["R-001"])
                self.assertTrue((workspace / "inputs" / "writing-profile" / "SKILL.md").is_file())
                addressed = []
                if calls["writer"] == 2:
                    staged = workspace / "inputs" / "manuscript"
                    self.assertTrue((staged / "paper-review.json").is_file())
                    self.assertTrue((staged / "exposition-review.json").is_file())
                    self.assertIn("X-###", kwargs["prompt"])
                    expectations = json.loads(
                        (workspace / "validation" / validation_common.EXPECTATIONS_FILENAME).read_text(encoding="utf-8")
                    )
                    self.assertEqual(expectations["previous_finding_ids"], ["P-001", "X-001"])
                    with (workspace / "readiness.md").open("a", encoding="utf-8") as report:
                        report.write("\n## P-001\n\nExpanded.\n\n## X-001\n\nDefined early.\n")
                    addressed = [
                        {"finding_id": "P-001", "disposition": "resolved", "explanation": "Expanded."},
                        {"finding_id": "X-001", "disposition": "resolved", "explanation": "Defined early."},
                    ]
                common.write_json(workspace / "agent-result.json", paper_result(["R-001"], addressed=addressed))
                write_run_files(workspace)
            elif name == "open-problem-paper-review.schema.json":
                calls["critic"] += 1
                # The full-context critic keeps its inputs and never sees the reader's report.
                self.assertFalse(list((workspace / "inputs").rglob("exposition-review*")))
                self.assertTrue((workspace / "inputs" / "results" / "R-001" / "attempt" / "attempt.md").is_file())
                if calls["critic"] == 1:
                    result = critic_result("needs_minor_revision", [critic_finding()])
                    critique = "# Critique\n\n## P-001\n\nExpand.\n"
                else:
                    result = critic_result("ready_for_expert_review", [])
                    critique = "# Critique\n\nReady.\n"
                (workspace / "paper-critique.md").write_text(critique, encoding="utf-8")
                common.write_json(workspace / "agent-result.json", result)
                write_run_files(workspace)
            else:
                calls["reader"] += 1
                self.assertFalse((workspace / "inputs" / "manuscript" / "readiness.md").exists())
                if calls["reader"] == 1:
                    result = reader_result("needs_major_revision", [reader_finding()])
                else:
                    # Only minor findings left: they do not keep the draft in revision.
                    result = reader_result("needs_minor_revision", [reader_finding("X-001", "minor")])
                write_reader_output(workspace, result)
            return workspace / "agent-result.json"

        outcome, manuscript, _ = self.run_pipeline(fake, max_rounds=3, profile=source)
        self.assertTrue(outcome.ready)
        self.assertEqual(calls, {"writer": 2, "critic": 2, "reader": 2})
        self.assertEqual(outcome.final_exposition.result["verdict"], "needs_minor_revision")
        self.assertEqual(outcome.final_exposition.draft.directory, manuscript / "draft-002")
        second = common.read_json(manuscript / "draft-002" / "manifest.json")
        self.assertEqual(second["previous_draft"], "draft-001")
        self.assertEqual(second["audience"], "computational geometers")
        self.assertTrue(second["exposition_review"]["enabled"])
        self.assertEqual(second["writing_profile"]["name"], "computational-geometry-writing")
        result = common.read_json(manuscript / "draft-002" / "paper-result.json")
        self.assertEqual([item["finding_id"] for item in result["addressed_findings"]], ["P-001", "X-001"])

    def test_the_editor_must_answer_the_readers_findings(self):
        previous_directory = self.root / "draft-001"
        previous_directory.mkdir()
        common.write_json(previous_directory / "paper-review.json", critic_result("needs_minor_revision", [critic_finding()]))
        common.write_json(previous_directory / "paper-result.json", paper_result(["R-001"]))
        common.write_json(previous_directory / "exposition-review.json", reader_result("needs_major_revision", [reader_finding()]))
        previous = write_paper.DraftRef(previous_directory, 1, {})
        self.assertEqual(write_paper._previous_finding_ids(previous), {"P-001"})
        self.assertEqual(write_paper._previous_finding_ids(previous, exposition=True), {"P-001", "X-001"})
        paper = make_paper(self.root / "papers")
        inputs = write_paper.load_paper_inputs([make_ready_attempt(paper, "OP-001")])
        workspace = self.root / "workspace"
        workspace.mkdir()
        write_manuscript_files(workspace, ["R-001"])
        with (workspace / "readiness.md").open("a", encoding="utf-8") as report:
            report.write("\n## P-001\n\nExpanded.\n")
        common.write_json(workspace / "agent-result.json", paper_result(["R-001"], addressed=[
            {"finding_id": "P-001", "disposition": "resolved", "explanation": "Expanded."},
        ]))
        write_paper.validate_paper_result(workspace / "agent-result.json", workspace, inputs, previous=previous)
        with self.assertRaisesRegex(common.CodexError, "did not address every previous"):
            write_paper.validate_paper_result(
                workspace / "agent-result.json", workspace, inputs, previous=previous, exposition=True
            )

    def test_without_the_options_the_workflow_is_unchanged(self):
        schemas = []

        def fake(**kwargs):
            workspace = kwargs["workspace"]
            schemas.append(kwargs["schema_path"].name)
            if kwargs["schema_path"].name == "open-problem-paper.schema.json":
                self.assertFalse((workspace / "inputs" / "writing-profile").exists())
                self.assertNotIn("writing profile", kwargs["prompt"])
                write_manuscript_files(workspace, ["R-001"])
                common.write_json(workspace / "agent-result.json", paper_result(["R-001"]))
            else:
                (workspace / "paper-critique.md").write_text("# Critique\n\nReady.\n", encoding="utf-8")
                common.write_json(workspace / "agent-result.json", critic_result("ready_for_expert_review", []))
            write_run_files(workspace)
            return workspace / "agent-result.json"

        outcome, manuscript, _ = self.run_pipeline(fake, max_rounds=2, exposition=False)
        self.assertTrue(outcome.ready)
        self.assertIsNone(outcome.final_exposition)
        self.assertEqual(schemas, ["open-problem-paper.schema.json", "open-problem-paper-review.schema.json"])
        manifest = common.read_json(manuscript / "draft-001" / "manifest.json")
        for key in ("writing_profile", "audience", "exposition_review"):
            self.assertNotIn(key, manifest)
        self.assertFalse(list(manuscript.rglob("exposition-review*")))
        self.assertFalse((manuscript / "writing-profiles").exists())

    def test_research_verdicts_skip_the_reader(self):
        calls = {"reader": 0}

        def fake(**kwargs):
            workspace = kwargs["workspace"]
            name = kwargs["schema_path"].name
            if name == "open-problem-paper.schema.json":
                write_manuscript_files(workspace, ["R-001"])
                common.write_json(workspace / "agent-result.json", paper_result(["R-001"]))
            elif name == "open-problem-paper-review.schema.json":
                finding = {**critic_finding(), "requires_new_research": True}
                result = {**critic_result("needs_research", [finding]), "recommended_action": "new_research"}
                (workspace / "paper-critique.md").write_text("# Critique\n\n## P-001\n", encoding="utf-8")
                common.write_json(workspace / "agent-result.json", result)
            else:
                calls["reader"] += 1
            write_run_files(workspace)
            return workspace / "agent-result.json"

        outcome, _, _ = self.run_pipeline(fake, max_rounds=2)
        self.assertEqual(outcome.reason, "needs_research")
        self.assertEqual(calls["reader"], 0)
        self.assertIsNone(outcome.final_exposition)

    def test_a_revision_inherits_the_options_unless_switched_off(self):
        def fake(**kwargs):
            workspace = kwargs["workspace"]
            name = kwargs["schema_path"].name
            if name == "open-problem-paper.schema.json":
                write_manuscript_files(workspace, ["R-001"])
                common.write_json(workspace / "agent-result.json", paper_result(["R-001"]))
                write_run_files(workspace)
            elif name == "open-problem-paper-review.schema.json":
                (workspace / "paper-critique.md").write_text("# Critique\n\n## P-001\n", encoding="utf-8")
                common.write_json(workspace / "agent-result.json", critic_result("needs_minor_revision", [critic_finding()]))
                write_run_files(workspace)
            else:
                write_reader_output(workspace, reader_result("clear", []))
            return workspace / "agent-result.json"

        _, manuscript, _ = self.run_pipeline(fake, max_rounds=1, profile=make_profile(self.root))
        draft = manuscript / "draft-001"

        def dry_run(*extra):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = write_paper.main(["--revise", str(draft), "--dry-run", *extra])
            self.assertEqual(code, 0, errors.getvalue())
            return output.getvalue()

        inherited = dry_run()
        self.assertIn("Writing profile: computational-geometry-writing", inherited)
        self.assertIn("Audience: computational geometers", inherited)
        self.assertIn("Exposition review:", inherited)
        plain = dry_run("--no-writing-profile", "--no-exposition-review")
        self.assertNotIn("Writing profile:", plain)
        self.assertNotIn("Exposition review:", plain)
        with redirect_stderr(StringIO()):
            self.assertNotEqual(
                write_paper.main(["--revise", str(draft), "--dry-run", "--exposition-review", "--no-exposition-review"]),
                0,
            )

    def test_profile_entry_point_and_exclusions(self):
        source = make_profile(self.root)
        (source / "references" / "evaluation.md").write_text("# Evaluate the skill\n", encoding="utf-8")
        (source / "agents").mkdir()
        (source / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
        manuscript = self.root / "manuscripts" / "test"
        manuscript.mkdir(parents=True)
        everything = write_paper.load_writing_profile(source, manuscript)
        self.assertEqual(everything.entry, "SKILL.md")
        self.assertIn("references/evaluation.md", [path for path, _, _ in everything.files])
        trimmed = write_paper.load_writing_profile(
            source, manuscript, exclude=("references/evaluation.md", "agents/*")
        )
        self.assertEqual([path for path, _, _ in trimmed.files], sorted(PROFILE_FILES))
        self.assertNotEqual(trimmed.digest, everything.digest)
        record = trimmed.record(manuscript)
        self.assertEqual(record["excluded"], ["references/evaluation.md", "agents/*"])
        self.assertEqual(record["entry"], "SKILL.md")
        self.assertEqual(write_paper.stored_writing_profile(record, manuscript).excluded, trimmed.excluded)
        reader = write_paper.render_exposition_prompt(
            write_paper.DEFAULT_EXPOSITION_PROMPT_PATH.read_text(encoding="utf-8"),
            context=self.root, audience="geometers", writing_profile=trimmed,
        )
        self.assertIn("Start with `writing-profile/SKILL.md`", reader)
        self.assertIn("not part of your task", reader)
        single = self.root / "style.md"
        single.write_text("# Style\n", encoding="utf-8")
        self.assertEqual(write_paper.load_writing_profile(single, manuscript).entry, "style.md")

    def test_the_editor_decides_reader_findings_on_the_mathematics(self):
        previous = self.root / "draft-001"
        previous.mkdir()
        prompt = write_paper.render_writer_prompt(
            write_paper.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"),
            context=self.root, authors=[], title_hint=None,
            previous=write_paper.DraftRef(previous, 1, {}), exposition_review=True,
        )
        self.assertIn("only when following it would make the manuscript incorrect", prompt)
        self.assertIn("not by itself a reason to reject", prompt)
        self.assertNotIn("findings take precedence", prompt)

    def scripted(self, critic_verdicts, reader_severities, calls):
        """Writer answers every finding; critic and reader follow the scripts."""
        def fake(**kwargs):
            workspace = kwargs["workspace"]
            name = kwargs["schema_path"].name
            if name == "open-problem-paper.schema.json":
                calls["writer"] += 1
                write_manuscript_files(workspace, ["R-001"])
                expectations = json.loads(
                    (workspace / "validation" / validation_common.EXPECTATIONS_FILENAME).read_text(encoding="utf-8")
                )
                ids = expectations["previous_finding_ids"]
                with (workspace / "readiness.md").open("a", encoding="utf-8") as report:
                    report.write("".join(f"\n## {finding_id}\n\nAnswered.\n" for finding_id in ids))
                addressed = [
                    {"finding_id": finding_id, "disposition": "resolved", "explanation": "Done."}
                    for finding_id in ids
                ]
                common.write_json(workspace / "agent-result.json", paper_result(["R-001"], addressed=addressed))
                write_run_files(workspace)
            elif name == "open-problem-paper-review.schema.json":
                verdict = critic_verdicts[calls["critic"]]
                calls["critic"] += 1
                findings = [] if verdict == "ready_for_expert_review" else [critic_finding()]
                (workspace / "paper-critique.md").write_text(
                    "# Critique\n\n" + "".join(f"## {item['id']}\n" for item in findings), encoding="utf-8"
                )
                common.write_json(workspace / "agent-result.json", critic_result(verdict, findings))
                write_run_files(workspace)
            else:
                severity = reader_severities[calls["reader"]]
                calls["reader"] += 1
                if severity is None:
                    result = reader_result("clear", [])
                elif severity == "minor":
                    result = reader_result("needs_minor_revision", [reader_finding("X-001", "minor")])
                else:
                    result = reader_result("needs_major_revision", [reader_finding("X-001", severity)])
                write_reader_output(workspace, result)
            return workspace / "agent-result.json"
        return fake

    def test_serious_reader_findings_keep_a_ready_draft_in_revision(self):
        calls = {"writer": 0, "critic": 0, "reader": 0}
        fake = self.scripted(
            ["ready_for_expert_review", "ready_for_expert_review"], ["major", "minor"], calls
        )
        outcome, manuscript, _ = self.run_pipeline(fake, max_rounds=3)
        self.assertTrue(outcome.ready)
        self.assertEqual(calls, {"writer": 2, "critic": 2, "reader": 2})
        # The critic's own verdict is kept on the first draft.
        first = common.read_json(manuscript / "draft-001" / "paper-review.json")
        self.assertEqual(first["verdict"], "ready_for_expert_review")
        answered = common.read_json(manuscript / "draft-002" / "paper-result.json")["addressed_findings"]
        self.assertEqual([item["finding_id"] for item in answered], ["X-001"])

    def test_the_round_limit_still_holds_with_open_reader_findings(self):
        calls = {"writer": 0, "critic": 0, "reader": 0}
        fake = self.scripted(
            ["ready_for_expert_review", "ready_for_expert_review"], ["blocking", "major"], calls
        )
        outcome, _, _ = self.run_pipeline(fake, max_rounds=2)
        self.assertFalse(outcome.ready)
        self.assertEqual(outcome.reason, write_paper.OPEN_EXPOSITION_REASON)
        self.assertEqual(outcome.final_review.result["verdict"], "ready_for_expert_review")
        self.assertEqual(write_paper.serious_exposition_findings(outcome.final_exposition), ["X-001"])
        self.assertEqual(calls, {"writer": 2, "critic": 2, "reader": 2})

    def test_critic_revision_verdicts_still_drive_rounds_with_a_clear_reader(self):
        calls = {"writer": 0, "critic": 0, "reader": 0}
        fake = self.scripted(
            ["needs_minor_revision", "ready_for_expert_review"], [None, None], calls
        )
        outcome, _, _ = self.run_pipeline(fake, max_rounds=3)
        self.assertTrue(outcome.ready)
        self.assertEqual(calls, {"writer": 2, "critic": 2, "reader": 2})

    def test_revising_a_draft_the_critic_now_finds_ready_continues_for_the_reader(self):
        draft_directory = self.root / "manuscripts" / "test" / "draft-001"
        draft_directory.mkdir(parents=True)
        write_manuscript_files(draft_directory, ["R-001"])
        common.write_json(draft_directory / "paper-result.json", paper_result(["R-001"]))
        common.write_json(draft_directory / "manifest.json", {"schema_version": 1})
        previous = write_paper.DraftRef(draft_directory, 1, paper_result(["R-001"]))
        calls = {"writer": 0, "critic": 0, "reader": 0}
        fake = self.scripted(
            ["ready_for_expert_review", "ready_for_expert_review"], ["major", None], calls
        )
        outcome, manuscript, _ = self.run_pipeline(fake, max_rounds=1, previous=previous)
        self.assertTrue(outcome.ready)
        self.assertEqual(calls, {"writer": 1, "critic": 2, "reader": 2})
        self.assertEqual(outcome.final_review.draft.directory, manuscript / "draft-002")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Review an existing LaTeX manuscript: its mathematics, its exposition, or both.

    python src/review_manuscript.py ~/papers/spanners/socg/main.tex \\
        [--math] [--exposition] [--audience TEXT] [--writing-profile DIR] \\
        [--context NOTES ...] [--build "make paper" | --no-build]

The manuscript stays where it is, in its own layout. The script builds it
(by default `latexmk` into a temporary directory, so the repository is not
touched), collects the files the build actually read (LaTeX's recorder file,
plus the bibliography and figures it names), stages copies of exactly those
files for the reviewer, and records the reviewed version: the files with
their SHA-256 hashes, the repository's commit, and which reviewed files had
uncommitted changes. Each review goes into its own directory below
`manuscript-reviews/` in the repository, named with date, manuscript, kind,
agent and a random suffix so that reviews made on different machines never
collide. `--status` tells, for every review of a manuscript, whether the files
it reviewed have changed since.

- The mathematical reviewer (`--math`) sees the manuscript and any `--context`
  files (research notes, results from a pool) and may search the web.
- The exposition reviewer (`--exposition`) is the manuscript-only reader of
  write_paper.py: the manuscript, its PDF and figures, the audience and the
  writing profile; no context files and, by default, no web search.

Without --math or --exposition, both run. Settings missing on the command line
come from the repository's loose-ends.toml (see workbench_projects.py).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence

import artifact_reporting
import codex_cli
import open_problem_common as common
from validation import exposition_review as exposition_validation
from validation import manuscript_review as math_validation
import workbench_projects
import write_paper


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATH_PROMPT = PROJECT_ROOT / "prompts" / "review-manuscript-mathematics.md"
DEFAULT_MATH_SCHEMA = PROJECT_ROOT / "schemas" / "manuscript-math-review.schema.json"
REVIEWS_DIRECTORY = "manuscript-reviews"
MANIFEST_SCHEMA_VERSION = 1
KINDS = ("math", "exposition")
BUILD_TIMEOUT_SECONDS = 600
# Files a build reads that belong to the manuscript, not to the TeX system.
MANUSCRIPT_SUFFIXES = {
    ".tex", ".bib", ".sty", ".cls", ".bst", ".bbx", ".cbx", ".def", ".cfg",
    ".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg", ".ipe", ".tikz", ".pgf",
    ".dat", ".csv", ".txt",
}
GRAPHICS_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".eps")
INPUT_COMMANDS = re.compile(
    r"\\(input|include|subfile|subfileinclude|includegraphics|includesvg|includepdf|"
    r"bibliography|addbibresource|usepackage|documentclass)\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}"
)


@dataclass
class Build:
    status: str  # ok | failed | missing-tool | skipped
    command: list[str] = field(default_factory=list)
    pdf: Path | None = None
    recorded: list[Path] = field(default_factory=list)
    log_tail: str = ""
    note: str = ""

    def record(self, repository: Path) -> dict:
        return {
            "status": self.status,
            "command": self.command,
            "pdf": _relative_or_none(self.pdf, repository),
            "note": self.note,
            "log_tail": self.log_tail[-4000:],
        }


def _relative_or_none(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None  # a temporary build output


def find_repository(entry: Path) -> Path:
    """The git repository of the manuscript, or its directory."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(entry.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return entry.parent
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return entry.parent


def git_state(repository: Path, files: Sequence[Path]) -> dict:
    """Commit, remote, and the reviewed files with uncommitted changes."""
    def git(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repository), *arguments],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if commit is None:
        return {"git": False, "commit": None, "uncommitted": [], "remote": None}
    relative = [path.relative_to(repository).as_posix() for path in files]
    status = git("status", "--porcelain", "--", *relative) or ""
    changed = sorted({line[3:].strip() for line in status.splitlines() if line.strip()})
    remote = (git("config", "--get", "remote.origin.url") or "").strip() or None
    return {"git": True, "commit": commit.strip(), "uncommitted": changed, "remote": remote}


def settings_for(entry: Path, repository: Path) -> dict:
    """The manuscript's entry in the repository's loose-ends.toml, if any."""
    try:
        shared = workbench_projects.read_paper_project(repository)
    except workbench_projects.ProjectError as exc:
        raise common.CodexError(str(exc)) from exc
    relative = entry.relative_to(repository).as_posix()
    manuscript = next(
        (item for item in shared["manuscripts"] if item.entry == relative), None
    )
    return {
        "project": shared.get("id") or "",
        "name": manuscript.name if manuscript else entry.stem,
        "build": list(manuscript.build) if manuscript else [],
        "pdf": manuscript.pdf_path if manuscript else "",
        "audience": shared.get("audience") or "",
        "writing_profile": shared.get("writing_profile") or "",
    }


def build_manuscript(
    entry: Path,
    *,
    command: Sequence[str] = (),
    pdf: Path | None = None,
    latexmk: str = "latexmk",
    skip: bool = False,
    workspace: Path,
) -> Build:
    """Build the manuscript without writing into its repository (by default)."""
    if skip:
        existing = pdf if pdf and pdf.is_file() else None
        return Build("skipped", pdf=existing, note="not rebuilt (--no-build)")
    if command:
        argv = list(command)
        if shutil.which(argv[0]) is None:
            existing = pdf if pdf and pdf.is_file() else None
            return Build("missing-tool", argv, existing, note=f"{argv[0]} is not installed")
        try:
            completed = subprocess.run(
                argv, cwd=entry.parent, capture_output=True, text=True,
                timeout=BUILD_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            return Build("failed", argv, pdf if pdf and pdf.is_file() else None,
                         note="the build timed out")
        log = (completed.stdout or "") + (completed.stderr or "")
        built = pdf if pdf and pdf.is_file() else None
        recorder = entry.with_suffix(".fls")
        recorded = _recorded_inputs(recorder, entry.parent) if recorder.is_file() else []
        if completed.returncode != 0:
            return Build("failed", argv, built, recorded, log[-4000:],
                         "the project's build failed")
        return Build("ok", argv, built, recorded, log[-4000:])
    executable = shutil.which(latexmk)
    output = workspace / "build"
    output.mkdir(parents=True, exist_ok=True)
    argv = [latexmk, "-pdf", "-interaction=nonstopmode", "-halt-on-error",
            "-file-line-error", "-recorder", f"-outdir={output}", entry.name]
    if executable is None:
        existing = pdf if pdf and pdf.is_file() else None
        return Build("missing-tool", argv, existing,
                     note=f"{latexmk} is not installed; install TeX Live with latexmk")
    try:
        completed = subprocess.run(
            argv, cwd=entry.parent, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return Build("failed", argv, note="the build timed out")
    log = (completed.stdout or "") + (completed.stderr or "")
    built = output / entry.with_suffix(".pdf").name
    recorder = output / entry.with_suffix(".fls").name
    recorded = _recorded_inputs(recorder, entry.parent) if recorder.is_file() else []
    if completed.returncode != 0 or not built.is_file():
        existing = pdf if pdf and pdf.is_file() else None
        return Build("failed", argv, existing, recorded, log[-4000:],
                     "latexmk failed" + ("; using the existing PDF, which may not match the sources"
                                         if existing else ""))
    return Build("ok", argv, built, recorded, log[-4000:])


def _recorded_inputs(recorder: Path, directory: Path) -> list[Path]:
    files = []
    for line in recorder.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("INPUT "):
            path = Path(line[6:].strip())
            files.append((path if path.is_absolute() else directory / path).resolve())
    return files


def _named_inputs(entry: Path, repository: Path) -> list[Path]:
    """Files the sources name: included sources, bibliographies, figures, local packages."""
    found: list[Path] = []
    queue = [entry]
    seen: set[Path] = set()
    while queue:
        source = queue.pop()
        if source in seen or not source.is_file():
            continue
        seen.add(source)
        found.append(source)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = re.sub(r"(?<!\\)%.*", "", text)
        base = entry.parent
        for command, argument in INPUT_COMMANDS.findall(text):
            for name in (part.strip() for part in argument.split(",")):
                if not name:
                    continue
                candidates: list[Path] = []
                path = base / name
                if command in ("input", "include", "subfile", "subfileinclude"):
                    candidates = [path, path.with_name(path.name + ".tex")]
                elif command in ("bibliography", "addbibresource"):
                    candidates = [path, path.with_name(path.name + ".bib")]
                elif command in ("usepackage", "documentclass"):
                    suffix = ".sty" if command == "usepackage" else ".cls"
                    candidates = [path.with_name(path.name + suffix)]
                else:
                    candidates = [path, *(path.with_name(path.name + ext) for ext in GRAPHICS_SUFFIXES)]
                for candidate in candidates:
                    candidate = candidate.resolve()
                    if candidate.is_file() and _inside(candidate, repository):
                        if candidate.suffix == ".tex":
                            queue.append(candidate)
                        elif candidate not in found:
                            found.append(candidate)
                        break
    return found


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def manuscript_files(entry: Path, repository: Path, build: Build, workspace: Path) -> list[Path]:
    """The repository files the manuscript consists of, sorted."""
    files = set(_named_inputs(entry, repository))
    for path in build.recorded:
        if (
            path.is_file() and _inside(path, repository) and not _inside(path, workspace)
            and ".git" not in path.relative_to(repository).parts
            and path.suffix.casefold() in MANUSCRIPT_SUFFIXES
        ):
            files.add(path)
    if build.pdf is not None and _inside(build.pdf, repository):
        files.discard(build.pdf.resolve())  # staged as the compiled PDF instead
    return sorted(files, key=lambda path: path.relative_to(repository).as_posix())


def file_records(files: Sequence[Path], repository: Path) -> list[dict]:
    return [
        {"path": path.relative_to(repository).as_posix(),
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in files
    ]


def content_digest(records: Sequence[dict]) -> str:
    return common.stable_value_digest([{"path": item["path"], "sha256": item["sha256"]} for item in records])


def stage_manuscript(
    workspace: Path, entry: Path, repository: Path, files: Sequence[Path], pdf: Path | None
) -> tuple[Path, str, str | None]:
    """Copy exactly the manuscript's files; return (inputs root, entry, pdf) as staged."""
    root = workspace / "inputs"
    manuscript = root / "manuscript"
    manuscript.mkdir(parents=True, exist_ok=False)
    for path in files:
        target = manuscript / path.relative_to(repository)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    staged_entry = f"manuscript/{entry.relative_to(repository).as_posix()}"
    staged_pdf = None
    if pdf is not None and pdf.is_file():
        staged_pdf = f"manuscript/{entry.relative_to(repository).with_suffix('.pdf').as_posix()}"
        target = root / staged_pdf
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, target)
    return root, staged_entry, staged_pdf


def review_directory(reviews: Path, name: str, kind: str, agent: str) -> Path:
    """A fresh directory whose name no other machine will choose."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    while True:
        directory = reviews / f"{stamp}-{name}-{kind}-{agent}-{secrets.token_hex(3)}"
        if not directory.exists():
            return directory


def _install(
    workspace: Path, destination: Path, markdown: str, result: dict, manifest: dict
) -> None:
    staging = Path(tempfile.mkdtemp(prefix=".review-install-", dir=destination.parent))
    try:
        shutil.copyfile(workspace / markdown, staging / markdown)
        common.write_json(staging / "review.json", result)
        common.write_json(staging / "manifest.json", manifest)
        logs = staging / "logs"
        logs.mkdir()
        for name in ("events.jsonl", "run.log", "repair-events.jsonl", "repair-run.log"):
            if (workspace / name).is_file():
                shutil.copyfile(workspace / name, logs / name)
        # Run logs stay on this machine (as in a pool); the review is shared.
        (staging / ".gitignore").write_text("logs/\n", encoding="utf-8")
        os.replace(staging, destination)
    except OSError as exc:
        raise common.CodexError(
            f"could not install the review; staging preserved at {staging}: {exc}"
        ) from exc


def profile_record(profile: write_paper.WritingProfile) -> dict:
    """The profile's identity; the profile itself is not copied into the repository."""
    return {
        **profile.summary(),
        "excluded": list(profile.excluded),
        "files": [{"path": path, "sha256": digest, "bytes": size} for path, digest, size in profile.files],
    }


@dataclass
class Plan:
    entry: Path
    repository: Path
    settings: dict
    kinds: list[str]
    audience: str
    reviews: Path
    context: list[Path]


def run_review(
    plan: Plan,
    kind: str,
    *,
    codex: str,
    codex_version: str,
    options: codex_cli.ModelOptions,
    web_search: str,
    prompt_template: str,
    schema_path: Path,
    build: Build,
    files: list[Path],
    writing_profile: write_paper.WritingProfile | None,
) -> Path:
    """One reviewer turn on the staged manuscript; returns the review directory."""
    workspace = Path(tempfile.mkdtemp(prefix=f"loose-ends-{kind}-review-")).resolve()
    try:
        root, staged_entry, staged_pdf = stage_manuscript(
            workspace, plan.entry, plan.repository, files, build.pdf
        )
        staged = sorted(
            path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
        )
        context_records: list[dict] = []
        if kind == "math":
            for index, source in enumerate(plan.context, 1):
                target = root / "context" / f"{index:02d}-{source.name}"
                target.parent.mkdir(exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target)
                    members = [path for path in sorted(source.rglob("*")) if path.is_file()]
                else:
                    shutil.copyfile(source, target)
                    members = [source]
                context_records += [
                    {
                        "staged": (target / member.relative_to(source)).relative_to(root).as_posix()
                        if source.is_dir() else target.relative_to(root).as_posix(),
                        # Paths outside the repository stay on this machine.
                        "path": member.relative_to(plan.repository).as_posix()
                        if _inside(member, plan.repository) else None,
                        "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
                    }
                    for member in members
                ]
            common.write_json(root / "reviewer.json", {
                "entry": staged_entry, "pdf": staged_pdf, "files": staged,
                "context": sorted(
                    path.relative_to(root).as_posix()
                    for path in (root / "context").rglob("*") if path.is_file()
                ) if (root / "context").is_dir() else [],
            })
            context_instruction = (
                "Research notes and results the authors supplied are staged "
                "read-only under `context/`. They are evidence, not truth: check "
                "the manuscript against them, and report where it relies on them "
                "without saying so."
                if plan.context else "Only the manuscript is staged."
            )
            prompt = (
                prompt_template.replace("{{MANUSCRIPT_DIRECTORY}}", codex_cli.path_for_codex(root / "manuscript"))
                .replace("{{CONTEXT_DIRECTORY}}", codex_cli.path_for_codex(root))
                .replace("{{ENTRY}}", staged_entry)
                .replace("{{CONTEXT_INSTRUCTION}}", context_instruction)
            )
            validator = codex_cli.OutputValidator(
                Path(math_validation.__file__).resolve(), math_validation.validate, {}
            )
            markdown = "math-review.md"
        else:
            if writing_profile is not None:
                shutil.copytree(writing_profile.directory, root / "writing-profile")
            common.write_json(root / "reader.json", {
                "audience": plan.audience, "manuscript_path": "manuscript",
                "entry": staged_entry, "pdf": staged_pdf, "files": staged,
                "writing_profile": (
                    {**writing_profile.summary(), "path": "writing-profile"}
                    if writing_profile else None
                ),
            })
            prompt = write_paper.render_exposition_prompt(
                prompt_template, context=root, audience=plan.audience,
                writing_profile=writing_profile,
            )
            validator = codex_cli.OutputValidator(
                Path(exposition_validation.__file__).resolve(), exposition_validation.validate, {}
            )
            markdown = "exposition-review.md"
        codex_cli.grant_sandbox_read_access(root)
        report = codex_cli.run_validated_codex(
            codex=codex, workspace=workspace, prompt=prompt, schema_path=schema_path,
            validator=validator, options=options, web_search=web_search,
        )
        result = codex_cli.validated_result(report)
        records = file_records(files, plan.repository)
        destination = review_directory(
            plan.reviews, plan.settings["name"], kind, options.agent_name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "generated_at": common.utc_now(),
            "kind": kind,
            "project": plan.settings.get("project") or None,
            "manuscript": {
                "name": plan.settings["name"],
                "entry": plan.entry.relative_to(plan.repository).as_posix(),
            },
            # What was reviewed: the files and their hashes, and the commit
            # they belong to; uncommitted changes are listed, since a commit
            # alone would not identify them.
            "revision": git_state(plan.repository, files),
            "content_digest": content_digest(records),
            "files": records,
            "pdf_reviewed": staged_pdf is not None,
            "build": build.record(plan.repository),
            "audience": plan.audience if kind == "exposition" else None,
            "writing_profile": (
                profile_record(writing_profile) if writing_profile and kind == "exposition" else None
            ),
            "context": context_records,
            "staged_files": sorted(
                path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
            ),
            "codex_version": codex_version,
            "requested_model": options.model,
            **codex_cli.agent_provenance(options),
            "requested_reasoning_effort": options.reasoning_effort,
            "requested_web_search": web_search,
            "verdict": result["verdict"],
        }
        _install(workspace, destination, markdown, result, manifest)
    except (common.CodexError, OSError) as exc:
        raise common.CodexError(common.preserved_workspace_message(exc, workspace)) from exc
    common.cleanup_workspace(workspace)
    artifact_reporting.report_artifacts([destination / markdown, destination / "review.json"])
    return destination


def review_status(repository: Path, entry: Path | None = None) -> list[dict]:
    """Every review below manuscript-reviews/, and whether its files changed since."""
    reviews = repository / REVIEWS_DIRECTORY
    rows = []
    relative_entry = entry.relative_to(repository).as_posix() if entry else None
    for manifest_path in sorted(reviews.glob("*/manifest.json")) if reviews.is_dir() else []:
        manifest = common.load_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            continue
        if relative_entry and manifest.get("manuscript", {}).get("entry") != relative_entry:
            continue
        changed, missing = [], []
        for record in manifest.get("files", []):
            path = repository / record.get("path", "")
            if not path.is_file():
                missing.append(record.get("path"))
            elif hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
                changed.append(record.get("path"))
        result = common.load_json(manifest_path.parent / "review.json") or {}
        rows.append({
            "directory": manifest_path.parent.relative_to(repository).as_posix(),
            "kind": manifest.get("kind"),
            "manuscript": manifest.get("manuscript", {}).get("name"),
            "entry": manifest.get("manuscript", {}).get("entry"),
            "verdict": result.get("verdict") or manifest.get("verdict"),
            "agent": manifest.get("requested_agent"),
            "model": manifest.get("requested_model"),
            "generated_at": manifest.get("generated_at"),
            "commit": (manifest.get("revision") or {}).get("commit"),
            "uncommitted": (manifest.get("revision") or {}).get("uncommitted", []),
            "current": not changed and not missing,
            "changed": changed,
            "missing": missing,
            "findings": len(result.get("findings", []) or []),
            "build": (manifest.get("build") or {}).get("status"),
            "report": f"{manifest_path.parent.relative_to(repository).as_posix()}/"
                      f"{'math-review.md' if manifest.get('kind') == 'math' else 'exposition-review.md'}",
        })
    rows.sort(key=lambda row: str(row.get("generated_at") or ""))
    return rows


def manuscript_overview(repository: Path, manuscript: workbench_projects.Manuscript) -> dict:
    """What the workbench shows of a repository's manuscript: PDF and reviews."""
    entry = repository / manuscript.entry
    pdf = repository / manuscript.pdf_path
    overview = {
        **manuscript.record(),
        "entryPath": str(entry),
        "exists": entry.is_file(),
        "pdfPath": str(pdf),
        "pdfState": "missing",
        "reviews": [],
    }
    if not entry.is_file():
        return overview
    if pdf.is_file():
        sources = _named_inputs(entry, repository)
        newest = max((path.stat().st_mtime for path in sources), default=0.0)
        # Not proof that the PDF matches, but a PDF older than a source does not.
        overview["pdfState"] = "current" if pdf.stat().st_mtime >= newest else "older than its sources"
    overview["reviews"] = [
        {**row, "report": str(repository / row["report"])} for row in review_status(repository, entry)
    ]
    return overview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="review an existing LaTeX manuscript in its own repository",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("entry", type=Path, help="the manuscript's LaTeX entry file")
    parser.add_argument("--math", action="store_true", help="review the mathematics")
    parser.add_argument("--exposition", action="store_true",
                        help="review the exposition, as a reader with only the manuscript")
    parser.add_argument("--status", action="store_true",
                        help="list the manuscript's reviews and whether they are current")
    parser.add_argument("--audience", help="intended readers (exposition review)")
    parser.add_argument("--writing-profile", type=Path, metavar="PATH",
                        help="writing criteria for the exposition review")
    parser.add_argument("--writing-profile-exclude", action="append", default=[], metavar="PATTERN")
    parser.add_argument("--context", action="append", type=Path, default=[], metavar="PATH",
                        help="notes or results the mathematical reviewer may use; repeatable")
    parser.add_argument("--build", metavar="COMMAND",
                        help="the project's build command, run next to the entry file")
    parser.add_argument("--no-build", action="store_true", help="use the existing PDF, if any")
    parser.add_argument("--pdf", type=Path, help="the compiled PDF (default: the entry as .pdf)")
    parser.add_argument("--latexmk", default="latexmk")
    parser.add_argument("--output-dir", type=Path,
                        help=f"parent of the review directories (default: {REVIEWS_DIRECTORY}/ "
                        "in the repository)")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--dry-run", action="store_true",
                        help="build, collect the files, and report, without a review")
    codex_cli.add_prompt_arguments(parser, default_template=DEFAULT_MATH_PROMPT,
                                   task="mathematical reviewer", prefix="math")
    codex_cli.add_prompt_arguments(parser, default_template=write_paper.DEFAULT_EXPOSITION_PROMPT_PATH,
                                   task="exposition reviewer", prefix="exposition")
    codex_cli.add_model_arguments(parser)
    codex_cli.add_web_search_argument(parser, default="live")
    codex_cli.add_model_arguments(parser, prefix="exposition")
    parser.add_argument("--exposition-web-search", choices=codex_cli.WEB_SEARCH_MODES,
                        default="disabled", metavar="MODE",
                        help="web search for the exposition reviewer (default: disabled)")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        entry = args.entry.expanduser().resolve()
        if not entry.is_file() or entry.suffix != ".tex":
            raise common.CodexError(f"not a LaTeX file: {entry}")
        repository = find_repository(entry)
        if args.status:
            rows = review_status(repository, entry)
            if not rows:
                print(f"No reviews of {entry.relative_to(repository)} yet.")
            for row in rows:
                state = "current" if row["current"] else (
                    "outdated: " + ", ".join(row["changed"] + [f"{path} (missing)" for path in row["missing"]])
                )
                print(f"{row['directory']}: {row['kind']} {row['verdict']} "
                      f"({row['agent']}/{row['model'] or 'default'}, {row['findings']} finding(s)); {state}")
            return 0
        settings = settings_for(entry, repository)
        kinds = [kind for kind in KINDS if getattr(args, kind)] or list(KINDS)
        if args.build and args.no_build:
            raise common.CodexError("--build and --no-build exclude each other")
        command = shlex.split(args.build) if args.build else settings["build"]
        pdf = (
            args.pdf.expanduser().resolve() if args.pdf
            else (repository / settings["pdf"]) if settings["pdf"]
            else entry.with_suffix(".pdf")
        )
        audience = args.audience or settings["audience"] or write_paper.DEFAULT_AUDIENCE
        profile_source = (
            args.writing_profile.expanduser() if args.writing_profile
            else repository / settings["writing_profile"] if settings["writing_profile"]
            else None
        )
        context = [path.expanduser().resolve() for path in args.context]
        for path in context:
            if not path.exists():
                raise common.CodexError(f"context not found: {path}")
        reviews = (args.output_dir.expanduser().resolve() if args.output_dir
                   else repository / REVIEWS_DIRECTORY)
        options = codex_cli.model_options_from_args(args)
        exposition_options = codex_cli.inherit_model_options(
            options, codex_cli.model_options_from_args(args, prefix="exposition")
        )
    except (common.CodexError, OSError) as exc:
        return codex_cli.report_error(parser, exc)

    plan = Plan(entry, repository, settings, kinds, audience, reviews, context)
    workspace = Path(tempfile.mkdtemp(prefix="loose-ends-manuscript-build-"))
    try:
        build = build_manuscript(entry, command=command, pdf=pdf, latexmk=args.latexmk,
                                 skip=args.no_build, workspace=workspace)
        files = manuscript_files(entry, repository, build, workspace)
        print(f"Manuscript {settings['name']}: {entry.relative_to(repository)} in {repository}")
        print(f"  build: {build.status}" + (f" ({build.note})" if build.note else ""))
        if build.status == "failed" and build.log_tail:
            print("  " + "\n  ".join(build.log_tail.strip().splitlines()[-8:]))
        print(f"  {len(files)} file(s): " + ", ".join(path.relative_to(repository).as_posix() for path in files[:12])
              + (" …" if len(files) > 12 else ""))
        print("  PDF: " + ("yes" if build.pdf else "none; the reviewers read the sources only"))
        state = git_state(repository, files)
        if state["git"]:
            print(f"  commit {state['commit'][:12]}"
                  + (f"; uncommitted: {', '.join(state['uncommitted'])}" if state["uncommitted"] else ""))
        if args.dry_run:
            print(f"Would run: {', '.join(kinds)} review(s) into {reviews}")
            return 0
        if not files:
            raise common.CodexError("no manuscript files found")
        codex, codex_version = codex_cli.codex_runtime(
            args.codex, *([options] if "math" in kinds else []),
            *([exposition_options] if "exposition" in kinds else []),
        )
        profile = None
        if "exposition" in kinds and profile_source is not None:
            profile = write_paper.load_writing_profile(
                profile_source, workspace, args.writing_profile_exclude
            )
        made = []
        for kind in kinds:
            template_path = (args.math_prompt_template if kind == "math"
                             else args.exposition_prompt_template)
            template = codex_cli.with_user_prompt(
                template_path.expanduser().read_text(encoding="utf-8"),
                args.math_prompt if kind == "math" else args.exposition_prompt,
                task=f"{kind} reviewer",
                option_name=f"--{kind}-prompt",
            )
            directory = run_review(
                plan, kind, codex=codex, codex_version=codex_version,
                options=options if kind == "math" else exposition_options,
                web_search=args.web_search if kind == "math" else args.exposition_web_search,
                prompt_template=template,
                schema_path=DEFAULT_MATH_SCHEMA if kind == "math" else write_paper.DEFAULT_EXPOSITION_SCHEMA_PATH,
                build=build, files=files, writing_profile=profile,
            )
            verdict = common.read_json(directory / "review.json").get("verdict")
            print(f"Completed {kind} review: {directory} ({verdict})", flush=True)
            made.append(directory)
    except (common.CodexError, OSError, UnicodeError) as exc:
        print(f"Manuscript review failed: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print("Commit the review directories to share them; their logs/ stay local.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

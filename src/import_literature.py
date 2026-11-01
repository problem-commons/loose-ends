#!/usr/bin/env python3
"""Install a literature search done outside the pipeline as the problem's report.

    python src/import_literature.py PAPER/OP-ID --from DIR --person NAME \\
        --agent claude [--model MODEL] [--context context.json] [--force] [--dry-run]

DIR holds `agent-result.json` (one entry for the problem) and
`literature-OP-###.md` in the literature search's format (see
`export_problem_context.py --task literature`). They are validated exactly like
a pipeline result and installed as `literature.md`, `literature.json` and
`literature-manifest.json`, so triage, solving and the workbench use the report
like any other; the manifest records where it came from. A current report is
only replaced with `--force`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import codex_cli
import export_problem_context
import literature_review
import open_problem_common as common
from validation import literature as literature_validation


def validate_outside_literature(
    problem: common.ProblemRef,
    source: Path,
) -> tuple[dict, list[str]]:
    if not (source / "agent-result.json").is_file():
        return {}, [f"{source} has no agent-result.json"]
    with tempfile.TemporaryDirectory(prefix="loose-ends-literature-import-") as temporary:
        workspace = Path(temporary)
        schema = literature_review.write_literature_schema(
            literature_review.DEFAULT_SCHEMA_PATH, workspace, [problem]
        )
        for name in ("agent-result.json", f"literature-{problem.id}.md"):
            if (source / name).is_file():
                shutil.copyfile(source / name, workspace / name)
        report = literature_validation.validate(
            workspace=workspace,
            expectations={
                "problem_ids": [problem.id],
                "result_schema": json.loads(schema.read_text(encoding="utf-8")),
            },
        )
    return report.result or {}, [issue.render() for issue in report.issues]


def check_context(problem: common.ProblemRef, context: dict | None) -> list[str]:
    if context is None:
        return []
    if context.get("task") != "literature":
        return ["the context is not a literature export (--task literature)"]
    if (
        context.get("problem_id") != problem.id
        or context.get("paper_directory") != problem.paper_directory.name
    ):
        return [f"the context is for {context.get('paper_directory')}/{context.get('problem_id')}"]
    if context.get("input_digest") != common.literature_input_digest(problem):
        return ["the problem changed after the export (re-analysis?); export it again"]
    return []


def install_outside_literature(
    problem: common.ProblemRef,
    source: Path,
    root: dict,
    *,
    origin: dict,
) -> None:
    entry = next(item for item in root["literature"] if item["problem_id"] == problem.id)
    problem.directory.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".literature-install-", dir=problem.directory))
    agents, models = origin["agents"], origin["models"]
    try:
        shutil.copyfile(source / f"literature-{problem.id}.md",
                        staging / common.LITERATURE_MARKDOWN)
        common.write_json(staging / common.LITERATURE_RESULT, {
            **entry,
            "paper_title": problem.paper_title,
            "searched_at": common.utc_now(),
            "run_status": root["status"],
            "run_warnings": root["warnings"],
        })
        common.write_json(staging / common.LITERATURE_MANIFEST, {
            "schema_version": common.LITERATURE_MANIFEST_SCHEMA_VERSION,
            "generated_at": common.utc_now(),
            "input_digest": common.literature_input_digest(problem),
            "config_digest": None,
            "analysis_digest": common.analysis_digest(problem.paper_directory),
            "codex_version": None,
            "requested_model": models[0] if len(models) == 1 else None,
            "requested_agent": agents[0] if len(agents) == 1 else "mixed",
            "origin": origin,
            "resolution_status": entry["resolution_status"],
            "confidence": entry["confidence"],
            "source_count": len(entry["sources"]),
        })
        for name in common.LITERATURE_RUN_FILES:
            (problem.directory / name).unlink(missing_ok=True)
        for name in (common.LITERATURE_MARKDOWN, common.LITERATURE_RESULT,
                     common.LITERATURE_MANIFEST):
            os.replace(staging / name, problem.directory / name)
    except OSError as exc:
        raise common.CodexError(
            f"could not install the literature report; staging preserved at {staging}: {exc}"
        ) from exc
    shutil.rmtree(staging)
    common.report_artifacts(
        problem.directory / name
        for name in (common.LITERATURE_MARKDOWN, common.LITERATURE_RESULT)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("problem", type=Path, help="PAPER/OP-ID path")
    parser.add_argument("--from", dest="directory", type=Path, required=True,
                        help="directory with agent-result.json and literature-OP-###.md")
    parser.add_argument("--person", dest="people", action="append", required=True)
    parser.add_argument("--agent", dest="agents", action="append", required=True)
    parser.add_argument("--model", dest="models", action="append", default=[])
    parser.add_argument("--context", type=Path,
                        help="context.json of the literature export the work started from")
    parser.add_argument("--note", help="one line for the manifest")
    parser.add_argument("--force", action="store_true",
                        help="replace a current literature report")
    parser.add_argument("--dry-run", action="store_true", help="validate only")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        problem = export_problem_context.resolve_problem(args.problem)
        source = args.directory.expanduser().resolve()
        context = (
            common.read_json(args.context.expanduser(), description="export context")
            if args.context else None
        )
        root, issues = validate_outside_literature(problem, source)
        issues += check_context(problem, context)
        if common.literature_is_current(problem) and not args.force:
            issues.append(f"{problem.id} has a current literature report; pass --force to replace it")
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)
    if issues:
        print(f"Not imported: {len(issues)} problem(s)", file=sys.stderr)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    entry = root["literature"][0]
    summary = (f"{entry['resolution_status']} ({entry['confidence']} confidence, "
               f"{len(entry['sources'])} source(s))")
    if args.dry_run:
        print(f"Valid literature report for {problem.id}: {summary}")
        return 0
    origin = {"kind": "interactive", "people": args.people, "agents": args.agents,
              "models": args.models}
    if args.note:
        origin["note"] = args.note
    try:
        install_outside_literature(problem, source, root, origin=origin)
    except (common.CodexError, OSError) as exc:
        return codex_cli.report_error(parser, exc)
    print(f"Installed the literature report for {problem.id}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

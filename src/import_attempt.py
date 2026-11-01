#!/usr/bin/env python3
"""Install work done outside the solver as a problem's next attempt.

    python src/import_attempt.py PAPER/OP-ID --from DIR --origin interactive \\
        --person NAME --agent claude --model opus [--context context.json]

DIR holds `attempt.md` and `agent-result.json` (`solver-result.json` is
accepted too) in the solver's format, plus the `artifacts/` the result lists.
The result is validated exactly like a solver result and installed as the
next `attempt-NNN`, so critics, triage, later solve runs and the workbench
treat it like any other attempt.  Its manifest records where it came from:
an interactive session (`--origin interactive`) or a separate project
(`--origin external`, with `--source URL@COMMIT`), the people, agents and
models involved.

With `--context` (the `context.json` of export_problem_context.py), the
import stops when attempts were added or reviewed after the export, because
the new snapshot could not reconcile them.
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
import open_problem_common as common
import solve_open_problems
from validation import solver as solver_validation


ORIGINS = ("interactive", "external")
RESULT_FILES = ("agent-result.json", "solver-result.json")


def find_result(source: Path) -> Path:
    for name in RESULT_FILES:
        if (source / name).is_file():
            return source / name
    raise common.CodexError(
        f"{source} has no {' or '.join(RESULT_FILES)}"
    )


def validate_outside_attempt(
    problem: common.ProblemRef,
    source: Path,
) -> tuple[dict, list[str]]:
    """Validate an attempt directory with the solver's own validator."""
    result_path = find_result(source)
    schema = json.loads(
        solve_open_problems.DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory(prefix="loose-ends-import-") as temporary:
        workspace = Path(temporary)
        if (source / "attempt.md").is_file():
            shutil.copyfile(source / "attempt.md", workspace / "attempt.md")
        shutil.copyfile(result_path, workspace / "agent-result.json")
        if (source / "artifacts").is_dir():
            shutil.copytree(source / "artifacts", workspace / "artifacts")
        report = solver_validation.validate(
            workspace=workspace,
            expectations={
                "prior_claim_refs": solve_open_problems._prior_claim_refs(problem),
                "result_schema": schema,
            },
        )
    issues = [issue.render() for issue in report.issues]
    return report.result or {}, issues


def check_context(
    problem: common.ProblemRef,
    context: dict | None,
    *,
    allow_new_history: bool,
) -> list[str]:
    """Problems with the base the outside work started from."""
    if context is None:
        return []
    problems = []
    if context.get("problem_id") != problem.id or context.get(
        "paper_directory"
    ) != problem.paper_directory.name:
        problems.append(
            f"the context is for {context.get('paper_directory')}/"
            f"{context.get('problem_id')}, not {problem.paper_directory.name}/"
            f"{problem.id}"
        )
    elif (
        context.get("attempt_history_digest")
        != common.attempt_history_digest(problem)
        and not allow_new_history
    ):
        now = [directory.name for directory in common.attempt_directories(problem)]
        added = [name for name in now if name not in context.get("attempts", [])]
        detail = (
            f"added since the export: {', '.join(added)}"
            if added
            else "earlier attempts were reviewed or changed since the export"
        )
        problems.append(
            f"the attempt history changed ({detail}); export the context again "
            "and reconcile it, or pass --allow-new-history"
        )
    return problems


def origin_record(args: argparse.Namespace) -> dict:
    record = {
        "kind": args.origin,
        "people": args.people,
        "agents": args.agents,
        "models": args.models,
    }
    if args.source:
        record["source"] = args.source
    if args.session:
        record["session"] = args.session
    if args.note:
        record["note"] = args.note
    return record


def install_outside_attempt(
    problem: common.ProblemRef,
    source: Path,
    result: dict,
    *,
    origin: dict,
    context: dict | None,
) -> Path:
    """Install the validated attempt like a solver run would."""
    number = common.next_attempt_number(problem)
    destination = problem.directory / f"attempt-{number:03d}"
    if destination.exists():
        raise common.CodexError(f"attempt destination already exists: {destination}")
    agents = origin["agents"]
    models = origin["models"]
    staging = Path(
        tempfile.mkdtemp(prefix=".attempt-install-", dir=problem.directory)
    )
    try:
        shutil.copyfile(source / "attempt.md", staging / "attempt.md")
        common.write_json(staging / "solver-result.json", result)
        manifest = {
            "schema_version": 3,
            "generated_at": common.utc_now(),
            "problem_id": problem.id,
            "problem_digest": common.problem_digest(problem),
            "analysis_digest": common.analysis_digest(problem.paper_directory),
            "prior_attempt_history_digest": (
                context or {}
            ).get("attempt_history_digest") or common.attempt_history_digest(problem),
            "triage_snapshot_digest": (context or {}).get("triage_snapshot_digest"),
            "literature_snapshot_digest": (context or {}).get(
                "literature_snapshot_digest"
            ),
            "research_guidance": (context or {}).get("research_guidance"),
            "config_digest": None,
            "codex_version": None,
            "requested_model": models[0] if len(models) == 1 else None,
            # Read by tools that compare solver and reviewer vendors.
            "requested_agent": agents[0] if len(agents) == 1 else "mixed",
            "origin": origin,
            "claimed_result_type": result["claimed_result_type"],
            "claim_count": len(result["checkable_claims"]),
            "attempt_semantics": "cumulative_snapshot",
            "prior_claim_disposition_count": len(
                result["prior_claim_dispositions"]
            ),
        }
        common.write_json(staging / "manifest.json", manifest)
        for relative in result.get("artifacts", []):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        os.replace(staging, destination)
    except OSError as exc:
        raise common.CodexError(
            f"could not install the attempt; staging preserved at {staging}: {exc}"
        ) from exc
    common.report_artifacts(
        [
            destination / "attempt.md",
            destination / "solver-result.json",
            *(destination / relative for relative in result.get("artifacts", [])),
        ]
    )
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("problem", type=Path, help="PAPER/OP-ID path")
    parser.add_argument(
        "--from", dest="directory", type=Path, required=True,
        help="directory with attempt.md and agent-result.json",
    )
    parser.add_argument("--origin", choices=ORIGINS, required=True)
    parser.add_argument(
        "--person", dest="people", action="append", required=True,
        help="person responsible for the work; repeat for several",
    )
    parser.add_argument(
        "--agent", dest="agents", action="append", required=True,
        help="agent vendor used (claude, codex, human, ...); repeat for several",
    )
    parser.add_argument(
        "--model", dest="models", action="append", default=[],
        help="model used, e.g. opus or gpt-6.1-sol; repeat for several",
    )
    parser.add_argument(
        "--context", type=Path,
        help="context.json of the export the work started from",
    )
    parser.add_argument(
        "--source",
        help="for external work: repository URL@commit of the imported state",
    )
    parser.add_argument("--session", help="where the session's notes live")
    parser.add_argument("--note", help="one line for the manifest")
    parser.add_argument(
        "--allow-new-history", action="store_true",
        help="import although attempts were added or reviewed after the export",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and report only"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.origin == "external" and not args.source:
            raise common.CodexError("external attempts need --source URL@COMMIT")
        problem = export_problem_context.resolve_problem(args.problem)
        directory = args.directory.expanduser().resolve()
        if not directory.is_dir():
            raise common.CodexError(f"{directory} is not a directory")
        context = (
            common.read_json(args.context.expanduser(), description="export context")
            if args.context
            else None
        )
        result, issues = validate_outside_attempt(problem, directory)
        issues += check_context(
            problem, context, allow_new_history=args.allow_new_history
        )
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)
    if issues:
        print(f"Not imported: {len(issues)} problem(s)", file=sys.stderr)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    next_name = f"attempt-{common.next_attempt_number(problem):03d}"
    summary = (
        f"{result['claimed_result_type']}; "
        f"{len(result['checkable_claims'])} claim(s); "
        f"{len(result['prior_claim_dispositions'])} prior claim disposition(s)"
    )
    if context is None:
        print(
            "warning: no --context; the import cannot check that the work "
            "reconciles every earlier attempt",
            file=sys.stderr,
        )
    if args.dry_run:
        print(f"Valid: would install {problem.id}/{next_name} ({summary})")
        return 0
    try:
        destination = install_outside_attempt(
            problem,
            directory,
            result,
            origin=origin_record(args),
            context=context,
        )
    except (common.CodexError, OSError) as exc:
        return codex_cli.report_error(parser, exc)
    print(f"Installed {destination} ({summary})")
    print(
        "Next: review it with another agent (review_solutions.py); triage of "
        "this problem is now stale and can suggest next directions."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

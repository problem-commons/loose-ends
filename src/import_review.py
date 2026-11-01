#!/usr/bin/env python3
"""Validate a review written outside the pipeline and file it.

    python src/import_review.py PAPER/OP-ID/attempt-NNN --from DIR \\
        --output REVIEW-DIRECTORY --person NAME --agent claude [--model MODEL] \\
        [--context context.json] [--dry-run]

DIR holds `critique.md` and `agent-result.json` in the critic's format (see
`export_problem_context.py --task review`). The result is validated exactly
like a critic result, for exactly the attempt's claims, and written to
REVIEW-DIRECTORY as `critique.md`, `review-result.json` and
`review-manifest.json`. The attempt's own critique in the paper tree is never
replaced: an outside review is an additional review, kept where the project
keeps such reviews. With `--context`, the import stops when the attempt changed
after the export.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

import codex_cli
import export_problem_context
import open_problem_common as common
import review_solutions
from validation import solution_review as solution_review_validation


def validate_outside_review(
    attempt: review_solutions.AttemptRef,
    source: Path,
) -> tuple[dict, list[str]]:
    if not (source / "agent-result.json").is_file():
        return {}, [f"{source} has no agent-result.json"]
    claims = attempt.solver_result.get("checkable_claims") or []
    with tempfile.TemporaryDirectory(prefix="loose-ends-review-import-") as temporary:
        workspace = Path(temporary)
        schema = review_solutions.write_attempt_review_schema(
            review_solutions.DEFAULT_SCHEMA_PATH, workspace, attempt
        )
        for name in ("critique.md", "agent-result.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, workspace / name)
        report = solution_review_validation.validate(
            workspace=workspace,
            expectations={
                "claim_ids": [claim["id"] for claim in claims if isinstance(claim, dict)],
                "claimed_result_type": common.claimed_result_type(attempt.solver_result),
                "result_schema": json.loads(schema.read_text(encoding="utf-8")),
            },
        )
    return report.result or {}, [issue.render() for issue in report.issues]


def check_context(attempt: review_solutions.AttemptRef, context: dict | None) -> list[str]:
    if context is None:
        return []
    if context.get("task") != "review":
        return ["the context is not a review export (--task review)"]
    if (
        context.get("problem_id") != attempt.problem.id
        or context.get("attempt") != attempt.name
        or context.get("paper_directory") != attempt.problem.paper_directory.name
    ):
        return [
            f"the context is for {context.get('paper_directory')}/"
            f"{context.get('problem_id')}/{context.get('attempt')}"
        ]
    if context.get("attempt_digest") != common.solver_attempt_digest(attempt.directory):
        return ["the attempt changed after the export; export it again and review the new state"]
    return []


def write_review(
    attempt: review_solutions.AttemptRef,
    source: Path,
    output: Path,
    result: dict,
    *,
    origin: dict,
    context: dict | None,
) -> None:
    if output.exists() and any(output.iterdir()):
        raise common.CodexError(f"{output} exists and is not empty")
    output.mkdir(parents=True, exist_ok=True)
    result = {**result, "human_priority": review_solutions.derive_human_priority(result)}
    shutil.copyfile(source / "critique.md", output / "critique.md")
    common.write_json(output / "review-result.json", result)
    agents, models = origin["agents"], origin["models"]
    common.write_json(output / "review-manifest.json", {
        "schema_version": 2,
        "generated_at": common.utc_now(),
        "attempt": f"{attempt.problem.id}/{attempt.name}",
        "attempt_digest": common.solver_attempt_digest(attempt.directory),
        "config_digest": None,
        "codex_version": None,
        "requested_model": models[0] if len(models) == 1 else None,
        "requested_agent": agents[0] if len(agents) == 1 else "mixed",
        "origin": origin,
        "blind": bool((context or {}).get("blind")),
        "correctness": result["correctness"],
        "reviewed_coverage": result["reviewed_coverage"],
        "importance": result["importance"],
        "verification_confidence": result["verification_confidence"],
        "human_priority": result["human_priority"],
        "assessment_semantics": "cumulative_snapshot",
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("attempt", type=Path, help="PAPER/OP-ID/attempt-NNN path")
    parser.add_argument("--from", dest="directory", type=Path, required=True,
                        help="directory with critique.md and agent-result.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="new directory for the filed review")
    parser.add_argument("--person", dest="people", action="append", required=True)
    parser.add_argument("--agent", dest="agents", action="append", required=True)
    parser.add_argument("--model", dest="models", action="append", default=[])
    parser.add_argument("--context", type=Path,
                        help="context.json of the review export the work started from")
    parser.add_argument("--note", help="one line for the manifest")
    parser.add_argument("--dry-run", action="store_true", help="validate only")
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        attempt = export_problem_context.resolve_attempt(args.attempt)
        source = args.directory.expanduser().resolve()
        context = (
            common.read_json(args.context.expanduser(), description="export context")
            if args.context else None
        )
        result, issues = validate_outside_review(attempt, source)
        issues += check_context(attempt, context)
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)
    if issues:
        print(f"Not imported: {len(issues)} problem(s)", file=sys.stderr)
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        return 1
    summary = (f"{result['correctness']}; {result['reviewed_coverage']}, "
               f"{result['importance']} ({result['verification_confidence']} confidence)")
    if args.dry_run:
        print(f"Valid review of {attempt.problem.id}/{attempt.name}: {summary}")
        return 0
    origin = {"kind": "interactive", "people": args.people, "agents": args.agents,
              "models": args.models}
    if args.note:
        origin["note"] = args.note
    try:
        output = args.output.expanduser().resolve()
        write_review(attempt, source, output, result, origin=origin, context=context)
    except (common.CodexError, OSError) as exc:
        return codex_cli.report_error(parser, exc)
    print(f"Filed the review of {attempt.problem.id}/{attempt.name} in {output}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Export the context a pipeline run would get, for work outside the pipeline.

    python src/export_problem_context.py PAPER/OP-ID --output DIR [--replace]
    python src/export_problem_context.py PAPER/OP-ID/attempt-NNN --task review \
        --output DIR [--blind]
    python src/export_problem_context.py PAPER/OP-ID --task literature --output DIR

DIR receives `inputs/` (staged exactly as for the solve run, critic or
literature search), `TASK.md` (that run's instructions), the result schema, a
README, and `context.json`, which records what the export was based on so that
the matching import (import_attempt.py, import_review.py,
import_literature.py) can tell whether the problem changed in the meantime.
Nothing here runs an agent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import codex_cli
import literature_review
import open_problem_common as common
import review_solutions
import solve_open_problems


CONTEXT_FILE = "context.json"
CONTEXT_SCHEMA_VERSION = 1
TASK_FILE = "TASK.md"
SCHEMA_FILE = "agent-result.schema.json"
README_FILE = "README.md"
GENERATED = ("inputs", TASK_FILE, SCHEMA_FILE, README_FILE, CONTEXT_FILE)

TASK_PREAMBLE = """\
<!-- Exported by export_problem_context.py. This is the task a Loose Ends
solve run receives for this problem. Outside the solver, write attempt.md and
agent-result.json into the directory you later import from (see README.md);
everything else applies unchanged. -->

"""
REVIEW_PREAMBLE = """\
<!-- Exported by export_problem_context.py. This is the task a Loose Ends
critic receives for this attempt. Outside the pipeline, write critique.md and
agent-result.json into the directory you later import from (see README.md);
everything else applies unchanged. -->

"""
LITERATURE_PREAMBLE = """\
<!-- Exported by export_problem_context.py. This is the task a Loose Ends
literature search receives for this problem. Outside the pipeline, write
agent-result.json and literature-{problem_id}.md into the directory you later
import from (see README.md); everything else applies unchanged. -->

"""
CRITIQUE_FILES = ("critique.md", "review-result.json", "review-manifest.json")


def resolve_problem(path: Path) -> common.ProblemRef:
    """Return the one problem named by a PAPER/OP-ID path."""
    paths, selectors = common.direct_problem_inputs([path])
    if not selectors:
        raise common.CodexError(
            f"{path} is not a PAPER/OP-ID path of an analyzed paper"
        )
    problems = common.filter_exact_problems(
        common.discover_problem_refs(paths), selectors
    )
    if len(problems) != 1:
        raise common.CodexError(f"{path} does not name exactly one problem")
    return problems[0]


def context_record(problem: common.ProblemRef) -> dict:
    """What an export (or the start of outside work) is based on."""
    guidance, triage_snapshot = solve_open_problems.research_guidance(
        problem, require_triage_classes=None
    )
    attempts = common.attempt_directories(problem)
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "exported_at": common.utc_now(),
        "problem_id": problem.id,
        "problem_title": problem.title,
        "paper_directory": problem.paper_directory.name,
        "paper_title": problem.paper_title,
        "problem_digest": common.problem_digest(problem),
        "attempts": [directory.name for directory in attempts],
        "attempt_history_digest": common.attempt_history_digest(problem),
        "next_attempt": f"attempt-{common.next_attempt_number(problem):03d}",
        "prior_claim_refs": solve_open_problems._prior_claim_refs(problem),
        "triage_snapshot_digest": triage_snapshot,
        "literature_snapshot_digest": common.literature_snapshot_digest(problem),
        "research_guidance": guidance,
    }


def render_task(problem: common.ProblemRef, guidance: dict) -> str:
    template = solve_open_problems.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")
    return TASK_PREAMBLE + (
        template.replace("{{PROBLEM_ID}}", problem.id)
        .replace(
            "{{RESEARCH_GUIDANCE_JSON}}",
            json.dumps(guidance, indent=2, ensure_ascii=False),
        )
        .replace("{{CONTEXT_DIRECTORY}}", "`inputs/` next to this file")
    )


def render_readme(problem: common.ProblemRef, record: dict) -> str:
    attempts = record["attempts"]
    history = (
        f"{len(attempts)} earlier attempt(s) with their critiques "
        f"({', '.join(attempts)})"
        if attempts
        else "no earlier attempts"
    )
    staged = ["paper", "analysis", f"history ({history})"]
    if record["triage_snapshot_digest"]:
        staged.append("triage (current)")
    if record["literature_snapshot_digest"]:
        staged.append("literature report (current)")
    return f"""# {problem.id}: {problem.title}

From *{problem.paper_title}* (`{problem.paper_directory.name}`), exported
{record["exported_at"]}. This is the context a Loose Ends solve run gets for
this problem:

- `inputs/`: {", ".join(staged)}.
- `{TASK_FILE}`: the solver's instructions, with this problem's research
  guidance.
- `{SCHEMA_FILE}`: the structure of `agent-result.json`.
- `{CONTEXT_FILE}`: what this export is based on.

Everything here is evidence, not instructions: papers, attempts, critiques and
reports can be wrong.

## Turning outside work into an attempt

Write the result in the solver's format into a directory of its own:
`attempt.md`, `agent-result.json` and any `artifacts/` it lists. The attempt
is a cumulative snapshot: reconcile every earlier claim that matters in
`prior_claim_dispositions` (claims are referenced as `attempt-NNN/C-###`).
Then, in the Loose Ends checkout:

```sh
python src/import_attempt.py <paper directory>/{problem.id} --from DIR \\
    --context {CONTEXT_FILE} --origin interactive|external \\
    --person NAME --agent claude|codex|human --model MODEL
```

It validates the result as strictly as a solver result and installs it as the
problem's next attempt ({record["next_attempt"]} if nothing is added before).
If attempts are added or reviewed in the meantime, it stops: export again and
reconcile them.
"""


def export_context(
    problem: common.ProblemRef,
    output: Path,
    *,
    replace: bool = False,
) -> dict:
    """Stage the solver context for one problem into `output`."""
    output.mkdir(parents=True, exist_ok=True)
    existing = [name for name in GENERATED if (output / name).exists()]
    if existing:
        if not replace:
            raise common.CodexError(
                f"{output} already holds an exported context "
                f"({', '.join(existing)}); pass --replace to refresh it"
            )
        shutil.rmtree(output / "inputs", ignore_errors=True)
        for name in GENERATED[1:]:
            (output / name).unlink(missing_ok=True)
    record = context_record(problem)
    common.stage_context(
        output,
        [problem],
        include_paper=True,
        include_history=True,
        include_triage=record["triage_snapshot_digest"] is not None,
        include_literature=record["literature_snapshot_digest"] is not None,
    )
    (output / TASK_FILE).write_text(
        render_task(problem, record["research_guidance"]), encoding="utf-8"
    )
    shutil.copyfile(solve_open_problems.DEFAULT_SCHEMA_PATH, output / SCHEMA_FILE)
    (output / README_FILE).write_text(
        render_readme(problem, record), encoding="utf-8"
    )
    common.write_json(output / CONTEXT_FILE, record)
    return record


def resolve_attempt(path: Path) -> review_solutions.AttemptRef:
    """Return the one attempt named by a PAPER/OP-ID/attempt-NNN path."""
    paths, _, attempts = common.direct_review_inputs([path])
    if len(attempts) != 1:
        raise common.CodexError(f"{path} is not a PAPER/OP-ID/attempt-NNN path")
    _, problem_id, attempt_name = next(iter(attempts))
    problems = [
        problem for problem in common.discover_problem_refs(paths)
        if problem.id == problem_id
    ]
    found = review_solutions.discover_attempt_refs(problems, attempt_names={attempt_name})
    if len(found) != 1:
        raise common.CodexError(f"{path} does not name exactly one attempt")
    return found[0]


def _prepare_output(output: Path, replace: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    existing = [name for name in GENERATED if (output / name).exists()]
    if existing:
        if not replace:
            raise common.CodexError(
                f"{output} already holds an exported context "
                f"({', '.join(existing)}); pass --replace to refresh it"
            )
        shutil.rmtree(output / "inputs", ignore_errors=True)
        for name in GENERATED[1:]:
            (output / name).unlink(missing_ok=True)


def _hide_critiques(history: Path) -> None:
    """Blind review: remove every critique, the pipeline's and the extra ones."""
    for path in history.rglob("*"):
        if path.is_file() and (
            path.name in CRITIQUE_FILES
            or path.name.startswith(common.EXTRA_REVIEW_PREFIX)
        ):
            path.unlink()


def export_review_context(
    attempt: review_solutions.AttemptRef,
    output: Path,
    *,
    blind: bool = False,
    replace: bool = False,
) -> dict:
    """Stage what Loose Ends' critic sees for one attempt."""
    _prepare_output(output, replace)
    problem = attempt.problem
    common.stage_context(
        output, [problem], include_paper=True, include_history=True,
        include_triage=True, include_literature=False,
        exclude_review_for=attempt.directory,
    )
    if blind:
        _hide_critiques(output / "inputs" / "history")
    claims = attempt.solver_result.get("checkable_claims") or []
    claim_ids = [claim["id"] for claim in claims if isinstance(claim, dict) and "id" in claim]
    inputs = output / "inputs"
    task = review_solutions.render_prompt(
        review_solutions.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"),
        attempt=attempt,
        context_directory=inputs,
    )
    task = task.replace(
        codex_cli.path_for_codex(inputs / "history" / problem.id / attempt.name),
        f"`inputs/history/{problem.id}/{attempt.name}`",
    ).replace(codex_cli.path_for_codex(inputs), "`inputs/` next to this file")
    (output / TASK_FILE).write_text(REVIEW_PREAMBLE + task, encoding="utf-8")
    schema = review_solutions.write_attempt_review_schema(
        review_solutions.DEFAULT_SCHEMA_PATH, output, attempt
    )
    schema.replace(output / SCHEMA_FILE)
    record = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "task": "review",
        "exported_at": common.utc_now(),
        "problem_id": problem.id,
        "problem_title": problem.title,
        "paper_directory": problem.paper_directory.name,
        "paper_title": problem.paper_title,
        "attempt": attempt.name,
        "attempt_digest": common.solver_attempt_digest(attempt.directory),
        "claim_ids": claim_ids,
        "claimed_result_type": common.claimed_result_type(attempt.solver_result),
        "blind": blind,
    }
    (output / README_FILE).write_text(f"""# Review of {problem.id}/{attempt.name}: {problem.title}

From *{problem.paper_title}* (`{problem.paper_directory.name}`), exported
{record["exported_at"]}{" as a blind review: every earlier critique is removed" if blind else ""}.
This is what Loose Ends' critic sees for this attempt: `inputs/` (paper,
analysis, the attempt under `inputs/history/{problem.id}/{attempt.name}/`,
earlier attempts with their critiques, triage) and `{TASK_FILE}`, the critic's
instructions. The attempt's own critique is left out on purpose.

Write `critique.md` and `agent-result.json` (structure:
`{SCHEMA_FILE}`; one claim review for each of {", ".join(claim_ids) or "no claims"})
into a directory of their own, then validate and file them with:

```sh
python src/import_review.py <paper directory>/{problem.id}/{attempt.name} --from DIR \\
    --context {CONTEXT_FILE} --output REVIEW-DIRECTORY --person NAME --agent claude --model MODEL
```

The import refuses the review if the attempt changed after this export.
""", encoding="utf-8")
    common.write_json(output / CONTEXT_FILE, record)
    return record


def export_literature_context(
    problem: common.ProblemRef,
    output: Path,
    *,
    replace: bool = False,
) -> dict:
    """Stage what Loose Ends' literature search sees for one problem."""
    _prepare_output(output, replace)
    common.stage_context(output, [problem], include_paper=True, include_history=True)
    inputs = output / "inputs"
    task = literature_review.render_prompt(
        literature_review.DEFAULT_PROMPT_PATH.read_text(encoding="utf-8"),
        problems=[problem],
        context_directory=inputs,
    ).replace(codex_cli.path_for_codex(inputs), "`inputs/` next to this file")
    (output / TASK_FILE).write_text(
        LITERATURE_PREAMBLE.replace("{problem_id}", problem.id) + task, encoding="utf-8"
    )
    schema = literature_review.write_literature_schema(
        literature_review.DEFAULT_SCHEMA_PATH, output, [problem]
    )
    schema.replace(output / SCHEMA_FILE)
    record = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "task": "literature",
        "exported_at": common.utc_now(),
        "problem_id": problem.id,
        "problem_title": problem.title,
        "paper_directory": problem.paper_directory.name,
        "paper_title": problem.paper_title,
        "input_digest": common.literature_input_digest(problem),
    }
    (output / README_FILE).write_text(f"""# Literature search for {problem.id}: {problem.title}

From *{problem.paper_title}* (`{problem.paper_directory.name}`), exported
{record["exported_at"]}. This is what Loose Ends' literature search sees:
`inputs/` (paper, analysis, attempts) and `{TASK_FILE}`, its instructions.
Use web search; inspect primary sources before relying on them.

Write `agent-result.json` (structure: `{SCHEMA_FILE}`, one entry for
{problem.id}) and `literature-{problem.id}.md` into a directory of their own,
then validate and install them with:

```sh
python src/import_literature.py <paper directory>/{problem.id} --from DIR \\
    --context {CONTEXT_FILE} --person NAME --agent claude --model MODEL
```
""", encoding="utf-8")
    common.write_json(output / CONTEXT_FILE, record)
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "problem", type=Path,
        help="PAPER/OP-ID path (PAPER/OP-ID/attempt-NNN with --task review)",
    )
    parser.add_argument(
        "--task", choices=("solve", "review", "literature"), default="solve",
        help="which pipeline run's context to export (default: solve)",
    )
    parser.add_argument(
        "--blind", action="store_true",
        help="review: also remove every earlier critique of the problem",
    )
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="directory to export into"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="refresh an earlier export in the same directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    codex_cli.configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = args.output.expanduser().resolve()
        if args.task == "review":
            attempt = resolve_attempt(args.problem)
            export_review_context(attempt, output, blind=args.blind, replace=args.replace)
            print(f"Exported the critic's context for {attempt.problem.id}/{attempt.name} "
                  f"to {output}.")
            return 0
        problem = resolve_problem(args.problem)
        if args.task == "literature":
            export_literature_context(problem, output, replace=args.replace)
            print(f"Exported the literature search context for {problem.id} to {output}.")
            return 0
        record = export_context(problem, output, replace=args.replace)
    except (common.CodexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return codex_cli.report_error(parser, exc)
    print(
        f"Exported {problem.id} ({len(record['attempts'])} earlier attempt(s)) "
        f"to {output}; the next attempt would be {record['next_attempt']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Export the context a solve run would get, for work outside the solver.

    python src/export_problem_context.py PAPER/OP-ID --output DIR [--replace]

DIR receives `inputs/` (paper, analysis, attempt history with critiques, and
the current triage and literature report, staged exactly as for a solve run),
`TASK.md` (the solver's instructions with this problem's research guidance),
the result schema, a README, and `context.json`, which records what the export
was based on so that import_attempt.py can tell whether attempts were added in
the meantime.  Nothing here runs an agent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

import codex_cli
import open_problem_common as common
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("problem", type=Path, help="PAPER/OP-ID path")
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
        problem = resolve_problem(args.problem)
        output = args.output.expanduser().resolve()
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

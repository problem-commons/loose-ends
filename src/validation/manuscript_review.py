"""Validate a mathematical review of an existing manuscript."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from . import common


FINDING_ID_RE = re.compile(r"^M-[0-9]{3,}$")
VERDICTS = {"no_issues_found", "needs_minor_revision", "needs_major_revision", "incorrect"}
SEVERITIES = {"blocking", "major", "minor"}
CATEGORIES = {"proof", "statement", "definition", "computation", "citation", "novelty", "other"}
ASSESSMENTS = {"verified", "gap", "incorrect", "not_checked"}


def validate(*, workspace: Path, expectations: Mapping[str, object]) -> common.ValidationReport:
    reporter = common.Reporter()
    result = common.read_json_object(workspace / common.AGENT_RESULT_FILENAME, reporter, code="E_RESULT_JSON", description="mathematical review result")
    report = common.read_markdown(workspace / "math-review.md", reporter, code="E_REPORT", description="mathematical review")
    files = [workspace / "math-review.md"] if report is not None else []
    if result is None:
        return common.ValidationReport(files=files, issues=reporter.issues)
    common.validate_result_schema(result, expectations, reporter)
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
        reporter.error("E_VERDICT", "invalid verdict", path="agent-result.json#/verdict")
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        reporter.error("E_SUMMARY", "summary must be nonempty", path="agent-result.json#/summary")
    if common.string_list(result.get("warnings")) is None:
        reporter.error("E_WARNINGS", "warnings must be an array of strings", path="agent-result.json#/warnings")
    statements = result.get("statements")
    assessments: list[str] = []
    if not isinstance(statements, list):
        reporter.error("E_STATEMENTS", "statements must be an array", path="agent-result.json#/statements")
        statements = []
    for index, statement in enumerate(statements):
        base = f"agent-result.json#/statements/{index}"
        if not isinstance(statement, dict):
            reporter.error("E_STATEMENT", "statement must be an object", path=base)
            continue
        if statement.get("assessment") not in ASSESSMENTS:
            reporter.error("E_ASSESSMENT", "invalid assessment", path=f"{base}/assessment")
        else:
            assessments.append(statement["assessment"])
        for field in ("label", "location", "explanation"):
            if not isinstance(statement.get(field), str) or not statement[field].strip():
                reporter.error("E_STATEMENT_TEXT", f"{field} must be nonempty", path=f"{base}/{field}")
    findings = result.get("findings")
    finding_ids: set[str] = set()
    severities: list[str] = []
    if not isinstance(findings, list):
        reporter.error("E_FINDINGS", "findings must be an array", path="agent-result.json#/findings")
        findings = []
    for index, finding in enumerate(findings):
        base = f"agent-result.json#/findings/{index}"
        if not isinstance(finding, dict):
            reporter.error("E_FINDING", "finding must be an object", path=base)
            continue
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not FINDING_ID_RE.fullmatch(finding_id) or finding_id in finding_ids:
            reporter.error("E_FINDING_ID", f"invalid or duplicate finding ID {finding_id!r} (use M-001, M-002, ...)", path=f"{base}/id")
        else:
            finding_ids.add(finding_id)
        if finding.get("severity") not in SEVERITIES:
            reporter.error("E_SEVERITY", "invalid severity", path=f"{base}/severity")
        else:
            severities.append(finding["severity"])
        if finding.get("category") not in CATEGORIES:
            reporter.error("E_CATEGORY", "invalid category", path=f"{base}/category")
        for field in ("location", "explanation", "suggested_repair"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                reporter.error("E_FINDING_TEXT", f"{field} must be nonempty", path=f"{base}/{field}")
    serious = any(severity in {"blocking", "major"} for severity in severities)
    if verdict == "no_issues_found" and (serious or "incorrect" in assessments):
        reporter.error("E_CLEAN_CONSISTENCY", "no_issues_found is incompatible with blocking or major findings or an incorrect statement", path="agent-result.json#/verdict")
    if verdict == "needs_minor_revision" and (not findings or serious):
        reporter.error("E_MINOR_CONSISTENCY", "needs_minor_revision requires findings, none of them blocking or major", path="agent-result.json#/verdict")
    if verdict == "needs_major_revision" and not serious:
        reporter.error("E_MAJOR_CONSISTENCY", "needs_major_revision requires a blocking or major finding", path="agent-result.json#/verdict")
    if verdict == "incorrect" and "incorrect" not in assessments and "blocking" not in severities:
        reporter.error("E_INCORRECT_CONSISTENCY", "incorrect requires an incorrect statement or a blocking finding", path="agent-result.json#/verdict")
    if report is not None:
        for finding_id in sorted(finding_ids):
            if finding_id not in report:
                reporter.error("E_FINDING_MARKDOWN", f"math-review.md omits {finding_id}", path="math-review.md")
    return common.ValidationReport(result=result, files=files, issues=reporter.issues)


if __name__ == "__main__":
    # Agent-only entry point for the staged workspace. Repository drivers
    # import validate() and pass authoritative expectations directly.
    raise SystemExit(common.run_agent_validation(validate, validation_directory=Path(__file__).resolve().parent))

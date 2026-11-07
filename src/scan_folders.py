#!/usr/bin/env python3
"""What Loose Ends sees in your folders, and how to use each of them.

    python src/scan_folders.py ~/papers/oriented-spanners ~/math-notes ...
    python src/scan_folders.py ~/math-notes --toml     # a loose-ends.toml to start from

For every folder: whether it is a git checkout (repository, commit, pushed),
its loose-ends.toml or the LaTeX manuscripts found in
it, their PDFs and reviews, LaTeX documents too deep to be found
automatically, and other material (Markdown, PDFs, code) that can serve as
context for a mathematical review. Nothing is changed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import re
import sys

import review_manuscript
import source_references
import workbench_projects as projects


SCAN_DEPTH = 6
MAX_FILES = 50_000
MATERIAL_SUFFIXES = {
    ".md": "Markdown", ".txt": "text", ".pdf": "PDF", ".tex": "LaTeX", ".bib": "BibTeX",
    ".lean": "Lean", ".py": "Python", ".ipynb": "notebooks", ".ipe": "Ipe", ".svg": "SVG",
}
DOCUMENT_CLASS = re.compile(r"^[^%\n]*\\documentclass", re.MULTILINE)


def _walk(folder: Path):
    """(relative path, depth) of files below the folder, skipping what detection skips."""
    count = 0
    for directory, children, files in os.walk(folder):
        relative = Path(directory).relative_to(folder)
        depth = len(relative.parts)
        children[:] = sorted(
            name for name in children
            if name not in projects.SKIPPED_DIRECTORIES and not name.startswith(".")
            and depth < SCAN_DEPTH
        )
        for name in sorted(files):
            count += 1
            if count > MAX_FILES:
                return
            yield relative / name, depth


def _is_document(path: Path) -> bool:
    try:
        return DOCUMENT_CLASS.search(path.read_text(encoding="utf-8", errors="replace")[:20000]) is not None
    except OSError:
        return False


def scan(folder: Path) -> dict:
    folder = folder.expanduser().resolve()
    report: dict = {"folder": folder, "exists": folder.is_dir()}
    if not folder.is_dir():
        return report
    report["source"] = source_references.reference(folder, status=False)
    report["checkout"] = source_references.checkout_root(folder)
    try:
        shared = projects.read_paper_project(folder)
        report["error"] = None
    except projects.ProjectError as exc:
        shared = {"described": True, "manuscripts": ()}
        report["error"] = str(exc)
    report["described"] = shared.get("described", False)
    report["id"] = shared.get("id")
    report["reviews"] = shared.get("reviews") or projects.DEFAULT_REVIEWS
    manuscripts = []
    for manuscript in shared["manuscripts"]:
        overview = review_manuscript.manuscript_overview(
            folder, manuscript, shared.get("reviews") or projects.DEFAULT_REVIEWS
        )
        manuscripts.append(overview)
    report["manuscripts"] = manuscripts
    listed = {item["entry"] for item in manuscripts}
    deeper, material, inner = [], Counter(), []
    for relative, _depth in _walk(folder):
        kind = MATERIAL_SUFFIXES.get(relative.suffix.casefold())
        if kind:
            material[kind] += 1
        if relative.name == projects.SHARED_PROJECT_FILE and relative.parent.parts:
            inner.append(relative.parent.as_posix())
        if relative.suffix == ".tex" and relative.as_posix() not in listed and _is_document(folder / relative):
            deeper.append(relative.as_posix())
    # Folders below with a loose-ends.toml are projects of their own.
    report["projects_inside"] = sorted(inner)
    if not report["described"] and inner:
        report["manuscripts"] = [
            item for item in manuscripts
            if not any(item["entry"].startswith(f"{path}/") for path in inner)
        ]
        deeper = [path for path in deeper if not any(path.startswith(f"{inner_path}/") for inner_path in inner)]
    report["unlisted_documents"] = deeper
    report["material"] = material
    return report


def _print(report: dict) -> None:
    folder = report["folder"]
    print(folder)
    if not report["exists"]:
        print("  not found")
        return
    source, root = report["source"], report["checkout"]
    if source["commit"]:
        where = source["repository"] or "local checkout without a hosted remote"
        inside = f" (inside the checkout {root})" if root != folder else ""
        state = [f"commit {source['commit'][:10]}"]
        if source["committed"] is False:
            state.append("uncommitted changes")
        if source["pushed"] is False:
            state.append("not pushed")
        print(f"  git: {where}{inside}; " + ", ".join(state))
    elif root:
        print(f"  git: checkout {root} without commits")
    else:
        print("  git: not a git checkout; reviews identify files by their hashes only")
    if report["error"]:
        print(f"  loose-ends.toml: {report['error']}")
    elif report["described"]:
        print(f"  loose-ends.toml: id {report['id'] or '(none)'}; reviews in {report['reviews']}/")
    else:
        print("  loose-ends.toml: none; manuscripts are detected (\\documentclass up to two levels deep)")
    for item in report["manuscripts"]:
        pdf = "no PDF" if item["pdfState"] == "missing" else f"PDF {item['pdfState']}"
        reviews = item["reviews"]
        outdated = sum(not review["current"] for review in reviews)
        print(f"  manuscript {item['name']}: {item['entry']}, {pdf}, "
              f"{len(reviews)} review(s)" + (f", {outdated} outdated" if outdated else ""))
        if item.get("context"):
            missing = [path for path in item["context"] if not (folder / path).exists()]
            print("    context: " + ", ".join(item["context"])
                  + (f" (missing: {', '.join(missing)})" if missing else ""))
    if report["projects_inside"]:
        print("  projects inside (own loose-ends.toml): " + ", ".join(report["projects_inside"]))
    unlisted = report["unlisted_documents"]
    if unlisted:
        examples = ", ".join(unlisted[:4]) + (" …" if len(unlisted) > 4 else "")
        where = "not in loose-ends.toml" if report["described"] else "too deep to be found"
        print(f"  {len(unlisted)} more LaTeX document(s) {where}: {examples}")
    if report["material"]:
        print("  material: " + ", ".join(f"{count} {kind}" for kind, count in report["material"].most_common()))
    print("  use: " + _advice(report))


def _advice(report: dict) -> str:
    advice = _project_advice(report)
    notes = sum(report["material"].get(kind, 0) for kind in ("Markdown", "text", "Lean", "notebooks"))
    if notes and (report["manuscripts"] or report["unlisted_documents"]):
        advice += "; its notes can also be --context for a mathematical review"
    return advice


def _project_advice(report: dict) -> str:
    if report["projects_inside"] and not report["described"]:
        return ("folder of projects: add the projects inside to papers = [...], not this folder"
                + ("; it has further manuscripts of its own" if report["manuscripts"] else ""))
    if report["manuscripts"]:
        advice = ("paper project: add it to papers = [...] in ~/.config/problem-commons/workbench.toml "
                  "(or --paper), then review from the Manuscripts page")
        if report["unlisted_documents"] and not report["described"]:
            advice += "; list the other documents in loose-ends.toml (--toml prints a start)"
        return advice
    if report["unlisted_documents"]:
        return ("LaTeX documents deeper than two levels: write a loose-ends.toml listing them "
                "(--toml prints a start), then add the folder as a paper project")
    if report["material"]:
        return "notes: pass the folder or single files as --context to a mathematical review"
    return "nothing Loose Ends uses"


def suggested_description(report: dict) -> str:
    folder = report["folder"]
    entries = [item["entry"] for item in report.get("manuscripts", [])] + report.get("unlisted_documents", [])
    lines = [
        f"# loose-ends.toml for {folder.name}: shared with everyone who uses the folder,",
        "# so no machine-specific paths. Remove the manuscripts you do not want listed.",
        "schema_version = 1",
        f'id = "{re.sub(r"[^A-Za-z0-9._-]+", "-", folder.name).strip("-").casefold()}"',
        f'title = "{folder.name}"',
        '# audience = "computational geometers"',
        '# writing_profile = "writing/my-style"   # a folder inside this repository',
        '# reviews = "reviews"                     # default: manuscript-reviews',
        '# context = ["STATUS.md", "proofs"]       # for the mathematical reviewer',
    ]
    names: set[str] = set()
    for entry in entries:
        name = projects.default_name(entry)
        while name in names:
            name += "-1"
        names.add(name)
        lines += ["", "[[manuscript]]", f'name = "{name}"', f'entry = "{entry}"',
                  '# build = ["latexmk", "-pdf", "main.tex"]   # run next to the entry']
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--toml", action="store_true",
                        help="print a loose-ends.toml to start from for each folder")
    args = parser.parse_args(argv)
    reports = [scan(folder) for folder in args.folders]
    if args.toml:
        for report in reports:
            if report["exists"]:
                print(suggested_description(report))
        return 0
    for report in reports:
        _print(report)
        print()
    papers = [
        str(report["folder"]) for report in reports
        if report.get("manuscripts") and (report["described"] or not report["projects_inside"])
    ]
    if papers:
        print("For ~/.config/problem-commons/workbench.toml:")
        print("papers = [" + ", ".join(f'"{path}"' for path in papers) + "]")
    return 0 if all(report["exists"] for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())

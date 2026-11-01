"""Collections, labels and stars: organising a large workbench catalog.

Collections are the first directory level below a paper root
(`papers/own/arXiv-...` belongs to `own`).  Labels come from optional JSON
files given with `--labels`, typically written by a project's own tooling:

    {"schema_version": 1,
     "labels": [{"target": "own/arXiv-2412.06316/OP-004", "label": "excluded",
                 "hidden": true, "title": "Ongoing work of a member"}]}

A target is a paper or problem path below a paper root; an arXiv paper may be
named without its version suffix.  Labels on a paper apply to its problems.
Hidden labels hide what they mark until the viewer chooses to show hidden
items.  A label with "skip": true also keeps triage, literature search and
solving away from what it marks, when its file is named in the environment
variable LOOSE_ENDS_LABELS (os.pathsep-separated; the workbench passes it on to
its tasks).  Stars are personal and live in the workbench state directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Iterable, Sequence


LABELS_SCHEMA_VERSION = 1
LABELS_ENV = "LOOSE_ENDS_LABELS"
PROBLEM_ID_RE = re.compile(r"^OP-[0-9]{3,}$")
STARS_FILE = "stars.json"
MAX_LABEL_LENGTH = 60
VERSION_RE = re.compile(r"v[0-9]+$")


@dataclass(frozen=True)
class Label:
    target: str
    label: str
    hidden: bool = False
    title: str = ""
    skip: bool = False

    def record(self) -> dict:
        return {"label": self.label, "hidden": self.hidden, "title": self.title}


def read_labels(path: Path) -> tuple[list[Label], list[str]]:
    """Labels of one file, and problems with its entries."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], [f"{path}: labels file not found"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"{path}: {exc}"]
    if not isinstance(value, dict) or value.get("schema_version") != LABELS_SCHEMA_VERSION:
        return [], [f"{path}: expected schema_version {LABELS_SCHEMA_VERSION}"]
    entries = value.get("labels")
    if not isinstance(entries, list):
        return [], [f"{path}: labels must be a list"]
    labels: list[Label] = []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        where = f"{path}: labels[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} is not an object")
            continue
        target, label = entry.get("target"), entry.get("label")
        if not isinstance(target, str) or not target.strip("/ "):
            errors.append(f"{where} has no target")
            continue
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{where} has no label")
            continue
        hidden = entry.get("hidden", False)
        title = entry.get("title", "")
        labels.append(Label(
            target.strip("/ "),
            label.strip()[:MAX_LABEL_LENGTH],
            hidden is True,
            title if isinstance(title, str) else "",
            entry.get("skip") is True,
        ))
    return labels, errors


def environment_label_paths() -> list[Path]:
    value = os.environ.get(LABELS_ENV, "")
    return [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]


def skip_reason(labels: Iterable[Label], paper_directory: Path, problem_id: str) -> str | None:
    """Why a skip label keeps the pipeline away from a problem, if one does."""
    paper = paper_directory.as_posix()
    candidates = {paper, VERSION_RE.sub("", paper)}
    for label in labels:
        if not label.skip:
            continue
        head, _, tail = label.target.rpartition("/")
        paper_target, op = (head, tail) if PROBLEM_ID_RE.match(tail) else (label.target, "")
        if op and op != problem_id:
            continue
        if any(
            candidate == paper_target or candidate.endswith("/" + paper_target)
            for candidate in candidates
        ):
            return label.title or label.label
    return None


class LabelSource:
    """Labels from files that may change while the workbench runs."""

    def __init__(self, paths: Sequence[Path]):
        self.paths = [path.expanduser().resolve() for path in paths]
        self.lock = threading.Lock()
        self.labels: list[Label] = []
        self.errors: list[str] = []
        self.signature: tuple | None = None
        self.refresh()

    def _signature(self) -> tuple:
        values = []
        for path in self.paths:
            try:
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((str(path), None, None))
        return tuple(values)

    def refresh(self) -> bool:
        """Reload when a file changed; return whether anything did."""
        signature = self._signature()
        with self.lock:
            if signature == self.signature:
                return False
            labels: list[Label] = []
            errors: list[str] = []
            for path in self.paths:
                found, problems = read_labels(path)
                labels.extend(found)
                errors.extend(problems)
            self.labels, self.errors, self.signature = labels, errors, signature
            return True

    def current(self) -> list[Label]:
        with self.lock:
            return list(self.labels)


class StarStore:
    """Personal stars on papers and problems, kept with the workbench state."""

    def __init__(self, directory: Path):
        self.path = directory / STARS_FILE
        self.lock = threading.Lock()

    def load(self) -> dict[str, set[str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {}
        return {
            kind: {key for key in value.get(kind, []) if isinstance(key, str)}
            for kind in ("papers", "problems")
        }

    def set(self, kind: str, key: str, starred: bool) -> None:
        if kind not in ("papers", "problems") or not key:
            raise ValueError("a star needs kind papers or problems and a key")
        with self.lock:
            stars = self.load()
            (stars[kind].add if starred else stars[kind].discard)(key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=".stars-", suffix=".json", dir=self.path.parent
            )
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(
                    {kind: sorted(values) for kind, values in stars.items()},
                    output, indent=2, ensure_ascii=False,
                )
                output.write("\n")
            os.replace(temporary, self.path)


def paper_identity(paper_path: str, roots: Iterable[Path]) -> tuple[str, str]:
    """A paper's path below its paper root, and its collection."""
    path = Path(paper_path)
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return path.name, ""
        collection = relative.parts[0] if len(relative.parts) > 1 else ""
        return relative.as_posix(), collection
    return path.name, ""


def _index(labels: Iterable[Label]) -> dict[str, list[Label]]:
    index: dict[str, list[Label]] = {}
    for label in labels:
        index.setdefault(label.target, []).append(label)
    return index


def _labels_for(index: dict[str, list[Label]], paper_id: str, problem_id: str = "") -> list[dict]:
    papers = dict.fromkeys([paper_id, VERSION_RE.sub("", paper_id)])
    targets = list(papers)
    if problem_id:
        targets += [f"{paper}/{problem_id}" for paper in papers]
    found: dict[tuple[str, bool], dict] = {}
    for target in targets:
        for label in index.get(target, []):
            found.setdefault((label.label, label.hidden), label.record())
    return list(found.values())


def organize(
    value: dict,
    roots: Sequence[Path],
    labels: Iterable[Label],
    stars: dict[str, set[str]],
) -> dict:
    """Add collection, labels, hidden and starred to catalog rows, in place."""
    index = _index(labels)
    for paper in value.get("papers", []):
        paper_id, collection = paper_identity(paper.get("path", ""), roots)
        found = _labels_for(index, paper_id)
        paper.update({
            "paperId": paper_id,
            "collection": collection,
            "labels": found,
            "hidden": any(label["hidden"] for label in found),
            "starred": paper.get("key") in stars["papers"],
        })
    for item in value.get("reviews", []):
        paper_id, collection = paper_identity(item.get("paperDirectory", ""), roots)
        found = _labels_for(index, paper_id, item.get("problemId", ""))
        item.update({
            "paperId": paper_id,
            "collection": collection,
            "labels": found,
            "hidden": any(label["hidden"] for label in found),
            "starred": item.get("problemKey") in stars["problems"],
        })
    return value

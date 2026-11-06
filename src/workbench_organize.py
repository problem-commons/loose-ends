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

A labels file may also declare actions that change the labels, for example
adding a tag.  The workbench offers them on paper and problem pages only when
it was started with `--label-command`; it never runs anything named in the
file itself.  The command receives one JSON object on standard input,
`{"action", "kind", "target", "values"}`, is expected to change its own
records and rewrite the labels file, and prints a short message:

    {"id": "tag", "label": "Add tag", "kinds": ["paper", "problem"],
     "fields": [{"name": "value", "label": "Tag", "required": true,
                 "suggestions": ["frechet"]}]}
    {"id": "untag", "label": "Remove tag frechet", "kinds": ["paper"],
     "target": "own/arXiv-2501.12814", "values": {"value": "frechet"}}

Without "target" an action applies to every paper or problem of its kinds.
Fields are text (with optional suggestions), textarea or select (options as
[value, label] pairs), optionally with a starting "value"; "values" are sent
as given.

Collections, the first directory level below a paper root, are also offered
as destinations for added papers (`import_targets`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Iterable, Sequence


LABELS_SCHEMA_VERSION = 1
LABELS_ENV = "LOOSE_ENDS_LABELS"
PROBLEM_ID_RE = re.compile(r"^OP-[0-9]{3,}$")
STARS_FILE = "stars.json"
MAX_LABEL_LENGTH = 60
VERSION_RE = re.compile(r"v[0-9]+$")
ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ACTION_KINDS = ("paper", "problem")
FIELD_TYPES = ("text", "textarea", "select")
MAX_VALUE_LENGTH = 2000


@dataclass(frozen=True)
class Label:
    target: str
    label: str
    hidden: bool = False
    title: str = ""
    skip: bool = False
    root: str = ""  # the paper root it describes; empty: every root

    def covers(self, root: str) -> bool:
        return not self.root or self.root == root

    def record(self) -> dict:
        return {"label": self.label, "hidden": self.hidden, "title": self.title}


@dataclass(frozen=True)
class LabelAction:
    """A change to the labels that the label command carries out."""

    id: str
    label: str
    kinds: tuple[str, ...]
    target: str = ""
    title: str = ""
    fields: tuple = field(default=(), compare=False)  # dicts, as in the file
    values: tuple = ()  # (name, value) pairs
    root: str = ""  # the paper root it acts on; empty: every root
    source: str = ""  # the labels file that declared it (selects the command)

    @property
    def key(self) -> str:
        """Stable identity, so a click never runs an action that has changed."""
        text = json.dumps(self.describe(with_key=False), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def describe(self, *, with_key: bool = True) -> dict:
        value = {
            "id": self.id, "label": self.label, "title": self.title,
            "target": self.target, "fields": [dict(item) for item in self.fields],
            "values": dict(self.values),
        }
        if self.root:
            value["root"] = self.root
        if with_key:
            value["key"] = self.key
        return value

    def applies_to(self, kind: str, target: str, root: str = "") -> bool:
        if kind not in self.kinds:
            return False
        if self.root and root and self.root != root:
            return False
        if not self.target:
            return True
        return _strip_version(self.target) == _strip_version(target)


def _strip_version(target: str) -> str:
    """own/arXiv-1234.5v2/OP-001 and own/arXiv-1234.5/OP-001 name the same problem."""
    head, _, tail = target.rpartition("/")
    if PROBLEM_ID_RE.match(tail):
        return f"{VERSION_RE.sub('', head)}/{tail}"
    return VERSION_RE.sub("", target)


def _load_file(path: Path) -> tuple[dict | None, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{path}: labels file not found"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"{path}: {exc}"]
    if not isinstance(value, dict) or value.get("schema_version") != LABELS_SCHEMA_VERSION:
        return None, [f"{path}: expected schema_version {LABELS_SCHEMA_VERSION}"]
    return value, []


def _read_field(raw: object, where: str) -> tuple[dict | None, str]:
    if not isinstance(raw, dict):
        return None, f"{where} is not an object"
    name, label = raw.get("name"), raw.get("label")
    if not isinstance(name, str) or not FIELD_NAME_RE.match(name):
        return None, f"{where} needs a name of lower-case letters, digits and _"
    if not isinstance(label, str) or not label.strip():
        return None, f"{where} needs a label"
    kind = raw.get("type", "text")
    if kind not in FIELD_TYPES:
        return None, f"{where}: type must be one of {', '.join(FIELD_TYPES)}"
    value: dict = {"name": name, "label": label.strip(), "type": kind,
                   "required": raw.get("required") is True}
    for key in ("help", "value"):  # value: what the field starts with
        if isinstance(raw.get(key), str):
            value[key] = raw[key]
    if kind == "select":
        options = raw.get("options")
        if not isinstance(options, list) or not options or not all(
            isinstance(option, list) and len(option) == 2
            and all(isinstance(part, str) for part in option)
            for option in options
        ):
            return None, f"{where}: a select needs options as [value, label] pairs"
        value["options"] = [list(option) for option in options]
    suggestions = raw.get("suggestions", [])
    if isinstance(suggestions, list):
        value["suggestions"] = [item for item in suggestions if isinstance(item, str)][:200]
    return value, ""


def _scope(value: dict, root: str) -> str:
    """A labels file's own "root" wins over the scope the workbench assigns it."""
    declared = value.get("root")
    if isinstance(declared, str) and declared.strip():
        return str(Path(declared).expanduser().resolve())
    return root


def _read_action(raw: object, where: str) -> tuple[LabelAction | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{where} is not an object"]
    action_id, label = raw.get("id"), raw.get("label")
    if not isinstance(action_id, str) or not ACTION_ID_RE.match(action_id):
        return None, [f"{where} needs an id of lower-case letters, digits and -"]
    if not isinstance(label, str) or not label.strip():
        return None, [f"{where} needs a label"]
    kinds = raw.get("kinds", list(ACTION_KINDS))
    if not isinstance(kinds, list) or not kinds or any(kind not in ACTION_KINDS for kind in kinds):
        return None, [f"{where}: kinds must be a list of {' and '.join(ACTION_KINDS)}"]
    target = raw.get("target", "")
    if not isinstance(target, str):
        return None, [f"{where}: target must be a string"]
    values = raw.get("values", {})
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in values.items()
    ):
        return None, [f"{where}: values must map names to strings"]
    fields, errors = [], []
    for index, item in enumerate(raw.get("fields", []) if isinstance(raw.get("fields"), list) else []):
        parsed, error = _read_field(item, f"{where}.fields[{index}]")
        if parsed is None:
            errors.append(error)
        else:
            fields.append(parsed)
    if errors:
        return None, errors
    title = raw.get("title", "")
    return LabelAction(
        action_id, label.strip()[:MAX_LABEL_LENGTH], tuple(kinds), target.strip("/ "),
        title if isinstance(title, str) else "",
        tuple(fields),
        tuple(sorted(values.items())),
    ), []


def read_actions(path: Path, root: str = "") -> tuple[list[LabelAction], list[str]]:
    """Label actions of one file, and problems with its entries."""
    value, errors = _load_file(path)
    if value is None:
        return [], errors
    root = _scope(value, root)
    entries = value.get("actions", [])
    if not isinstance(entries, list):
        return [], [f"{path}: actions must be a list"]
    actions: list[LabelAction] = []
    for index, entry in enumerate(entries):
        action, problems = _read_action(entry, f"{path}: actions[{index}]")
        errors.extend(problems)
        if action is not None:
            actions.append(replace(action, root=root, source=str(path)))
    return actions, errors


def read_labels(path: Path, root: str = "") -> tuple[list[Label], list[str]]:
    """Labels of one file, and problems with its entries.

    A file may name the paper root it describes (`"root"`); its labels then
    apply only to papers below that root, so that two pools with the same
    collection names do not share labels.
    """
    value, errors = _load_file(path)
    if value is None:
        return [], errors
    root = _scope(value, root)
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
            root,
        ))
    return labels, errors


def environment_label_paths() -> list[Path]:
    value = os.environ.get(LABELS_ENV, "")
    return [Path(part).expanduser() for part in value.split(os.pathsep) if part.strip()]


def skip_reason(labels: Iterable[Label], paper_directory: Path, problem_id: str) -> str | None:
    """Why a skip label keeps the pipeline away from a problem, if one does."""
    paper = paper_directory.as_posix()
    candidates = {paper, VERSION_RE.sub("", paper)}
    resolved = paper_directory.expanduser().resolve()
    for label in labels:
        if not label.skip:
            continue
        if label.root and Path(label.root) not in resolved.parents:
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

    def __init__(self, paths: Sequence[Path], scopes: dict[Path, str] | None = None):
        self.paths = [path.expanduser().resolve() for path in paths]
        # Paper root a file describes when the file does not say so itself.
        self.scopes = {
            path.expanduser().resolve(): root for path, root in (scopes or {}).items()
        }
        self.lock = threading.Lock()
        self.labels: list[Label] = []
        self.actions: list[LabelAction] = []
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
            actions: list[LabelAction] = []
            errors: list[str] = []
            for path in self.paths:
                root = self.scopes.get(path, "")
                found, problems = read_labels(path, root)
                labels.extend(found)
                errors.extend(problems)
                if not problems:
                    declared, problems = read_actions(path, root)
                    actions.extend(declared)
                    errors.extend(problems)
            self.labels, self.actions = labels, actions
            self.errors, self.signature = errors, signature
            return True

    def current(self) -> list[Label]:
        with self.lock:
            return list(self.labels)

    def current_actions(self) -> list[LabelAction]:
        with self.lock:
            return list(self.actions)

    def find_action(
        self, key: str, kind: str, target: str, root: str = ""
    ) -> LabelAction | None:
        return next(
            (
                action for action in self.current_actions()
                if action.key == key and action.applies_to(kind, target, root)
            ),
            None,
        )


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


def paper_root(paper_path: str, roots: Iterable[Path]) -> str:
    """The paper root a paper lies in, as the string labels are scoped by."""
    path = Path(paper_path)
    for root in roots:
        if path == root or root in path.parents:
            return str(root)
    return ""


def _index(labels: Iterable[Label]) -> dict[str, list[Label]]:
    index: dict[str, list[Label]] = {}
    for label in labels:
        index.setdefault(label.target, []).append(label)
    return index


def _labels_for(
    index: dict[str, list[Label]], paper_id: str, problem_id: str = "", root: str = ""
) -> list[dict]:
    papers = dict.fromkeys([paper_id, VERSION_RE.sub("", paper_id)])
    targets = list(papers)
    if problem_id:
        targets += [f"{paper}/{problem_id}" for paper in papers]
    found: dict[tuple[str, bool], dict] = {}
    for target in targets:
        for label in index.get(target, []):
            if label.covers(root):
                found.setdefault((label.label, label.hidden), label.record())
    return list(found.values())


def _actions_for(
    actions: Sequence[LabelAction], kind: str, target: str, root: str = ""
) -> list[dict]:
    return [
        action.describe() for action in actions if action.applies_to(kind, target, root)
    ]


def organize(
    value: dict,
    roots: Sequence[Path],
    labels: Iterable[Label],
    stars: dict[str, set[str]],
    actions: Sequence[LabelAction] = (),
    projects: dict[str, dict] | None = None,
) -> dict:
    """Add collection, labels, hidden, starred, label actions and project to rows.

    `projects` maps a paper or manuscript root (as a string) to the project
    record ({"name", "id"}) it belongs to; rows outside them get no project.
    """
    index = _index(labels)
    projects = projects or {}

    def project_of(path: str) -> dict | None:
        candidate = Path(path)
        best = None
        for root, project in projects.items():
            root_path = Path(root)
            inside = candidate == root_path or root_path in candidate.parents
            if inside and (best is None or len(root_path.parts) > best[0]):
                best = (len(root_path.parts), project)
        return best[1] if best else None

    for paper in value.get("papers", []):
        root = paper_root(paper.get("path", ""), roots)
        paper_id, collection = paper_identity(paper.get("path", ""), roots)
        found = _labels_for(index, paper_id, root=root)
        paper.update({
            "paperId": paper_id,
            "collection": collection,
            "labels": found,
            "hidden": any(label["hidden"] for label in found),
            "starred": paper.get("key") in stars["papers"],
            "labelActions": _actions_for(actions, "paper", paper_id, root),
            "project": project_of(paper.get("path", "")),
        })
    for item in value.get("reviews", []):
        root = paper_root(item.get("paperDirectory", ""), roots)
        paper_id, collection = paper_identity(item.get("paperDirectory", ""), roots)
        found = _labels_for(index, paper_id, item.get("problemId", ""), root)
        target = f"{paper_id}/{item.get('problemId', '')}"
        item.update({
            "paperId": paper_id,
            "collection": collection,
            "labels": found,
            "hidden": any(label["hidden"] for label in found),
            "starred": item.get("problemKey") in stars["problems"],
            "labelActions": _actions_for(actions, "problem", target, root),
            "project": project_of(item.get("paperDirectory", "")),
        })
    for manuscript in value.get("manuscripts", []):
        manuscript["project"] = project_of(manuscript.get("path", ""))
    return value


def action_values(action: LabelAction, request: object) -> dict[str, str]:
    """The values a label action sends: its fixed values and the checked inputs."""
    if request is None:
        request = {}
    # ValueError throughout: these are mistakes in a person's input, not in code.
    if not isinstance(request, dict):
        raise ValueError("values must be an object")  # noqa: TRY004
    values = dict(action.values)
    for item in action.fields:
        raw = request.get(item["name"], "")
        if not isinstance(raw, str):
            raise ValueError(f"{item['label']} must be text")  # noqa: TRY004
        raw = raw.strip()
        if len(raw) > MAX_VALUE_LENGTH:
            raise ValueError(f"{item['label']} is too long")
        if item.get("required") and not raw:
            raise ValueError(f"{item['label']} is required")
        if item["type"] == "select" and raw not in [option[0] for option in item["options"]]:
            raise ValueError(f"{item['label']}: choose one of the options")
        values[item["name"]] = raw
    return values


def run_label_command(
    command: Sequence[str],
    action: LabelAction,
    kind: str,
    target: str,
    values: dict[str, str],
    *,
    timeout: float = 60,
) -> str:
    """Run the label command for one action; return its message."""
    payload = json.dumps(
        {"action": action.id, "kind": kind, "target": target, "values": values},
        ensure_ascii=False,
    )
    try:
        completed = subprocess.run(
            list(command), input=payload, capture_output=True, text=True,
            encoding="utf-8", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"the label command failed: {exc}") from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or output or f"exit code {completed.returncode}"
        raise RuntimeError(detail[-2000:])
    return output[-2000:] or f"{action.label}: done"


def import_targets(roots: Iterable[Path]) -> list[Path]:
    """Where added papers may go: each paper root's collections, then the root."""
    targets: list[Path] = []
    for root in roots:
        try:
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:
            children = []
        for child in children:
            if (
                child.is_dir()
                and not child.name.startswith((".", "_"))
                and not _is_paper_directory(child)
            ):
                targets.append(child)
        targets.append(root)
    return targets


def _is_paper_directory(path: Path) -> bool:
    # As analyze_papers.is_paper_directory, without importing the pipeline.
    return (path / "paper.pdf").is_file() or (path / "source").exists()

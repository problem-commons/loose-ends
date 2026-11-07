"""Projects of one workbench: paper roots, pools, and ordinary paper repositories.

A workbench can serve several projects at once (`workbench.py --projects
FILE`). The projects file is personal: it lists the checkouts on this machine
and is never shared. TOML or JSON, version 1:

    schema_version = 1

    [[project]]
    name = "pool"                    # unique display name on this workbench
    id = "problem-commons/pool"      # stable identity (default: name)
    kind = "pool"                    # pool | papers | paper
    papers = "~/problem-commons/pool/papers"
    manuscripts = "~/problem-commons/pool/manuscripts"
    labels = ["~/problem-commons/pool/workbench-labels.json"]
    label_command = ["python3", "tools/pool.py", "label-action"]

    [[project]]
    name = "spanners"
    kind = "paper"
    path = "~/papers/1d-oriented-spanners"

Relative paths are relative to the projects file. `pool` and `papers`
projects have a Loose Ends paper root; `pool` only marks that pool tooling
manages it. A `paper` project is an ordinary repository of manuscripts in its
own layout; its shared description, if any, is `loose-ends.toml` in the
repository (see `read_paper_project`). Labels and label actions of a project
apply only to its own paper root.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Sequence


PROJECTS_SCHEMA_VERSION = 1
PROJECT_KINDS = ("pool", "papers", "paper")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHARED_PROJECT_FILE = "loose-ends.toml"
# Directories never searched for LaTeX entry points of an undescribed repository.
DEFAULT_REVIEWS = "manuscript-reviews"
SKIPPED_DIRECTORIES = {".git", "node_modules", "__pycache__", ".loose-ends", "manuscript-reviews"}
MAX_DETECTION_DEPTH = 2


class ProjectError(ValueError):
    """An invalid projects file or project description."""


@dataclass(frozen=True)
class Manuscript:
    """One manuscript entry point of a paper project, relative to the repository."""

    name: str
    entry: str
    build: tuple[str, ...] = ()
    pdf: str = ""
    # Repository-relative files or folders the mathematical reviewer gets.
    context: tuple[str, ...] = ()

    @property
    def pdf_path(self) -> str:
        return self.pdf or str(Path(self.entry).with_suffix(".pdf").as_posix())

    def record(self) -> dict:
        return {
            "name": self.name,
            "entry": self.entry,
            "build": list(self.build),
            "pdf": self.pdf_path,
            "context": list(self.context),
        }


@dataclass(frozen=True)
class Project:
    name: str
    id: str
    kind: str
    papers: Path | None = None
    manuscripts: Path | None = None
    labels: tuple[Path, ...] = ()
    label_command: tuple[str, ...] = ()
    path: Path | None = None
    status: str = "ok"
    problems: tuple[str, ...] = ()
    paper_manuscripts: tuple[Manuscript, ...] = field(default=(), compare=False)
    title: str = ""
    writing_profile: str = ""
    audience: str = ""

    @property
    def available(self) -> bool:
        return self.status == "ok"

    def owns(self, path: Path) -> bool:
        """Whether a path lies in this project's papers, manuscripts, or repository."""
        resolved = path.expanduser().resolve()
        return any(
            root is not None and (resolved == root or root in resolved.parents)
            for root in (self.papers, self.manuscripts, self.path)
        )

    def record(self) -> dict:
        return {
            "name": self.name,
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "problems": list(self.problems),
            "papers": str(self.papers) if self.papers else None,
            "manuscripts": str(self.manuscripts) if self.manuscripts else None,
            "path": str(self.path) if self.path else None,
            "labelActions": bool(self.label_command),
            "paperManuscripts": [item.record() for item in self.paper_manuscripts],
            "writingProfile": self.writing_profile,
            "audience": self.audience,
        }


def _load_document(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProjectError(f"could not read {path}: {exc}") from exc
    try:
        value = tomllib.loads(text) if path.suffix.casefold() == ".toml" else json.loads(text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"{path} is not valid {path.suffix.lstrip('.') or 'JSON'}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectError(f"{path} must contain a table/object")
    return value


def _resolve(value: object, base: Path, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{where} must be a path")
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _command(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ProjectError(f"{where} must be a list of arguments, not a shell string")
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ProjectError(f"{where} must be a nonempty list of arguments")
    return tuple(value)


def _relative(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{where} must be a repository-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProjectError(f"{where} must stay inside the repository: {value}")
    return relative.as_posix()


def slug(text: str) -> str:
    """A name of letters, digits, . _ - from a folder or file name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "project"


def default_name(entry: str) -> str:
    """socg/main.tex is "socg"; paper.tex and notes/long.tex keep their names."""
    path = PurePosixPath(entry)
    if path.name == "main.tex" and len(path.parts) > 1:
        return slug("-".join(path.parent.parts))
    return slug("-".join(path.with_suffix("").parts))


def _paths(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list):
        raise ProjectError(f"{where} must be a list of repository-relative paths")
    return tuple(_relative(item, where) for item in value)


def detect_manuscripts(repository: Path) -> tuple[Manuscript, ...]:
    """LaTeX entry points (files with \\documentclass) near the repository root."""
    found: list[Manuscript] = []
    names: set[str] = set()
    for directory, children, files in os.walk(repository):
        relative_directory = Path(directory).relative_to(repository)
        depth = len(relative_directory.parts)
        children[:] = sorted(
            name for name in children
            if name not in SKIPPED_DIRECTORIES and not name.startswith(".")
            and depth < MAX_DETECTION_DEPTH
        )
        for name in sorted(files):
            if not name.endswith(".tex"):
                continue
            path = Path(directory) / name
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                continue
            if not re.search(r"^[^%\n]*\\documentclass", head, re.MULTILINE):
                continue
            relative = (relative_directory / name).as_posix()
            label = default_name(relative)
            while label in names:
                label += "-1"
            names.add(label)
            found.append(Manuscript(label, relative))
    return tuple(found)


def read_paper_project(repository: Path) -> dict:
    """The shared description of a paper repository (`loose-ends.toml`), checked.

        schema_version = 1
        id = "oriented-spanners"            # stable identity of the project
        title = "Oriented spanners"
        writing_profile = "writing/cg"      # optional, repository-relative
        audience = "computational geometers"
        reviews = "reviews"                 # optional, default: manuscript-reviews
        context = ["STATUS.md", "proofs"]   # optional: for the mathematical reviewer

        [[manuscript]]
        name = "socg"                       # unique in the project
        entry = "socg/main.tex"             # repository-relative LaTeX entry
        build = ["latexmk", "-pdf", "main.tex"]   # optional, run next to entry
        pdf = "socg/main.pdf"               # optional, default: entry as .pdf
        context = ["notes/socg"]            # optional, instead of the project's

    Without the file, LaTeX entry points are detected and nothing else is
    assumed. Shared descriptions contain no machine-specific paths.
    """
    path = repository / SHARED_PROJECT_FILE
    if not path.is_file():
        return {"described": False, "reviews": DEFAULT_REVIEWS, "context": (),
                "manuscripts": detect_manuscripts(repository)}
    value = _load_document(path)
    if value.get("schema_version") != PROJECTS_SCHEMA_VERSION:
        raise ProjectError(f"{path}: expected schema_version = {PROJECTS_SCHEMA_VERSION}")
    context = _paths(value.get("context"), f"{path} context")
    manuscripts = []
    names: set[str] = set()
    for index, raw in enumerate(value.get("manuscript", [])):
        where = f"{path} manuscript[{index}]"
        if not isinstance(raw, dict):
            raise ProjectError(f"{where} must be a table")
        entry = _relative(raw.get("entry"), f"{where} entry")
        name = raw.get("name") or default_name(entry)
        if not isinstance(name, str) or not NAME_RE.match(name) or name in names:
            raise ProjectError(f"{where} needs a unique name of letters, digits, . _ -")
        names.add(name)
        manuscripts.append(Manuscript(
            name,
            entry,
            _command(raw.get("build"), f"{where} build"),
            _relative(raw["pdf"], f"{where} pdf") if raw.get("pdf") else "",
            _paths(raw["context"], f"{where} context") if "context" in raw else context,
        ))
    for key in ("id", "title", "audience", "writing_profile"):
        if key in value and not isinstance(value[key], str):
            raise ProjectError(f"{path}: {key} must be a string")
    if value.get("writing_profile"):
        _relative(value["writing_profile"], f"{path} writing_profile")
    reviews = _relative(value["reviews"], f"{path} reviews") if value.get("reviews") else DEFAULT_REVIEWS
    return {
        "described": True,
        "reviews": reviews,
        "context": context,
        "id": value.get("id", ""),
        "title": value.get("title", ""),
        "audience": value.get("audience", ""),
        "writing_profile": value.get("writing_profile", ""),
        "manuscripts": tuple(manuscripts) or tuple(
            replace(item, context=context) for item in detect_manuscripts(repository)
        ),
    }


def _project(raw: object, base: Path, index: int) -> Project:
    where = f"project[{index}]"
    if not isinstance(raw, dict):
        raise ProjectError(f"{where} must be a table")
    name = raw.get("name")
    kind = raw.get("kind") or ("paper" if raw.get("path") and not raw.get("papers") else "papers")
    if kind not in PROJECT_KINDS:
        raise ProjectError(f"{where}: kind must be one of {', '.join(PROJECT_KINDS)}")
    if kind == "paper":
        path = _resolve(raw.get("path"), base, f"{where} path")
        name = name or slug(path.name)
    else:
        papers = _resolve(raw.get("papers"), base, f"{where} papers")
        name = name or papers.parent.name
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ProjectError(f"{where}: name must be letters, digits, . _ - (at most 64)")
    project_id = raw.get("id") or name
    if not isinstance(project_id, str):
        raise ProjectError(f"{where}: id must be a string")
    if kind == "paper":
        if not path.is_dir():
            return Project(name, project_id, kind, path=path, status="missing")
        try:
            shared = read_paper_project(path)
        except ProjectError as exc:
            return Project(name, project_id, kind, path=path, status=f"invalid: {exc}")
        problems = () if shared["manuscripts"] else ("no LaTeX entry point found",)
        if not shared["described"]:
            problems = (*problems, f"no {SHARED_PROJECT_FILE}; entry points were detected")
        return Project(
            name, shared.get("id") or project_id, kind, path=path,
            problems=problems, paper_manuscripts=shared["manuscripts"],
            title=shared.get("title", ""), writing_profile=shared.get("writing_profile", ""),
            audience=shared.get("audience", ""),
        )
    manuscripts = (
        _resolve(raw["manuscripts"], base, f"{where} manuscripts")
        if raw.get("manuscripts") else None
    )
    labels = raw.get("labels", [])
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list):
        raise ProjectError(f"{where}: labels must be a list of paths")
    return Project(
        name, project_id, kind,
        papers=papers,
        manuscripts=manuscripts,
        labels=tuple(_resolve(item, base, f"{where} labels") for item in labels),
        label_command=_command(raw.get("label_command"), f"{where} label_command"),
        status="ok" if papers.is_dir() else "missing",
        title=str(raw.get("title") or ""),
    )


def load_projects(path: Path) -> list[Project]:
    """Read a personal projects file; unavailable checkouts get status `missing`."""
    path = path.expanduser().resolve()
    value = _load_document(path)
    if value.get("schema_version") != PROJECTS_SCHEMA_VERSION:
        raise ProjectError(f"{path}: expected schema_version {PROJECTS_SCHEMA_VERSION}")
    entries = value.get("project", value.get("projects", []))
    if not isinstance(entries, list):
        raise ProjectError(f"{path}: project must be a list of tables")
    projects = [_project(raw, path.parent, index) for index, raw in enumerate(entries)]
    seen: set[str] = set()
    for project in projects:
        if project.name in seen:
            raise ProjectError(f"{path}: two projects are named {project.name!r}")
        seen.add(project.name)
    return projects


def project_for(projects: Sequence[Project], path: Path | str) -> Project | None:
    """The project a path belongs to (the most specific root wins)."""
    resolved = Path(path).expanduser().resolve()
    best: tuple[int, Project] | None = None
    for project in projects:
        for root in (project.papers, project.manuscripts, project.path):
            if root is not None and (resolved == root or root in resolved.parents):
                depth = len(root.parts)
                if best is None or depth > best[0]:
                    best = (depth, project)
    return best[1] if best else None

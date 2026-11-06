"""Portable references to files and folders: repository, path, commit.

A review records its inputs so that anyone with access can find them again,
without the paths of the machine it ran on:

    {"repository": "github.com/problem-commons/pool",
     "path": "papers/own/arXiv-2412.06316/OP-001/attempt-002",
     "commit": "3a1fb45...", "committed": true, "pushed": true}

`repository` is the remote of the checkout (credentials and `.git` removed;
None for a checkout without a hosted remote), `path` is relative to the
checkout's root, `committed` says whether the file (or every file in the
folder) is committed unchanged at `commit`, and `pushed` whether that commit
is on a remote branch as far as this checkout knows. Outside a git checkout
every field is None; the content hash recorded next to the reference is then
the only identification.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Sequence
from urllib.parse import urlsplit

GIT_TIMEOUT_SECONDS = 30
SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")


def _git(directory: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def normalize_remote(url: str | None) -> str | None:
    """host/owner/repo for a hosted remote; None for a local or unknown one."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https", "ssh", "git"} or not parts.hostname:
            return None  # file:// and other local transports
        host, path = parts.hostname, parts.path
    elif match := SCP_REMOTE.match(url):
        host, path = match.group(1), match.group(2)
    else:
        return None  # a local path
    path = path.strip("/").removesuffix(".git").strip("/")
    return f"{host.casefold()}/{path}" if path else None


@dataclass(frozen=True)
class Checkout:
    root: Path
    repository: str | None
    commit: str | None
    pushed: bool | None

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()


def checkout_root(path: Path) -> Path | None:
    directory = path if path.is_dir() else path.parent
    top = _git(directory, "rev-parse", "--show-toplevel")
    return Path(top.strip()).resolve() if top and top.strip() else None


def checkout_of(path: Path) -> Checkout | None:
    """The git checkout a file or folder belongs to, or None (read now, not cached)."""
    root = checkout_root(path)
    if root is None:
        return None
    commit = (_git(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip() or None
    remote = (_git(root, "config", "--get", "remote.origin.url") or "").strip()
    pushed = None
    if commit and (_git(root, "remote") or "").strip():
        pushed = bool((_git(root, "branch", "-r", "--contains", commit) or "").strip())
    return Checkout(root, normalize_remote(remote), commit, pushed)


def changed_paths(checkout: Checkout, path: Path) -> set[str] | None:
    """Paths below `path` (relative to the checkout) that are not committed as they are.

    Modified, staged, untracked and ignored files all count; an ignored folder
    is reported once with a trailing slash. None if git could not tell.
    """
    output = _git(
        checkout.root, "status", "--porcelain=v1", "-z", "--ignored=matching",
        "--untracked-files=all", "--", checkout.relative(path) or ".",
    )
    if output is None:
        return None
    changed: set[str] = set()
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        changed.add(entry[3:])
        if entry[0] in "RC":
            index += 1  # the original name of a rename or copy
    return changed


def is_changed(relative: str, changed: set[str]) -> bool:
    return relative in changed or any(
        item.endswith("/") and relative.startswith(item) for item in changed
    )


def describe(path: Path, members: Sequence[Path] = ()) -> tuple[dict, list[dict]]:
    """The reference of a file or folder, and the path and state of each member file."""
    path = path.resolve()
    checkout = checkout_of(path)
    if checkout is None:
        return (
            {"repository": None, "path": None, "commit": None, "committed": None, "pushed": None},
            [{"path": None, "committed": None} for _ in members],
        )
    relative = checkout.relative(path)
    changed = changed_paths(checkout, path) if checkout.commit else None
    if changed is None:
        committed = None
    elif path.is_dir():
        committed = not changed
    else:
        committed = not is_changed(relative, changed)
    states = []
    for member in members:
        member_path = checkout.relative(member)
        states.append({
            "path": member_path,
            "committed": None if changed is None else not is_changed(member_path, changed),
        })
    return {
        "repository": checkout.repository,
        "path": relative or ".",
        "commit": checkout.commit,
        "committed": committed,
        "pushed": checkout.pushed,
    }, states


def reference(path: Path) -> dict:
    """Where a file or folder lives, independent of this machine."""
    return describe(path)[0]


def label(value: dict, fallback: str = "") -> str:
    """repository:path@commit for display."""
    if not value.get("commit"):
        return fallback
    where = value.get("repository") or "local checkout"
    return f"{where}:{value.get('path')}@{value['commit'][:10]}"


def warnings(name: str, value: dict) -> list[str]:
    """Why a reference would not let someone else find exactly this content."""
    if value.get("commit") is None:
        return [f"{name} is not in a git checkout; only its hash identifies it"]
    found = []
    if value.get("committed") is False:
        found.append(f"{name} has uncommitted or untracked changes; only its hash identifies this version")
    if value.get("repository") is None:
        found.append(f"{name} is in a checkout without a hosted remote; others cannot look it up")
    elif value.get("pushed") is False:
        found.append(f"{name}: commit {value['commit'][:10]} is not pushed; push it so that others can look it up")
    return found

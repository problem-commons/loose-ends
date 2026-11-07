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
import hashlib
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
    """The nearest folder with a .git (directory, or file for a worktree); no git call."""
    directory = (path if path.is_dir() else path.parent).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def checkout_of(path: Path, cache: dict | None = None) -> Checkout | None:
    """The git checkout a file or folder belongs to, or None.

    Read now; `cache` (a dict kept by the caller) shares one reading among
    several folders of the same checkout.
    """
    root = checkout_root(path)
    if root is None:
        return None
    if cache is not None and root in cache:
        return cache[root]
    checkout = _read_checkout(root)
    if cache is not None:
        cache[root] = checkout
    return checkout


def _read_checkout(root: Path) -> Checkout:
    commit = (_git(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip() or None
    remote = (_git(root, "config", "--get", "remote.origin.url") or "").strip()
    pushed = None
    if commit and (_git(root, "remote") or "").strip():
        pushed = bool((_git(root, "branch", "-r", "--contains", commit) or "").strip())
    return Checkout(root, normalize_remote(remote), commit, pushed)


def committed_blobs(checkout: Checkout, path: Path) -> dict[str, str] | None:
    """Blob ids at HEAD of the files below `path`, by path relative to the checkout.

    Reads only git's objects. `git status` would refresh the index over the
    whole folder, which takes minutes on a Windows drive seen from WSL.
    """
    if checkout.commit is None:
        return None
    output = _git(
        checkout.root, "ls-tree", "-r", "-z", "--full-name", "HEAD", "--",
        checkout.relative(path) or ".",
    )
    if output is None:
        return None
    blobs = {}
    for entry in output.split("\0"):
        meta, _, name = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            blobs[name] = parts[2]
    return blobs


def _blob_ids(path: Path) -> set[str]:
    """Git blob ids of a file, as checked out and with CRLF line ends normalized."""
    data = path.read_bytes()
    ids = {hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}  # git object id
    if b"\r\n" in data:
        normalized = data.replace(b"\r\n", b"\n")
        ids.add(hashlib.sha1(b"blob %d\0" % len(normalized) + normalized).hexdigest())
    return ids


def is_committed(checkout: Checkout, path: Path, blobs: dict[str, str]) -> bool:
    """Whether the file is at HEAD with exactly this content."""
    blob = blobs.get(checkout.relative(path))
    try:
        return blob is not None and blob in _blob_ids(path)
    except OSError:
        return False


def _files(folder: Path) -> list[Path]:
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(folder).parts
    )


def describe(
    path: Path, members: Sequence[Path] | None = None, *, status: bool = True,
    cache: dict | None = None,
) -> tuple[dict, list[dict]]:
    """The reference of a file or folder, and the path and state of each member file.

    `committed` says whether the file, or every file in the folder, is at the
    commit as it is (None without `status`). Members default to the folder's files.
    """
    path = path.resolve()
    checkout = checkout_of(path, cache)
    if members is None:
        members = (_files(path) if path.is_dir() else [path]) if status else []
    if checkout is None:
        return (
            {"repository": None, "path": None, "commit": None, "committed": None, "pushed": None},
            [{"path": None, "committed": None} for _ in members],
        )
    relative = checkout.relative(path)
    blobs = committed_blobs(checkout, path) if status else None
    states = [
        {"path": checkout.relative(member),
         "committed": None if blobs is None else is_committed(checkout, member, blobs)}
        for member in members
    ]
    if blobs is None:
        committed = None
    elif path.is_dir():
        present = {state["path"] for state in states}
        # Every file committed as it is, and no committed file deleted.
        committed = all(state["committed"] for state in states) and set(blobs) <= present
    else:
        committed = is_committed(checkout, path, blobs)
    return {
        "repository": checkout.repository,
        "path": relative or ".",
        "commit": checkout.commit,
        "committed": committed,
        "pushed": checkout.pushed,
    }, states


def reference(path: Path, *, status: bool = True, cache: dict | None = None) -> dict:
    """Where a file or folder lives, independent of this machine.

    Without `status` only repository, path, commit and pushed are read, which
    is quick for any folder; `committed` is then None.
    """
    return describe(path, status=status, cache=cache)[0]


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

"""Environment signals: the AST analyzer judges a command by its text (path rules); this layer
looks at the files it would actually touch in the real workspace and adjusts that risk.

Per scoped action (write/overwrite/delete/perm_change) on existing files:
  secret    content has a key/password (detect-secrets: regex + entropy) -> block, whatever the name
  perm      owner is not you (e.g. root), private mode (no group/other bits, e.g. 600),
            or not writable by you                                  -> at least dangerous
  git       not tracked by git (untracked or ignored): hard to restore -> +1 level
  snapshot  workspace is snapshotted and the action stays inside it  -> -1 level
  bulk      sandbox dry run changes more than `max_changed` files     -> confirm
Order: AST risk, +git, -snapshot, then the perm floor (a snapshot never undoes it).
"""
from __future__ import annotations

import base64
import glob
import io
import os
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..analyzer import LEVELS, RULES, Analysis, rank

IN_WORKSPACE = {"single", "glob", "recursive"}  # scopes a workspace snapshot covers
MAX_FILES = 2000  # per target when expanding a directory statically
MAX_SCAN = 2 * 1024 * 1024


@dataclass
class EnvResult:
    risk: str
    block: bool = False
    bulk: bool = False  # dry run changed more files than allowed: ask before running
    n_changed: Optional[int] = None  # None = no dry run
    reasons: list[str] = field(default_factory=list)


def assess(command: str, a: Analysis, root: str, *, snapshot: bool = True,
           backend=None, max_changed: int = 20) -> EnvResult:
    root = str(Path(root).resolve())
    tracked = _tracked(root)
    reasons: list[str] = []
    changed = None
    local = a.parseable and not a.opaque and a.scopes <= IN_WORKSPACE
    if backend is not None and local:  # only commands that statically stay in the workspace are dry-run
        changed = dry_run(command, root, backend)

    risks = [rank(a.risk)] if a.opaque or not a.parseable else []
    secret = False
    for act in a.actions:
        if act.effect not in RULES["scoped_effects"]:
            risks.append(rank(act.risk))
            continue
        files = _expand(act.targets, root)
        if changed is not None and act.scopes <= IN_WORKSPACE:
            files = [os.path.join(root, p) for p in changed]  # exact: what the dry run really changed
        r = rank(act.risk)
        sig = _collect(files, root, tracked)
        if sig["secret"]:
            secret = True
            reasons.append(f"secret in {_rel(sig['secret'], root)}")
        if sig["untracked"]:
            r += 1
            reasons.append(f"not tracked by git: {_rel(sig['untracked'], root)}")
        if snapshot and act.scopes <= IN_WORKSPACE:
            r -= 1
        r = min(max(r, 0), len(LEVELS) - 1)
        if sig["perm"]:
            r = max(r, rank("dangerous"))
            reasons.append(f"owner/mode/permission: {_rel(sig['perm'], root)}")
        risks.append(r)
    if snapshot:
        reasons.append("workspace snapshot: in-workspace changes are undoable")
    risk = LEVELS[max(risks)] if risks else a.risk
    bulk = changed is not None and len(changed) > max_changed
    if bulk:
        reasons.append(f"dry run changed {len(changed)} files (> {max_changed})")
    return EnvResult(risk, secret, bulk, None if changed is None else len(changed), reasons)


def _collect(files: list[str], root: str, tracked: Optional[set]) -> dict[str, list[str]]:
    out = {"secret": [], "perm": [], "untracked": []}
    for f in files:
        for s in _signals(f, root, tracked):
            out[s].append(f)
    return out


def _signals(path: str, root: str, tracked: Optional[set]) -> set[str]:
    out = set()
    try:
        st = os.stat(path)
    except OSError:
        return out  # does not exist (yet): nothing to lose
    if os.name == "posix":  # uid and permission bits are meaningless on Windows
        # owner-write bit as well as access(): root may write a 0444 file, but the mode says "don't"
        if st.st_uid != os.geteuid() or (st.st_mode & 0o077) == 0 or not st.st_mode & 0o200 \
                or not os.access(path, os.W_OK):
            out.add("perm")
    if _inside(path, root) and (tracked is None or os.path.relpath(path, root).replace(os.sep, "/") not in tracked):
        out.add("untracked")
    if has_secret(path):
        out.add("secret")
    return out


def has_secret(path: str) -> bool:
    from detect_secrets import SecretsCollection  # optional dep: pip install -e .[env]
    from detect_secrets.settings import default_settings

    # ponytail: files over MAX_SCAN bytes are not scanned (a dump with a key in it slips through);
    # scan the head only if that matters. Unreadable files already carry the perm signal.
    try:
        if os.path.getsize(path) > MAX_SCAN or not os.access(path, os.R_OK):
            return False
    except OSError:
        return False
    s = SecretsCollection()
    with default_settings():
        s.scan_file(path)
    return bool(s.data)


def _tracked(root: str) -> Optional[set]:
    """Paths (relative to root) git tracks; None = not a git repo, so nothing is tracked."""
    p = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, text=True)
    return set(p.stdout.split("\0")) - {""} if p.returncode == 0 else None


def _expand(targets: tuple, root: str) -> list[str]:
    # ponytail: static expansion ignores find's -name filters, so `find . -name x -delete` scans the
    # whole tree (over-flags). A dry run replaces this with the files really changed.
    out = []
    for t in targets:
        p = os.path.join(root, os.path.expanduser(t))
        for m in glob.glob(p) if any(c in t for c in "*?[") else [p]:
            if os.path.isdir(m) and _inside(m, root):  # outside dirs are system_path already
                for d, dirs, fs in os.walk(m):
                    dirs[:] = [x for x in dirs if x != ".git"]
                    out += [os.path.join(d, f) for f in fs]
                    if len(out) > MAX_FILES:
                        break
            else:
                out.append(m)
    return out


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([root, os.path.abspath(path)]) == root
    except ValueError:  # different drives on Windows
        return False


def _rel(files: list[str], root: str) -> str:
    names = [os.path.relpath(f, root) if _inside(f, root) else f for f in files]
    return ", ".join(names[:5]) + (f" (+{len(names) - 5})" if len(names) > 5 else "")


def dry_run(command: str, root: str, backend) -> list[str]:
    """Run the command on a copy of the workspace in the sandbox; relpaths it created, changed or removed."""
    # ponytail: ships the whole workspace (incl. .git) per call; bind-mount + overlay if repos get big.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(root, arcname=".")
    res = backend.run({"archive": base64.b64encode(buf.getvalue()).decode(), "command": command,
                       "check_type": "stdout_contains", "check_expected": "", "timeout": 10})
    if "changed" not in res:  # Docker bakes runner.py into the image
        raise RuntimeError(f"sandbox runner predates dry runs (rebuild: docker rmi vishell-sandbox): {res}")
    return res["changed"]

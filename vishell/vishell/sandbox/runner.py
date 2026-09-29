"""Run ONE episode: setup -> hash -> command -> hash -> check. Stdlib only (no imports
from the rest of vishell), because this file is what actually executes inside the
sandbox (Docker container or `unshare`d subprocess) — it must work standalone.

Protocol: reads one JSON object from stdin, writes one JSON object to stdout.
Input:  {"setup": str, "command": str, "check_type": str, "check_expected": str,
         "timeout": float, "undo": str (optional), "archive": base64 tar.gz (optional,
         unpacked into the workspace before setup: envcheck's dry run of a real workspace)}
Output: {"setup_rc", "rc", "stdout", "stderr", "fs_changed", "changed", "check_passed",
         "timed_out", "duration", "boundary_violation"} plus, when "undo" is given,
        {"undo_rc", "undo_score", "undo_lost"} (see undo_metrics).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

MAX_OUTPUT = 20_000  # chars kept in stdout/stderr; long tails are truncated, not head
CPU_SECONDS = 20
MEM_BYTES = 512 * 1024 * 1024
FSIZE_BYTES = 100 * 1024 * 1024
NPROC = 64

_BOUNDARY_PATTERNS = (
    "network is unreachable", "could not resolve host", "connection refused",
    "temporary failure in name resolution", "no route to host", "name or service not known",
    "operation not permitted", "read-only file system", "permission denied",
)


def _preexec_limits():
    """POSIX resource limits for the child process. No-op if `resource` is unavailable
    (e.g. accidentally imported on Windows) — Docker/unshare backends always run this
    on Linux, where the limits actually apply."""
    try:
        import resource
    except ImportError:
        return

    def _set():
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (MEM_BYTES, MEM_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
        os.setsid()

    return _set


def _env(workdir: str) -> dict:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": workdir,
    }


def tree_entries(root: str, skip_git: bool = False) -> list[tuple]:
    """Every (kind, relpath, mode, content-sha256) under root, sorted."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_git and ".git" in dirnames:
            dirnames.remove(".git")
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        for d in dirnames:
            p = os.path.join(dirpath, d)
            mode = oct(os.stat(p).st_mode & 0o7777)
            entries.append(("dir", os.path.join(rel_dir, d), mode, ""))
        for f in sorted(filenames):
            p = os.path.join(dirpath, f)
            rel = os.path.join(rel_dir, f)
            try:
                st = os.stat(p)
                mode = oct(st.st_mode & 0o7777)
                with open(p, "rb") as fh:
                    content_hash = hashlib.sha256(fh.read()).hexdigest()
            except OSError as e:
                mode, content_hash = "ERR", str(e)
            entries.append(("file", rel, mode, content_hash))
    entries.sort()
    return entries


def hash_tree(root: str) -> str:
    """Hash of every (relpath, mode, content) under root, order-independent of walk order."""
    blob = json.dumps(tree_entries(root), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def undo_metrics(before: list, after: list) -> tuple[float, float]:
    """(score, lost) comparing the tree before the command with the tree after its undo.
    score = Jaccard of the (kind, path, mode, hash) entry sets, 1.0 = restored exactly.
    lost = share of entries that existed before and are now gone or altered: pre-existing
    data destroyed, which the reward punishes on top of a low score."""
    b, a = {tuple(e) for e in before}, {tuple(e) for e in after}
    union = b | a
    score = len(b & a) / len(union) if union else 1.0
    lost = len(b - a) / len(b) if b else 0.0
    return score, lost


def _run(cmd: str, cwd: str, timeout: float) -> tuple[int, str, str, bool]:
    """Run `cmd` via bash -c under cwd with resource limits. Returns (rc, stdout, stderr, timed_out)."""
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=cwd,
            env=_env(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_preexec_limits(),
        )
        return proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
        err = (e.stderr or b"") if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
        if isinstance(out, (bytes, bytearray)):
            out = out.decode("utf-8", "replace")
        if isinstance(err, (bytes, bytearray)):
            err = err.decode("utf-8", "replace")
        return -1, out, err, True
    except Exception as e:
        return -1, "", f"runner exception: {e}", False


def _truncate(s: str) -> str:
    if len(s) <= MAX_OUTPUT:
        return s
    return s[:MAX_OUTPUT] + f"\n...[truncated {len(s) - MAX_OUTPUT} chars]"


def _check(check_type: str, check_expected: str, stdout: str, cwd: str, timeout: float) -> bool:
    if check_type == "stdout_equals":
        return stdout.rstrip("\n") == check_expected.rstrip("\n")
    if check_type == "stdout_contains":
        return check_expected in stdout
    if check_type == "stdout_lines_set":
        got = {ln.rstrip() for ln in stdout.splitlines()}
        want = {ln.rstrip() for ln in check_expected.splitlines()}
        return got == want
    if check_type == "script":
        rc, _, _, timed_out = _run(check_expected, cwd, timeout)
        return rc == 0 and not timed_out
    raise ValueError(f"unknown check_type: {check_type!r}")


def run_episode(payload: dict) -> dict:
    t0 = time.time()
    workdir = tempfile.mkdtemp(prefix="vishell_ws_")
    try:
        setup = payload.get("setup") or ""
        command = payload["command"]
        timeout = float(payload.get("timeout", 10))
        check_type = payload["check_type"]
        check_expected = payload.get("check_expected", "")
        if payload.get("archive"):
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload["archive"]))) as tf:
                if hasattr(tarfile, "data_filter"):
                    tf.extractall(workdir, filter="data")
                else:
                    tf.extractall(workdir)

        setup_rc = 0
        if setup.strip():
            setup_rc, _, setup_err, setup_timed_out = _run(setup, workdir, timeout)
            if setup_rc != 0 or setup_timed_out:
                return {
                    "setup_rc": setup_rc, "rc": None, "stdout": "", "stderr": _truncate(setup_err),
                    "fs_changed": False, "check_passed": False, "timed_out": bool(setup_timed_out),
                    "duration": time.time() - t0, "boundary_violation": False,
                    "error": "setup failed",
                }

        hash_before = hash_tree(workdir)
        # ponytail: .git is left out of the undo comparison (object/reflog/index churn makes a
        # correct git undo never score 1); git history changes are therefore not measured.
        tree_before = tree_entries(workdir, skip_git=True)
        rc, stdout, stderr, timed_out = _run(command, workdir, timeout)
        hash_after = hash_tree(workdir)
        fs_changed = hash_before != hash_after
        changed = sorted({e[1] for e in set(tree_before) ^ set(tree_entries(workdir, skip_git=True))
                          if e[0] == "file"})

        low_err = stderr.lower()
        boundary_violation = any(p in low_err for p in _BOUNDARY_PATTERNS) and rc != 0

        try:
            check_passed = (not timed_out) and _check(check_type, check_expected, stdout, workdir, timeout)
        except Exception:
            check_passed = False

        result = {
            "setup_rc": setup_rc, "rc": rc, "stdout": _truncate(stdout), "stderr": _truncate(stderr),
            "fs_changed": fs_changed, "changed": [os.path.normpath(c) for c in changed],
            "check_passed": check_passed, "timed_out": timed_out,
            "duration": time.time() - t0, "boundary_violation": boundary_violation,
        }
        undo = payload.get("undo")
        if undo is not None:  # after the check, which may inspect the post-command tree
            undo_rc = _run(undo, workdir, timeout)[0] if undo.strip() else 0
            score, lost = undo_metrics(tree_before, tree_entries(workdir, skip_git=True))
            result.update(undo_rc=undo_rc, undo_score=score, undo_lost=lost)
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def restore_check(payload: dict) -> dict:
    """Used only by verify.py to certify R1 templates: run setup, snapshot, run command,
    restore snapshot, and confirm the restored hash matches the pre-command hash exactly."""
    workdir = tempfile.mkdtemp(prefix="vishell_ws_")
    snapshot = tempfile.mkdtemp(prefix="vishell_snap_")
    try:
        setup = payload.get("setup") or ""
        command = payload["command"]
        timeout = float(payload.get("timeout", 10))
        if setup.strip():
            setup_rc, _, setup_err, _ = _run(setup, workdir, timeout)
            if setup_rc != 0:
                return {"restored_ok": False, "error": f"setup failed: {setup_err}"}

        shutil.rmtree(snapshot)
        shutil.copytree(workdir, snapshot)
        hash_before = hash_tree(workdir)

        rc, stdout, stderr, timed_out = _run(command, workdir, timeout)
        hash_after = hash_tree(workdir)

        for item in os.listdir(workdir):
            p = os.path.join(workdir, item)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        for item in os.listdir(snapshot):
            src, dst = os.path.join(snapshot, item), os.path.join(workdir, item)
            shutil.copytree(src, dst) if os.path.isdir(src) else shutil.copy2(src, dst)
        hash_restored = hash_tree(workdir)

        return {
            "restored_ok": hash_restored == hash_before,
            "fs_changed": hash_before != hash_after,
            "rc": rc, "timed_out": timed_out, "stdout": _truncate(stdout), "stderr": _truncate(stderr),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(snapshot, ignore_errors=True)


def main() -> None:
    payload = json.load(sys.stdin)
    mode = payload.pop("_mode", "run")
    result = restore_check(payload) if mode == "restore_check" else run_episode(payload)
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()

"""Two interchangeable ways to run one sandbox/runner.py episode: LocalBackend
(Linux/Colab, `unshare -rn` to cut network if available) and DockerBackend (Windows
laptop, or anywhere Docker is preferred over root-level unshare). Same interface:
`backend.run(payload) -> dict`, `backend.run_many(payloads) -> list[dict]`.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_RUNNER_PATH = Path(__file__).resolve().parent / "runner.py"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # vishell/ (contains docker/, vishell/)
_IMAGE = "vishell-sandbox"


class LocalBackend:
    """Runs runner.py as a direct subprocess under `unshare -rn` (no network). Refuses to
    run without it: generated commands are never executed unsandboxed. Note this isolates
    network only, not the filesystem -- use it on disposable VMs (Colab), DockerBackend elsewhere."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        # The binary existing isn't enough: Kaggle ships `unshare` but forbids user
        # namespaces ("Operation not permitted"), so probe that it actually works.
        self._has_unshare = shutil.which("unshare") is not None and subprocess.run(
            ["unshare", "-rn", "true"], capture_output=True
        ).returncode == 0
        if not self._has_unshare:
            raise RuntimeError("LocalBackend needs working `unshare -rn` (user namespaces); "
                               "refusing to execute generated commands unsandboxed. Use DockerBackend.")

    def run(self, payload: dict) -> dict:
        cmd = ["unshare", "-rn", sys.executable, str(_RUNNER_PATH)]
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True,
            timeout=self.timeout + 15,
        )
        if not proc.stdout.strip():
            raise RuntimeError(f"sandbox runner produced no output (rc={proc.returncode}): {proc.stderr}")
        return json.loads(proc.stdout)

    def run_many(self, payloads: list[dict], max_workers: int = 8) -> list[dict]:
        return _run_many(self, payloads, max_workers)


class DockerBackend:
    """Runs runner.py inside a locked-down, disposable container: no network, capped
    memory/cpu/pids, read-only root with a tmpfs /work for scratch space."""

    def __init__(self, timeout: float = 30, image: str = _IMAGE):
        self.timeout = timeout
        self.image = image
        self._ensure_image()

    def _ensure_image(self) -> None:
        check = subprocess.run(
            ["docker", "image", "inspect", self.image], capture_output=True, text=True
        )
        if check.returncode == 0:
            return
        build = subprocess.run(
            ["docker", "build", "-f", "docker/Dockerfile.sandbox", "-t", self.image, "."],
            cwd=_REPO_ROOT, capture_output=True, text=True,
        )
        if build.returncode != 0:
            raise RuntimeError(f"failed to build {self.image}:\n{build.stdout}\n{build.stderr}")

    def run(self, payload: dict) -> dict:
        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", "none", "--memory", "256m", "--cpus", "0.5",
            "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp",
            self.image,
        ]
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True,
            timeout=self.timeout + 15,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"docker run failed: {proc.stderr}")
        return json.loads(proc.stdout)

    def run_many(self, payloads: list[dict], max_workers: int = 8) -> list[dict]:
        return _run_many(self, payloads, max_workers)


_cache: dict[tuple, dict] = {}


def _run_many(backend, payloads: list[dict], max_workers: int) -> list[dict]:
    def _one(p: dict) -> dict:
        key = (p.get("instance_id"), p["command"])
        if key in _cache:
            return _cache[key]
        result = backend.run({k: v for k, v in p.items() if k != "instance_id"})
        _cache[key] = result
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, payloads))


def auto_backend(timeout: float = 30):
    """Docker on Windows (per AGENT.md); LocalBackend (unshare-guarded) elsewhere."""
    if platform.system() == "Windows":
        return DockerBackend(timeout=timeout)
    return LocalBackend(timeout=timeout)
